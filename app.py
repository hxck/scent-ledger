import csv
import functools
import hmac
import io
import ipaddress
import json
import os
import random
import re
import socket
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

from flask import (
    Flask, g, render_template, request, redirect, url_for, flash, abort, session, Response
)
from PIL import Image
import markdown as markdown_lib
import bleach

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = os.environ.get("DATABASE_PATH", str(BASE_DIR / "data" / "scent_ledger.db"))
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", str(BASE_DIR / "static" / "uploads"))
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_IMAGE_WIDTH = 900
SEASON_CHOICES = ["Spring", "Summer", "Fall", "Winter"]
DAYNIGHT_CHOICES = ["Day", "Night", "Both"]

# Single hardcoded admin account — no signup, no user table. Browsing (home,
# search, detail, stats) stays public; only Add/Edit/Delete require this login.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12MB upload cap
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # baseline CSRF mitigation for the write routes
# Set SESSION_COOKIE_SECURE=true once this is served over HTTPS (it should be,
# for a public-facing site with a login form) — off by default so local
# http://localhost testing without TLS still works.
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"

# Static assets are served with a long max-age and a cache-busting ?v= stamp
# derived from each file's mtime (see _static_cache_bust below). Without the
# stamp a long max-age would mean CSS and JS edits don't reach anyone until
# their cache expires; with it, the URL changes the moment the file does, so
# repeat visits can safely reuse everything — stylesheet, script, and every
# bottle photo — instead of refetching them.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(days=365)


@app.url_defaults
def _static_cache_bust(endpoint, values):
    if endpoint != "static" or "filename" not in values:
        return
    try:
        stamp = int(os.stat(os.path.join(app.static_folder, values["filename"])).st_mtime)
    except OSError:
        return  # missing file: let the normal 404 happen, don't stamp it
    values["v"] = stamp
app.permanent_session_lifetime = 30 * 24 * 60 * 60  # 30 days

if ADMIN_PASSWORD == "changeme":
    print(
        "WARNING: ADMIN_PASSWORD is not set — using the insecure default. "
        "Set ADMIN_USERNAME and ADMIN_PASSWORD before exposing this app publicly.",
        flush=True,
    )


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            if request.method == "GET":
                return redirect(url_for("login", next=request.path))
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_auth_state():
    return {"is_admin": bool(session.get("logged_in"))}


@app.template_filter("note_texts")
def note_texts_filter(items):
    """Notes reach templates in two shapes: {text, image} dicts from
    get_fragrance_full() (detail/edit pages), or plain strings when a form
    validation error re-renders straight from submitted data. Normalizes both
    to a plain string list, e.g. for pre-filling the chip inputs."""
    return [item["text"] if isinstance(item, dict) else item for item in (items or [])]


@app.template_filter("money")
def money_filter(value):
    """$1,234 — thousands-separated, no decimals, for larger aggregate totals."""
    if value is None:
        return ""
    return "${:,.0f}".format(value)


Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _run_migrations(conn):
    """CREATE TABLE IF NOT EXISTS in schema.sql only helps fresh installs — an
    existing database from before a column was added needs it added explicitly."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(fragrances)").fetchall()}
    if "price" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN price REAL")
    if "is_gift" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN is_gift INTEGER NOT NULL DEFAULT 0")
    if "is_discontinued" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN is_discontinued INTEGER NOT NULL DEFAULT 0")
    if "is_wishlist" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN is_wishlist INTEGER NOT NULL DEFAULT 0")
    if "currently_owned" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN currently_owned INTEGER NOT NULL DEFAULT 1")
    if "gave_away" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN gave_away INTEGER NOT NULL DEFAULT 0")
    if "bottles_owned" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN bottles_owned INTEGER NOT NULL DEFAULT 1")
    if "fragrantica_url" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN fragrantica_url TEXT")

    shelves_cols = {row[1] for row in conn.execute("PRAGMA table_info(shelves)").fetchall()}
    if "icon" not in shelves_cols:
        conn.execute("ALTER TABLE shelves ADD COLUMN icon TEXT")
    if "is_private" not in shelves_cols:
        conn.execute("ALTER TABLE shelves ADD COLUMN is_private INTEGER NOT NULL DEFAULT 0")

    if "rating" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN rating INTEGER")
    if "private_notes" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN private_notes TEXT")

    if "longevity_rating" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN longevity_rating INTEGER")
    if "sillage_rating" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN sillage_rating INTEGER")
    if "projection_rating" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN projection_rating INTEGER")

    scraps_cols = {row[1] for row in conn.execute("PRAGMA table_info(scraps)").fetchall()}
    if "is_private" not in scraps_cols:
        conn.execute("ALTER TABLE scraps ADD COLUMN is_private INTEGER NOT NULL DEFAULT 0")

    # Rich wear-log context. All nullable — logging a wear stays one click.
    wear_cols = {row[1] for row in conn.execute("PRAGMA table_info(wear_log)").fetchall()}
    for col, ddl in [
        ("container_id", "INTEGER REFERENCES containers(id) ON DELETE SET NULL"),
        ("occasion", "TEXT"),
        ("weather_temp_f", "REAL"),
        ("weather_summary", "TEXT"),
        ("sprays", "INTEGER"),
        ("longevity_hours", "REAL"),
        ("complimented", "INTEGER NOT NULL DEFAULT 0"),
        ("wear_note", "TEXT"),
    ]:
        if col not in wear_cols:
            conn.execute(f"ALTER TABLE wear_log ADD COLUMN {col} {ddl}")

    # Deliberately here rather than in schema.sql: on an upgrade the column
    # above doesn't exist until the ALTER has run, and schema.sql executes
    # before any of this.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wear_log_container ON wear_log(container_id)")

    _backfill_containers(conn)
    _repair_backfilled_container_dates(conn)
    _drop_legacy_fragrance_columns(conn)


def _backfill_containers(conn):
    """Give every pre-containers fragrance a single 'bottle' container that
    carries over whatever size/fill/price it already had, so existing
    collections keep their cost-per-wear and fill-level data instead of
    silently resetting.

    Idempotent by construction: it only touches fragrances that have no
    container yet, so re-running it (every startup) is a no-op, and a
    fragrance you deliberately emptied of containers won't get one
    resurrected on the next boot... except that's exactly the case the
    NOT EXISTS clause can't distinguish, which is why the whole backfill
    is additionally gated behind a one-time settings flag below.
    """
    already_done = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'containers_backfilled'"
    ).fetchone()
    if already_done:
        return

    # On a fresh install these columns never existed — schema.sql stopped
    # creating them once containers became the source of truth. There's
    # nothing to carry over, so just mark the backfill done.
    legacy = {"size_ml", "fill_level", "purchase_price"}
    cols = {row[1] for row in conn.execute("PRAGMA table_info(fragrances)").fetchall()}
    if not legacy.issubset(cols):
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('containers_backfilled', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        return

    # created_at is carried over from the fragrance, not defaulted to now.
    # Defaulting would stamp every pre-existing bottle with the migration
    # date and collapse the whole Collection Timeline into a single spike
    # in whichever month the upgrade happened.
    conn.execute(
        """
        INSERT INTO containers (fragrance_id, container_type, size_ml, fill_level,
                                purchase_price, created_at)
        SELECT f.id, 'bottle', f.size_ml, f.fill_level, f.purchase_price, f.created_at
        FROM fragrances f
        WHERE f.is_wishlist = 0
          AND NOT EXISTS (SELECT 1 FROM containers c WHERE c.fragrance_id = f.id)
        """
    )
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('containers_backfilled', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = '1'"
    )


def _repair_backfilled_container_dates(conn):
    """One-time repair for databases migrated by the first version of
    _backfill_containers, which let created_at default to the migration
    timestamp. That collapsed every pre-existing bottle into whichever
    month the upgrade ran, so the Collection Timeline showed one large
    spike instead of real history.

    Scoped tightly on purpose: only a fragrance's *earliest* container,
    only when it has no recorded purchase_date (nothing to override), and
    only when its created_at is later than the fragrance's own — which is
    the signature of a container stamped at migration time rather than
    created alongside its fragrance. A container legitimately added later
    is left alone, because it isn't the earliest one.
    """
    if conn.execute(
        "SELECT 1 FROM app_settings WHERE key = 'container_dates_repaired'"
    ).fetchone():
        return

    conn.execute(
        """
        UPDATE containers
        SET created_at = (SELECT f.created_at FROM fragrances f WHERE f.id = containers.fragrance_id)
        WHERE purchase_date IS NULL
          AND id = (SELECT MIN(c2.id) FROM containers c2 WHERE c2.fragrance_id = containers.fragrance_id)
          AND created_at > (SELECT f.created_at FROM fragrances f WHERE f.id = containers.fragrance_id)
        """
    )
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('container_dates_repaired', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = '1'"
    )


def _drop_legacy_fragrance_columns(conn):
    """Retire fragrances.size_ml / fill_level / purchase_price.

    These were deliberately left in place through the containers migration
    so it could be rolled back. Containers has been the source of truth for
    all size and money since, which leaves two overlapping stores for the
    same facts — the kind of split that eventually produces a bug where one
    is updated and the other isn't.

    Only runs once the backfill has definitely completed, so the data is
    never dropped before it's been copied. SQLite gained ALTER TABLE DROP
    COLUMN in 3.35; on anything older the columns are simply left alone,
    which is harmless since nothing reads them any more.
    """
    backfilled = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'containers_backfilled'"
    ).fetchone()
    if not backfilled:
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info(fragrances)").fetchall()}
    targets = [c for c in ("size_ml", "fill_level", "purchase_price") if c in cols]
    if not targets:
        return

    if tuple(int(p) for p in sqlite3.sqlite_version.split(".")[:2]) < (3, 35):
        print(
            "NOTE: SQLite %s is too old for ALTER TABLE DROP COLUMN. Leaving the "
            "legacy fragrances columns in place — nothing reads them, so this is "
            "cosmetic only." % sqlite3.sqlite_version
        )
        return

    # Safety net: never drop a column while any non-wishlist fragrance is
    # still missing the container that should be holding its data.
    stranded = conn.execute(
        """
        SELECT COUNT(*) FROM fragrances f
        WHERE f.is_wishlist = 0
          AND NOT EXISTS (SELECT 1 FROM containers c WHERE c.fragrance_id = f.id)
        """
    ).fetchone()[0]
    if stranded:
        print(
            f"NOTE: {stranded} fragrance(s) have no container yet — leaving the "
            "legacy columns in place rather than risk dropping un-migrated data."
        )
        return

    for col in targets:
        conn.execute(f"ALTER TABLE fragrances DROP COLUMN {col}")


def init_db():
    schema_path = BASE_DIR / "schema.sql"
    conn = sqlite3.connect(DATABASE_PATH)
    with open(schema_path, "r") as f:
        conn.executescript(f.read())
    _run_migrations(conn)
    _sync_settings_from_env(conn)
    conn.commit()
    conn.close()


def _sync_settings_from_env(conn):
    """Re-syncs env-sourced settings into app_settings on every startup, so
    a changed TZ takes effect on restart rather than sticking with whatever
    was there from the very first run."""
    tz = os.environ.get("TZ", "Etc/UTC").strip() or "Etc/UTC"
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('timezone', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (tz,),
    )


init_db()


# ---------------------------------------------------------------------------
# Small query helpers
# ---------------------------------------------------------------------------
def _get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _set_setting(key, value):
    get_db().execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _humanize_days_ago(iso_datetime_str):
    """"2026-08-01 12:00:00" -> "today" / "yesterday" / "N days ago". Returns
    None on anything unparseable rather than raising — this is display text,
    never something that should break a page render."""
    if not iso_datetime_str:
        return None
    try:
        worn = datetime.strptime(iso_datetime_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    days = (datetime.utcnow() - worn).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _utc_to_local_display(iso_utc_str):
    """Converts a stored UTC timestamp (SQLite's datetime('now') always
    stores UTC, regardless of the configured TZ — that's correct, the
    database should hold one unambiguous timezone) into the configured
    local timezone for display. Returns the original string unconverted if
    it can't be parsed, rather than hiding a real timestamp behind a blank
    field."""
    if not iso_utc_str:
        return iso_utc_str
    try:
        naive = datetime.strptime(iso_utc_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return iso_utc_str
    tz_name = _get_setting("timezone", "Etc/UTC") or "Etc/UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    local = naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    return local.strftime("%b %-d, %Y %-I:%M %p")


_SCRAP_ALLOWED_TAGS = [
    "p", "br", "strong", "em", "del", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "code", "pre", "a", "hr",
    "table", "thead", "tbody", "tr", "th", "td",
]
_SCRAP_ALLOWED_ATTRS = {"a": ["href", "title"]}
_SCRAP_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def render_scrap_markdown(text):
    """Markdown -> sanitized HTML for Scraps. Scraps are admin-authored but
    publicly viewable, and Markdown allows embedding raw HTML by default —
    bleach strips anything outside an explicit allowlist (no <script>,
    no images, no javascript: links, no inline styles/event handlers)
    before this is ever marked safe for the template. Deliberately no
    <img> support: Scraps has no upload pipeline, so any image tag could
    only point at an arbitrary external URL, not something we control."""
    if not text:
        return ""
    html = markdown_lib.markdown(text, extensions=["fenced_code", "tables", "nl2br"])
    return bleach.clean(
        html, tags=_SCRAP_ALLOWED_TAGS, attributes=_SCRAP_ALLOWED_ATTRS,
        protocols=_SCRAP_ALLOWED_PROTOCOLS, strip=True,
    )


app.jinja_env.filters["render_scrap_markdown"] = render_scrap_markdown


def _trim_decimal(value):
    """70.0 -> '70', 1.5 -> '1.5'. Container sizes span whole-number bottles
    and fractional samples, and '70.0ml' reads like a measurement error."""
    if value is None:
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    return str(int(f)) if f == int(f) else str(round(f, 2))


app.jinja_env.filters["trim_decimal"] = _trim_decimal


def _accord_chips(accords):
    """Stored accord rows -> the 'name:strength' chip strings the form
    edits, so round-tripping an edit doesn't silently drop the strengths."""
    out = []
    for a in accords or []:
        name = a.get("name") if isinstance(a, dict) else str(a)
        if not name:
            continue
        strength = a.get("strength") if isinstance(a, dict) else None
        out.append(f"{name}:{_trim_decimal(strength)}" if strength else name)
    return out


app.jinja_env.filters["accord_chips"] = _accord_chips


def _set_todays_fragrance(fragrance_id):
    """Marks a fragrance as today's pick for the header slot — called only
    from "I Wore This Today". Deliberately not called from the random-pick
    (dice) button: that's a separate, unrelated feature that just picks
    something to look at and has nothing to do with what actually gets
    logged as worn. Doesn't commit itself — the caller's own commit covers
    this alongside whatever else it's doing in the same request."""
    _set_setting("today_frag_id", str(fragrance_id))
    _set_setting("today_frag_at", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))


def _get_todays_fragrance_pick():
    """For the header's Today's/Yesterday's Fragrance slot — set only by
    "I Wore This Today" (see _set_todays_fragrance). The random-pick (dice)
    button is unrelated and never touches this. Compares calendar dates in
    the configured local timezone rather than UTC, so a late-night log
    doesn't get mislabeled. Returns (None, None) if nothing's been set,
    the fragrance no longer exists, or it's older than yesterday — there's
    no good label for "3 days ago" here, so it just stops showing rather
    than displaying something misleading."""
    pick_id = _get_setting("today_frag_id")
    picked_at = _get_setting("today_frag_at")
    if not pick_id or not picked_at:
        return None, None
    try:
        picked_naive = datetime.strptime(picked_at.split(".")[0], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None, None

    tz_name = _get_setting("timezone", "Etc/UTC") or "Etc/UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")

    picked_local_date = picked_naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()
    today_local_date = datetime.now(tz).date()
    day_diff = (today_local_date - picked_local_date).days

    if day_diff == 0:
        label = "Today's Fragrance"
    elif day_diff == 1:
        label = "Yesterday's Fragrance"
    else:
        return None, None

    row = get_db().execute(
        "SELECT id, brand, name, image_filename FROM fragrances WHERE id = ?", (pick_id,)
    ).fetchone()
    if row is None:
        return None, None
    return dict(row), label


def _season_color_group(season_csv):
    """Classifies a fragrance's seasons for sidebar color-coding:
    - "warm" (Spring and/or Summer only) — creamy red-orange
    - "cool" (Fall and/or Winter only) — chilly blue
    - "year-round" (has at least one from each side) — green-yellow
    - None (no seasons set at all) — no color, we don't know anything about it
    """
    seasons = set((season_csv or "").split(","))
    seasons.discard("")
    has_warm = bool(seasons & {"Spring", "Summer"})
    has_cool = bool(seasons & {"Fall", "Winter"})
    if has_warm and has_cool:
        return "year-round"
    if has_warm:
        return "warm"
    if has_cool:
        return "cool"
    return None


def sidebar_groups():
    """Brand -> [ {id, name, season_group} ] for the sidebar, alphabetical.
    Wishlist items live only on the Wishlist page — they don't show here
    until "graduated" into the collection via the Edit page toggle. Respects
    the "owned only" display preference (session-based, works for any
    visitor, doesn't affect the home page, search, or stats — those always
    show the full collection). Returns the unfiltered total too, so the UI
    can tell "collection is genuinely empty" apart from "the current filter
    just matches nothing"."""
    db = get_db()
    total_unfiltered = db.execute(
        "SELECT COUNT(*) AS c FROM fragrances WHERE is_wishlist = 0"
    ).fetchone()["c"]

    query = """
        SELECT f.id, f.brand, f.name, GROUP_CONCAT(DISTINCT s.season) AS season_csv
        FROM fragrances f
        LEFT JOIN seasons s ON s.fragrance_id = f.id
        WHERE f.is_wishlist = 0
    """
    if session.get("sidebar_owned_only"):
        query += " AND f.currently_owned = 1"
    query += " GROUP BY f.id ORDER BY f.brand COLLATE NOCASE, f.name COLLATE NOCASE"
    rows = db.execute(query).fetchall()

    groups = {}
    for r in rows:
        item = dict(r)
        item["season_group"] = _season_color_group(item.pop("season_csv"))
        groups.setdefault(item["brand"], []).append(item)
    return sorted(groups.items(), key=lambda kv: kv[0].lower()), len(rows), total_unfiltered


def get_all_fragrances_grouped_by_brand():
    """All non-wishlist fragrances grouped by brand — used for the shelf
    "add fragrances" list. Same shape as sidebar_groups() but without the
    owned-only filter, since a shelf is an independent grouping, not tied to
    what you currently own."""
    db = get_db()
    rows = db.execute(
        "SELECT id, brand, name FROM fragrances WHERE is_wishlist = 0 "
        "ORDER BY brand COLLATE NOCASE, name COLLATE NOCASE"
    ).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["brand"], []).append(dict(r))
    return sorted(groups.items(), key=lambda kv: kv[0].lower())


def _find_possible_duplicate(db, brand, name, exclude_id=None):
    """Soft duplicate check for the Add/Edit form: same brand (case/space
    insensitive), and a name that's an exact match or a substring either way
    — catches things like "Baccarat Rouge 540" vs "Baccarat Rouge 540
    Extrait" as well as exact re-entries. Never blocks a save, just flags it
    — plenty of collections have genuinely distinct bottles with very
    similar names (different concentrations, flankers, etc.)."""
    norm_name = name.strip().lower()
    if not norm_name:
        return None
    query = "SELECT id, brand, name FROM fragrances WHERE LOWER(TRIM(brand)) = LOWER(TRIM(?))"
    params = [brand]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    for row in db.execute(query, params).fetchall():
        candidate_name = (row["name"] or "").strip().lower()
        if not candidate_name:
            continue
        if candidate_name == norm_name or candidate_name in norm_name or norm_name in candidate_name:
            return dict(row)
    return None


def _find_similar_fragrances(fragrance_id, limit=4):
    """Other owned (non-wishlist) fragrances that share the most notes with
    this one, case/whitespace-normalized. Returns nothing if this fragrance
    has no notes of its own — there'd be nothing meaningful to compare."""
    db = get_db()
    my_notes = db.execute(
        "SELECT DISTINCT LOWER(TRIM(note_text)) AS n FROM notes WHERE fragrance_id = ?",
        (fragrance_id,),
    ).fetchall()
    note_set = {r["n"] for r in my_notes if r["n"]}
    if not note_set:
        return []
    placeholders = ",".join("?" * len(note_set))
    rows = db.execute(
        f"""
        SELECT f.id, f.brand, f.name, f.image_filename,
               COUNT(DISTINCT LOWER(TRIM(n.note_text))) AS shared
        FROM fragrances f
        JOIN notes n ON n.fragrance_id = f.id
        WHERE f.id != ? AND f.is_wishlist = 0
          AND LOWER(TRIM(n.note_text)) IN ({placeholders})
        GROUP BY f.id
        ORDER BY shared DESC, f.brand COLLATE NOCASE, f.name COLLATE NOCASE
        LIMIT ?
        """,
        (fragrance_id, *note_set, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@app.context_processor
def inject_sidebar():
    groups, total, total_unfiltered = sidebar_groups()
    wishlist_count = get_db().execute(
        "SELECT COUNT(*) AS c FROM fragrances WHERE is_wishlist = 1"
    ).fetchone()["c"]
    missing_notes_count = get_db().execute(
        """
        SELECT COUNT(*) AS c FROM (
            SELECT LOWER(TRIM(n.note_text)) AS note_key
            FROM notes n
            LEFT JOIN note_images ni ON ni.note_key = LOWER(TRIM(n.note_text))
            WHERE ni.image_filename IS NULL
            GROUP BY LOWER(TRIM(n.note_text))
        )
        """
    ).fetchone()["c"]
    shelves_count_query = "SELECT COUNT(*) AS c FROM shelves"
    if not session.get("logged_in"):
        shelves_count_query += " WHERE is_private = 0"
    shelves_count = get_db().execute(shelves_count_query).fetchone()["c"]
    all_shelves_for_bulk = []
    if session.get("logged_in"):
        all_shelves_for_bulk = [
            dict(r) for r in get_db().execute(
                "SELECT id, name, is_private FROM shelves ORDER BY name COLLATE NOCASE"
            ).fetchall()
        ]
    todays_frag, todays_frag_label = _get_todays_fragrance_pick()
    scraps_count_query = "SELECT COUNT(*) AS c FROM scraps"
    if not session.get("logged_in"):
        scraps_count_query += " WHERE is_private = 0"
    scraps_count = get_db().execute(scraps_count_query).fetchone()["c"]
    return {
        "sidebar_groups": groups, "sidebar_total": total, "sidebar_brand_count": len(groups),
        "wishlist_count": wishlist_count, "sidebar_owned_only": bool(session.get("sidebar_owned_only")),
        "sidebar_total_unfiltered": total_unfiltered, "missing_notes_count": missing_notes_count,
        "shelves_count": shelves_count, "all_shelves_for_bulk": all_shelves_for_bulk,
        "todays_frag": todays_frag, "todays_frag_label": todays_frag_label,
        "scraps_count": scraps_count,
    }


def get_all_fragrances_light(wishlist=False, respect_owned_filter=False):
    """Used for the home grid, search results, and the wishlist page:
    brand/name/subname/thumb, plus a combined `tags` list used for search
    matching only (never rendered directly on a card) — this includes both
    real tags AND every note on the fragrance, so search-by-note works the
    same way search-by-tag does. The collection and the wishlist are
    mutually exclusive sets — this never mixes them.
    respect_owned_filter=True additionally applies the sidebar's "owned only"
    session preference (only meaningful for the non-wishlist collection) —
    opt-in per caller so this stays in sync with the sidebar specifically
    where that's wanted, without silently changing other listings."""
    db = get_db()
    query = """
        SELECT f.id, f.brand, f.name, f.subname, f.image_filename, f.daynight, f.price,
               GROUP_CONCAT(DISTINCT t.name) AS tag_list,
               GROUP_CONCAT(DISTINCT n.note_text) AS note_list,
               GROUP_CONCAT(DISTINCT s.season) AS season_list
        FROM fragrances f
        LEFT JOIN fragrance_tags ft ON ft.fragrance_id = f.id
        LEFT JOIN tags t ON t.id = ft.tag_id
        LEFT JOIN notes n ON n.fragrance_id = f.id
        LEFT JOIN seasons s ON s.fragrance_id = f.id
        WHERE f.is_wishlist = ?
    """
    if respect_owned_filter and not wishlist and session.get("sidebar_owned_only"):
        query += " AND f.currently_owned = 1"
    query += " GROUP BY f.id ORDER BY f.brand COLLATE NOCASE, f.name COLLATE NOCASE"
    rows = db.execute(query, (1 if wishlist else 0,)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        tag_list = d.pop("tag_list")
        note_list = d.pop("note_list")
        season_list = d.pop("season_list")
        tags = tag_list.split(",") if tag_list else []
        notes = note_list.split(",") if note_list else []
        d["tags"] = [t.strip() for t in (tags + notes) if t.strip()]
        d["seasons"] = season_list.split(",") if season_list else []
        result.append(d)
    return result


SPRAYS_PER_ML = 10
SPRAYS_PER_WEAR = 6
CONTAINER_TYPES = ["bottle", "decant", "sample", "travel"]
CONTAINER_TYPE_LABELS = {
    "bottle": "Bottle", "decant": "Decant", "sample": "Sample", "travel": "Travel Spray",
}


def _cost_per_wear_from_size(price, size_ml):
    """Cost per wear from container economics — 10 sprays per ml (1,000
    sprays in a 100ml bottle), 6 sprays per wear — rather than actual
    purchase price divided by times logged worn. This is a fixed number
    based on price and size alone; it doesn't change as you log more
    wears, and doesn't need any wear log data to compute at all.
    Needs both a price and a size to mean anything."""
    if not price or not size_ml:
        return None
    total_sprays = size_ml * SPRAYS_PER_ML
    return (price / total_sprays) * SPRAYS_PER_WEAR


def get_containers(fragrance_id):
    """Every physical container held for a fragrance, each with its own
    derived remaining volume and per-wear cost. Unfinished containers sort
    first, then by type (bottle before decant before sample)."""
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM containers WHERE fragrance_id = ?
        ORDER BY is_finished,
                 CASE container_type WHEN 'bottle' THEN 0 WHEN 'travel' THEN 1
                                     WHEN 'decant' THEN 2 ELSE 3 END,
                 id
        """,
        (fragrance_id,),
    ).fetchall()
    result = []
    for r in rows:
        c = dict(r)
        size = c["size_ml"]
        c["type_label"] = CONTAINER_TYPE_LABELS.get(c["container_type"], c["container_type"].title())
        c["ml_remaining"] = round(size * c["fill_level"] / 100.0, 2) if size else None
        c["wears_remaining"] = (
            int(c["ml_remaining"] * SPRAYS_PER_ML / SPRAYS_PER_WEAR) if c["ml_remaining"] else None
        )
        c["cost_per_wear"] = _cost_per_wear_from_size(c["purchase_price"], size)
        result.append(c)
    return result


def _container_economics(containers, wear_count, first_worn_at):
    """Fragrance-level rollup across containers.

    cost_per_wear is *blended* rather than averaged: total money spent
    divided by total sprays bought. Averaging the per-container figures
    would let a $8 sample distort a $325 bottle's economics equally,
    which isn't how the money actually worked out.

    runout is projected from your observed wear rate, so it only appears
    once there's enough history for the rate to mean anything.
    """
    live = [c for c in containers if not c["is_finished"]]
    priced = [c for c in containers if c["purchase_price"] and c["size_ml"]]

    total_spent = sum(c["purchase_price"] for c in containers if c["purchase_price"]) or None
    ml_remaining = sum(c["ml_remaining"] for c in live if c["ml_remaining"]) or None
    wears_remaining = sum(c["wears_remaining"] for c in live if c["wears_remaining"]) or None

    cost_per_wear = None
    if priced:
        total_sprays = sum(c["size_ml"] * SPRAYS_PER_ML for c in priced)
        if total_sprays:
            cost_per_wear = (sum(c["purchase_price"] for c in priced) / total_sprays) * SPRAYS_PER_WEAR

    runout_days = None
    if wears_remaining and wear_count >= 3 and first_worn_at:
        try:
            first = datetime.strptime(first_worn_at.split(".")[0], "%Y-%m-%d %H:%M:%S")
            days_tracked = max((datetime.utcnow() - first).days, 1)
            wears_per_day = wear_count / days_tracked
            if wears_per_day > 0:
                runout_days = int(wears_remaining / wears_per_day)
        except (ValueError, TypeError):
            runout_days = None

    return {
        "container_count": len(containers),
        "live_container_count": len(live),
        "total_spent": total_spent,
        "ml_remaining": round(ml_remaining, 1) if ml_remaining else None,
        "wears_remaining": wears_remaining,
        "cost_per_wear": cost_per_wear,
        "runout_days": runout_days,
    }


def _humanize_runout(days):
    """'runs out in about 8 months'-style text. Deliberately vague past a
    few weeks — the underlying wear-rate estimate isn't precise enough to
    justify 'in 247 days'."""
    if days is None:
        return None
    if days < 14:
        return f"about {days} day{'s' if days != 1 else ''}"
    if days < 60:
        return f"about {days // 7} weeks"
    if days < 730:
        return f"about {max(days // 30, 2)} months"
    return f"over {days // 365} years"


def get_fragrance_full(fragrance_id):
    db = get_db()
    f = db.execute("SELECT * FROM fragrances WHERE id = ?", (fragrance_id,)).fetchone()
    if f is None:
        return None
    frag = dict(f)

    seasons = db.execute(
        "SELECT season FROM seasons WHERE fragrance_id = ?", (fragrance_id,)
    ).fetchall()
    frag["seasons"] = [s["season"] for s in seasons]

    notes = db.execute(
        "SELECT tier, note_text FROM notes WHERE fragrance_id = ? ORDER BY tier, position",
        (fragrance_id,),
    ).fetchall()

    note_keys = {n["note_text"].strip().lower() for n in notes}
    image_map = {}
    if note_keys:
        placeholders = ",".join("?" for _ in note_keys)
        image_rows = db.execute(
            f"SELECT note_key, image_filename FROM note_images WHERE note_key IN ({placeholders})",
            tuple(note_keys),
        ).fetchall()
        image_map = {r["note_key"]: r["image_filename"] for r in image_rows}

    frag["notes"] = {"top": [], "middle": [], "base": []}
    for n in notes:
        key = n["note_text"].strip().lower()
        frag["notes"][n["tier"]].append({"text": n["note_text"], "image": image_map.get(key)})

    tags = db.execute(
        """
        SELECT t.name FROM tags t
        JOIN fragrance_tags ft ON ft.tag_id = t.id
        WHERE ft.fragrance_id = ?
        ORDER BY t.name COLLATE NOCASE
        """,
        (fragrance_id,),
    ).fetchall()
    frag["tags"] = [t["name"] for t in tags]

    shelves = db.execute(
        """
        SELECT s.id, s.name, s.icon, s.is_private FROM shelves s
        JOIN shelf_fragrances sf ON sf.shelf_id = s.id
        WHERE sf.fragrance_id = ?
        ORDER BY s.name COLLATE NOCASE
        """,
        (fragrance_id,),
    ).fetchall()
    frag["shelves"] = [dict(s) for s in shelves]

    wear_rows = db.execute(
        "SELECT id, worn_at FROM wear_log WHERE fragrance_id = ? ORDER BY worn_at DESC",
        (fragrance_id,),
    ).fetchall()
    frag["wear_log"] = [dict(w) for w in wear_rows]
    for entry in frag["wear_log"]:
        entry["worn_at_display"] = _utc_to_local_display(entry["worn_at"])
    frag["wear_count"] = len(frag["wear_log"])
    frag["last_worn"] = frag["wear_log"][0]["worn_at"] if frag["wear_log"] else None
    frag["last_worn_display"] = _humanize_days_ago(frag["last_worn"])
    first_worn = frag["wear_log"][-1]["worn_at"] if frag["wear_log"] else None

    frag["containers"] = get_containers(fragrance_id)
    frag["economics"] = _container_economics(frag["containers"], frag["wear_count"], first_worn)
    frag["cost_per_wear"] = frag["economics"]["cost_per_wear"]
    frag["runout_text"] = _humanize_runout(frag["economics"]["runout_days"])

    frag["accords"] = [
        dict(a) for a in db.execute(
            """
            SELECT a.name, fa.strength
            FROM fragrance_accords fa JOIN accords a ON a.id = fa.accord_id
            WHERE fa.fragrance_id = ?
            ORDER BY fa.position, fa.strength DESC
            """,
            (fragrance_id,),
        ).fetchall()
    ]

    return frag


def get_tag_counts():
    db = get_db()
    rows = db.execute(
        """
        SELECT t.name AS tag,
               COUNT(DISTINCT CASE WHEN f.is_wishlist = 0 THEN ft.fragrance_id END) AS cnt
        FROM tags t
        LEFT JOIN fragrance_tags ft ON ft.tag_id = t.id
        LEFT JOIN fragrances f ON f.id = ft.fragrance_id
        GROUP BY t.id
        ORDER BY t.name COLLATE NOCASE
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_search_term_counts():
    """The search page's filter cloud: real tags AND every note in the
    collection, unified into one searchable term list. A tag and a note that
    share a name (case-insensitive) — e.g. tag "Vanilla" on one fragrance,
    note "vanilla" on another — collapse into a single term rather than
    double-counting a fragrance that matches both, but is_tag/is_note are
    tracked independently so the UI can still show what kind(s) of thing
    it is (a term can legitimately be both). Each entry carries the real
    tag's id when there is one (only those get a delete button in the UI;
    a note isn't a deletable row the way a tag is)."""
    db = get_db()

    tag_rows = db.execute("SELECT id, name FROM tags").fetchall()
    tag_id_by_key = {row["name"].strip().lower(): row["id"] for row in tag_rows}

    rows = db.execute(
        """
        SELECT f.id AS fragrance_id,
               GROUP_CONCAT(DISTINCT t.name) AS tag_list,
               GROUP_CONCAT(DISTINCT n.note_text) AS note_list
        FROM fragrances f
        LEFT JOIN fragrance_tags ft ON ft.fragrance_id = f.id
        LEFT JOIN tags t ON t.id = ft.tag_id
        LEFT JOIN notes n ON n.fragrance_id = f.id
        WHERE f.is_wishlist = 0
        GROUP BY f.id
        """
    ).fetchall()

    # lowercase key -> {"display": str, "ids": set(), "tag_id": int|None, "is_tag": bool, "is_note": bool}
    terms = {}

    def _touch(text, fragrance_id, source):
        text = (text or "").strip()
        if not text:
            return
        key = text.lower()
        if key not in terms:
            terms[key] = {"display": text, "ids": set(), "tag_id": tag_id_by_key.get(key), "is_tag": False, "is_note": False}
        terms[key]["ids"].add(fragrance_id)
        terms[key][source] = True

    for row in rows:
        if row["tag_list"]:
            for text in row["tag_list"].split(","):
                _touch(text, row["fragrance_id"], "is_tag")
        if row["note_list"]:
            for text in row["note_list"].split(","):
                _touch(text, row["fragrance_id"], "is_note")

    # Tags currently attached to nothing (e.g. mid-edit, before the orphan
    # sweep runs) still show up so they're reachable to delete by hand,
    # rather than silently vanishing from view.
    for row in tag_rows:
        key = row["name"].strip().lower()
        if key not in terms:
            terms[key] = {"display": row["name"], "ids": set(), "tag_id": row["id"], "is_tag": True, "is_note": False}

    result = [
        {
            "term": v["display"], "cnt": len(v["ids"]), "tag_id": v["tag_id"],
            "is_tag": v["is_tag"], "is_note": v["is_note"],
        }
        for v in terms.values()
    ]
    return sorted(result, key=lambda r: r["term"].lower())


# ---------------------------------------------------------------------------
# Stats page — everything here is a live query, so the page is always current
# as of the moment it's loaded (no caching to invalidate).
# ---------------------------------------------------------------------------
def _with_bar_pct(rows):
    """Adds a 0-100 'pct' to each row, relative to the largest count in the set,
    so bar widths are proportional to the biggest bar rather than an absolute scale."""
    max_cnt = max((r["cnt"] for r in rows), default=0)
    for r in rows:
        r["pct"] = round((r["cnt"] / max_cnt * 100), 1) if max_cnt else 0
    return rows


def stats_summary():
    db = get_db()
    total_bottles = db.execute("SELECT COUNT(*) AS c FROM fragrances WHERE is_wishlist = 0").fetchone()["c"]
    total_houses = db.execute("SELECT COUNT(DISTINCT brand) AS c FROM fragrances WHERE is_wishlist = 0").fetchone()["c"]
    # "Tags in use" — tags actually attached to >=1 non-wishlist fragrance.
    total_tags = db.execute(
        """
        SELECT COUNT(DISTINCT ft.tag_id) AS c
        FROM fragrance_tags ft
        JOIN fragrances f ON f.id = ft.fragrance_id
        WHERE f.is_wishlist = 0
        """
    ).fetchone()["c"]
    # Case/whitespace-insensitive: "Vanilla" and "vanilla " count as one note.
    total_unique_notes = db.execute(
        """
        SELECT COUNT(DISTINCT LOWER(TRIM(n.note_text))) AS c
        FROM notes n
        JOIN fragrances f ON f.id = n.fragrance_id
        WHERE f.is_wishlist = 0
        """
    ).fetchone()["c"]
    spend_row = db.execute(
        """
        SELECT COUNT(c.purchase_price) AS purchased_count, SUM(c.purchase_price) AS total_spent
        FROM containers c JOIN fragrances f ON f.id = c.fragrance_id
        WHERE f.is_wishlist = 0
        """
    ).fetchone()
    rating_row = db.execute(
        "SELECT COUNT(rating) AS rated_count, AVG(rating) AS avg_rating "
        "FROM fragrances WHERE is_wishlist = 0"
    ).fetchone()
    return {
        "total_bottles": total_bottles,
        "total_houses": total_houses,
        "total_tags": total_tags,
        "total_unique_notes": total_unique_notes,
        "purchased_count": spend_row["purchased_count"],
        "total_spent": round(spend_row["total_spent"], 2) if spend_row["total_spent"] is not None else None,
        "rated_count": rating_row["rated_count"],
        "avg_rating": round(rating_row["avg_rating"], 1) if rating_row["avg_rating"] is not None else None,
    }


def stats_top_notes(limit=5):
    db = get_db()
    rows = db.execute(
        """
        SELECT MIN(n.note_text) AS label, COUNT(DISTINCT n.fragrance_id) AS cnt,
               MAX(ni.image_filename) AS image
        FROM notes n
        JOIN fragrances f ON f.id = n.fragrance_id AND f.is_wishlist = 0
        LEFT JOIN note_images ni ON ni.note_key = LOWER(TRIM(n.note_text))
        GROUP BY LOWER(TRIM(n.note_text))
        ORDER BY cnt DESC, label ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return _with_bar_pct([dict(r) for r in rows])


def stats_top_tags(limit=5):
    ranked = sorted(get_tag_counts(), key=lambda r: (-r["cnt"], r["tag"]))
    top = [{"label": r["tag"], "cnt": r["cnt"]} for r in ranked if r["cnt"] > 0][:limit]
    return _with_bar_pct(top)


def stats_season_distribution():
    db = get_db()
    rows = db.execute(
        """
        SELECT s.season, COUNT(DISTINCT s.fragrance_id) AS cnt
        FROM seasons s
        JOIN fragrances f ON f.id = s.fragrance_id AND f.is_wishlist = 0
        GROUP BY s.season
        """
    ).fetchall()
    counts = {r["season"]: r["cnt"] for r in rows}
    ordered = [{"label": s, "cnt": counts.get(s, 0)} for s in SEASON_CHOICES]
    return _with_bar_pct(ordered)


def stats_top_houses(limit=5):
    db = get_db()
    rows = db.execute(
        "SELECT brand AS label, COUNT(*) AS cnt FROM fragrances WHERE is_wishlist = 0 GROUP BY brand ORDER BY cnt DESC, brand ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return _with_bar_pct([dict(r) for r in rows])


_MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def stats_by_occasion(limit=8):
    """What you actually reach for, by occasion."""
    db = get_db()
    rows = db.execute(
        """
        SELECT occasion AS label, COUNT(*) AS cnt
        FROM wear_log WHERE occasion IS NOT NULL AND TRIM(occasion) != ''
        GROUP BY LOWER(occasion) ORDER BY cnt DESC, label ASC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return _with_bar_pct([dict(r) for r in rows])


def stats_compliment_leaders(limit=5):
    """Which bottles actually earn compliments — rate, not raw count, so a
    fragrance worn twice to great effect isn't buried under a daily driver.
    Needs at least 2 logged wears before a rate means anything."""
    db = get_db()
    rows = db.execute(
        """
        SELECT f.id, (f.brand || ' ' || f.name) AS label,
               COUNT(w.id) AS wears, SUM(w.complimented) AS compliments
        FROM fragrances f JOIN wear_log w ON w.fragrance_id = f.id
        GROUP BY f.id HAVING wears >= 2 AND compliments > 0
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["rate"] = d["compliments"] / d["wears"] * 100
        d["cnt"] = d["compliments"]
        out.append(d)
    out.sort(key=lambda x: (-x["rate"], -x["compliments"]))
    return _with_bar_pct(out[:limit])


def stats_by_temperature(limit=5):
    """Cold-weather versus warm-weather picks, from the temperatures you
    actually logged rather than the season checkboxes you set once."""
    db = get_db()
    rows = db.execute(
        """
        SELECT (f.brand || ' ' || f.name) AS label, f.id,
               AVG(w.weather_temp_f) AS avg_temp, COUNT(*) AS cnt
        FROM fragrances f JOIN wear_log w ON w.fragrance_id = f.id
        WHERE w.weather_temp_f IS NOT NULL
        GROUP BY f.id HAVING cnt >= 2
        """
    ).fetchall()
    data = [dict(r) for r in rows]
    coldest = sorted(data, key=lambda x: x["avg_temp"])[:limit]
    warmest = sorted(data, key=lambda x: -x["avg_temp"])[:limit]
    return coldest, warmest


def stats_running_low(threshold=15, limit=8):
    """Containers close to empty, so a favourite doesn't run out unnoticed."""
    db = get_db()
    rows = db.execute(
        """
        SELECT c.id, c.fill_level, c.container_type, c.size_ml,
               f.id AS fragrance_id, (f.brand || ' ' || f.name) AS label
        FROM containers c JOIN fragrances f ON f.id = c.fragrance_id
        WHERE c.is_finished = 0 AND c.fill_level <= ? AND f.is_wishlist = 0
        ORDER BY c.fill_level ASC LIMIT ?
        """,
        (threshold, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def stats_timeline(months=12):
    """How the collection grew, by month, oldest to newest.

    Counts *containers*, keyed on when each was acquired, not when the
    fragrance row was created. Those differ in two cases that both matter:
    a wishlist item added in January and actually bought in June belongs in
    June, and a second bottle or a decant of something you already own is a
    real acquisition that a per-fragrance count would miss entirely.

    purchase_date wins when you've recorded one, since that's the real
    acquisition date; otherwise it falls back to when the container was
    created here.

    Always returns exactly `months` calendar-month buckets ending this
    month, zero-filled where nothing was added — a real gap should show as
    an empty bar, not be silently skipped and make the timeline look
    compressed.
    """
    db = get_db()
    now = datetime.now()
    buckets = []
    y, m = now.year, now.month
    for _ in range(months):
        buckets.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    buckets.reverse()

    rows = db.execute(
        """
        SELECT strftime('%Y-%m', COALESCE(c.purchase_date, c.created_at)) AS ym,
               COUNT(*) AS cnt
        FROM containers c JOIN fragrances f ON f.id = c.fragrance_id
        WHERE f.is_wishlist = 0
        GROUP BY ym
        """
    ).fetchall()
    counts_by_ym = {r["ym"]: r["cnt"] for r in rows}

    result = []
    for ym in buckets:
        year, month = ym.split("-")
        result.append({"label": f"{_MONTH_NAMES[int(month)]} '{year[2:]}", "cnt": counts_by_ym.get(ym, 0)})
    return _with_bar_pct(result)


def stats_most_worn(limit=5):
    db = get_db()
    rows = db.execute(
        """
        SELECT f.id, (f.brand || ' ' || f.name) AS label, COUNT(w.id) AS cnt
        FROM fragrances f
        JOIN wear_log w ON w.fragrance_id = f.id
        WHERE f.is_wishlist = 0
        GROUP BY f.id
        ORDER BY cnt DESC, label COLLATE NOCASE
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return _with_bar_pct([dict(r) for r in rows])


def stats_needs_love(limit=8, stale_days=90):
    """Currently-owned fragrances that either have never been logged as
    worn, or haven't been in stale_days+ — a nudge to actually use what you
    have, not just catalog it."""
    db = get_db()
    rows = db.execute(
        """
        SELECT f.id, f.brand, f.name, MAX(w.worn_at) AS last_worn
        FROM fragrances f
        LEFT JOIN wear_log w ON w.fragrance_id = f.id
        WHERE f.is_wishlist = 0 AND f.currently_owned = 1
        GROUP BY f.id
        HAVING last_worn IS NULL OR last_worn <= datetime('now', ?)
        ORDER BY (last_worn IS NULL) DESC, last_worn ASC
        LIMIT ?
        """,
        (f"-{stale_days} days", limit),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["last_worn_display"] = _humanize_days_ago(d["last_worn"]) if d["last_worn"] else "never"
        result.append(d)
    return result


def stats_best_value(limit=5):
    """Top N by total value actually gotten out of wearing it — wear count
    times the blended cost per wear across every container of that
    fragrance. Blended (total spent / total sprays bought) for the same
    reason the detail page is: averaging per-container figures would let a
    cheap sample distort an expensive bottle's economics equally.

    Needs at least one container with both a size and a price; wear count
    alone isn't enough, since a fragrance with no money attached can't
    have a dollar value attached to its wears."""
    db = get_db()
    rows = db.execute(
        """
        SELECT f.id, f.brand, f.name,
               SUM(c.purchase_price) AS total_price,
               SUM(c.size_ml) AS total_ml,
               (SELECT COUNT(*) FROM wear_log w WHERE w.fragrance_id = f.id) AS wear_count
        FROM fragrances f
        JOIN containers c ON c.fragrance_id = f.id
        WHERE f.is_wishlist = 0
          AND c.purchase_price IS NOT NULL AND c.size_ml IS NOT NULL
        GROUP BY f.id
        HAVING wear_count > 0
        """
    ).fetchall()
    result = []
    for r in rows:
        total_sprays = (r["total_ml"] or 0) * SPRAYS_PER_ML
        if not total_sprays or not r["total_price"]:
            continue
        cost_per_wear = (r["total_price"] / total_sprays) * SPRAYS_PER_WEAR
        result.append({
            "id": r["id"], "brand": r["brand"], "name": r["name"],
            "wear_count": r["wear_count"], "cost_per_wear": cost_per_wear,
            "amount_worn": cost_per_wear * r["wear_count"],
        })
    result.sort(key=lambda x: x["amount_worn"], reverse=True)
    return result[:limit]


def stats_price_extremes():
    """Most/least expensive by what you actually paid, summed across every
    container of that fragrance — not the Fragrantica reference price.
    Matches "Total Spent" on the same basis. Two 5ml decants at $24 each
    count as $48 invested in that fragrance, which is the honest figure.

    Wishlist items are excluded (nothing's been paid for those yet).
    Returns (most_expensive, cheapest), either possibly None."""
    db = get_db()

    def _extreme(direction):
        return db.execute(
            f"""
            SELECT f.id, f.brand, f.name, f.image_filename,
                   SUM(c.purchase_price) AS purchase_price
            FROM fragrances f JOIN containers c ON c.fragrance_id = f.id
            WHERE c.purchase_price IS NOT NULL AND f.is_wishlist = 0
            GROUP BY f.id
            ORDER BY purchase_price {direction}, f.name ASC
            LIMIT 1
            """
        ).fetchone()

    most_expensive = _extreme("DESC")
    cheapest = _extreme("ASC")
    return (
        dict(most_expensive) if most_expensive else None,
        dict(cheapest) if cheapest else None,
    )


def stats_daynight_donut():
    db = get_db()
    rows = db.execute(
        "SELECT daynight, COUNT(*) AS cnt FROM fragrances "
        "WHERE TRIM(COALESCE(daynight, '')) != '' AND is_wishlist = 0 GROUP BY daynight"
    ).fetchall()
    counts = {r["daynight"]: r["cnt"] for r in rows}
    colors = {"Day": "var(--sapphire-bright)", "Night": "var(--gold)", "Both": "var(--cyan)"}
    total = sum(counts.get(k, 0) for k in DAYNIGHT_CHOICES)

    segments = []
    cursor = 0.0
    for label in DAYNIGHT_CHOICES:
        cnt = counts.get(label, 0)
        if cnt == 0 or total == 0:
            continue
        pct = cnt / total * 100
        segments.append({
            "label": label, "cnt": cnt, "color": colors[label],
            "start": round(cursor, 2), "end": round(cursor + pct, 2),
        })
        cursor += pct

    if segments:
        gradient_css = "conic-gradient(" + ", ".join(
            f"{s['color']} {s['start']}% {s['end']}%" for s in segments
        ) + ")"
    else:
        gradient_css = "conic-gradient(var(--line-strong) 0% 100%)"

    return segments, gradient_css, total


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


_rembg_session = None


def _get_rembg_session():
    """Lazily creates (and caches) the rembg inference session. Lazy so that if
    rembg isn't installed, the app still starts fine and only fails at the point
    background removal is actually attempted — which is itself non-fatal (see
    strip_background)."""
    global _rembg_session
    if _rembg_session is None:
        from rembg import new_session
        _rembg_session = new_session("u2netp")
    return _rembg_session


def strip_background(img):
    """Removes the background from a bottle photo via rembg (a local ONNX model —
    no external API call, nothing leaves the machine). Returns an RGBA image on
    success, or None on any failure (rembg not installed, model error, etc.) —
    background removal is a nice-to-have, never something that should block
    saving a photo. Callers fall back to the image unchanged when this is None."""
    try:
        from rembg import remove
        result = remove(img, session=_get_rembg_session())
        return result.convert("RGBA")
    except Exception as e:
        app.logger.warning("Background removal failed, keeping original image: %s", e)
        return None


def _resize_and_store(pil_image, max_width=None, remove_bg=False):
    """Shared resize/save logic for uploaded files, imported bottle photos, and note icons.
    Keeps the source format instead of flattening everything to JPEG — PNG (and its
    transparency, if any) stays PNG, WEBP stays WEBP. Only formats without alpha
    support (JPEG, or anything Pillow can't otherwise identify) get converted to
    RGB JPEG, which was already lossy going in.

    remove_bg=True runs the image through rembg first and forces PNG output
    regardless of the source format, since the whole point is the new
    transparency. Only ever set for bottle photos, never note icons."""
    max_width = max_width or MAX_IMAGE_WIDTH
    fmt = (pil_image.format or "").upper()
    img = pil_image

    if img.width > max_width:
        ratio = max_width / float(img.width)
        img = img.resize((max_width, int(img.height * ratio)))

    if remove_bg:
        stripped = strip_background(img)
        if stripped is not None:
            img = stripped
            fmt = "PNG"
        # else: rembg unavailable/failed — fall through and save as normal,
        # in whatever format was already detected.

    if fmt == "PNG":
        filename = f"{uuid.uuid4().hex}.png"
        dest = Path(UPLOAD_FOLDER) / filename
        if img.mode == "P":
            # Palette images: expand to RGBA if they carry transparency, else RGB.
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        img.save(dest, "PNG", optimize=True)
    elif fmt == "WEBP":
        filename = f"{uuid.uuid4().hex}.webp"
        dest = Path(UPLOAD_FOLDER) / filename
        img.save(dest, "WEBP", quality=85)
    else:
        filename = f"{uuid.uuid4().hex}.jpg"
        dest = Path(UPLOAD_FOLDER) / filename
        img.convert("RGB").save(dest, "JPEG", quality=85, optimize=True)

    return filename


def save_image(file_storage):
    """Resize + save an uploaded image, return the stored filename. Background
    removal runs automatically on every upload."""
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        flash("Image must be a PNG, JPG, or WEBP file.", "error")
        return None
    img = Image.open(file_storage.stream)
    return _resize_and_store(img, remove_bg=True)


# ---------------------------------------------------------------------------
# Fragrantica bookmarklet import: the bookmarklet extracts fields from a page
# the user is already viewing in their own browser (no server-side scraping,
# no automated crawling of Fragrantica). The only server-side network calls
# this feature makes are fetching image URLs the user's own browser pointed
# at (the bottle photo, and small note icons) — same as if they'd downloaded
# and uploaded those images themselves.
# ---------------------------------------------------------------------------
MAX_REMOTE_IMAGE_BYTES = 8 * 1024 * 1024
NOTE_IMAGE_MAX_WIDTH = 120
MAX_NOTE_IMAGES_PER_SUBMIT = 15


def _normalize_fragrantica_url(url):
    """Normalizes a Fragrantica URL for duplicate-detection comparison — drops
    query string/fragment (tracking params, etc.) and trailing slash, lowercases
    the whole thing. The name on a fragrance can drift from what Fragrantica
    calls it (you renamed it locally), but the URL of the page you imported it
    from doesn't change, which makes it a much more reliable duplicate check."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if not parsed.scheme or not parsed.netloc:
            return None
        path = parsed.path.rstrip("/")
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path.lower()}"
    except Exception:
        return None


def _is_safe_remote_url(url):
    """Basic SSRF guard: only allow http(s) to public, non-local addresses."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def fetch_remote_image_filename(url, max_width=None, remove_bg=False):
    """Download one specific image URL (from the bookmarklet payload), resize, and store it.
    Returns a filename on success, or None on any failure — image import is a
    nice-to-have and never blocks saving the fragrance either way. Failures
    are logged (not silent) so a broken auto-import shows up in the
    container logs with an actual reason, rather than just mysteriously not
    working. A common one: Fragrantica sits behind Cloudflare, and a
    server-side fetch like this — no real browser, no JS execution — is
    exactly the kind of request Cloudflare's bot protection is built to
    catch, sometimes intermittently. That shows up here as an HTTP 403."""
    if not url:
        return None
    if not _is_safe_remote_url(url):
        print(f"WARNING: image auto-import blocked by the SSRF guard for URL: {url}")
        return None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; ScentLedger personal image import)"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                print(f"WARNING: image auto-import got a non-image response (Content-Type: '{content_type}') for URL: {url}")
                return None
            data = resp.read(MAX_REMOTE_IMAGE_BYTES + 1)
            if len(data) > MAX_REMOTE_IMAGE_BYTES:
                print(f"WARNING: image auto-import response exceeded the {MAX_REMOTE_IMAGE_BYTES}-byte limit for URL: {url}")
                return None
        img = Image.open(io.BytesIO(data))
        return _resize_and_store(img, max_width=max_width, remove_bg=remove_bg)
    except urllib.error.HTTPError as e:
        print(f"WARNING: image auto-import got HTTP {e.code} ({e.reason}) for URL: {url}")
        return None
    except urllib.error.URLError as e:
        print(f"WARNING: image auto-import couldn't connect ({e.reason}) for URL: {url}")
        return None
    except (OSError, ValueError) as e:
        print(f"WARNING: image auto-import downloaded data it couldn't process ({e}) for URL: {url}")
        return None


def cache_note_images(db, notes_images):
    """Given {note_name: image_url} from a bookmarklet import, downloads and stores
    an icon for any note we don't already have cached, keyed by normalized note name.
    Silently skips failures — this is a nice-to-have, never something that blocks saving."""
    if not isinstance(notes_images, dict):
        return
    attempts = 0
    for note_name, url in notes_images.items():
        if attempts >= MAX_NOTE_IMAGES_PER_SUBMIT:
            break
        if not isinstance(note_name, str) or not isinstance(url, str):
            continue
        key = note_name.strip().lower()
        if not key:
            continue
        already_cached = db.execute(
            "SELECT 1 FROM note_images WHERE note_key = ?", (key,)
        ).fetchone()
        if already_cached:
            continue
        attempts += 1
        filename = fetch_remote_image_filename(url, max_width=NOTE_IMAGE_MAX_WIDTH)
        if filename:
            db.execute(
                "INSERT OR IGNORE INTO note_images (note_key, image_filename) VALUES (?, ?)",
                (key, filename),
            )


BOOKMARKLET_JS_PATH = BASE_DIR / "static" / "fragrantica_bookmarklet.js"


@functools.lru_cache(maxsize=8)
def build_bookmarklet_href(add_url):
    """Reads the extraction script and inlines it as a javascript: bookmarklet URI,
    pointed at this deployment's own /add URL. Cached per add_url — the file and
    the minification are both deterministic, so there's nothing to redo on every
    request. Bounded cache size since in practice there's realistically only ever
    one or two distinct add_urls (e.g. different hostnames pointing at the same
    instance), not because we expect to actually need many."""
    with open(BOOKMARKLET_JS_PATH, "r") as f:
        src = f.read()
    # Strip `//` line comments BEFORE substituting the URL — the URL itself
    # contains "//" (http://...), which must not be treated as a comment start.
    src = "\n".join(line.split("//")[0] if "//" in line else line for line in src.split("\n"))
    src = src.replace("__ADD_URL__", add_url)
    minified = re.sub(r"\s+", " ", src).strip()
    return "javascript:" + minified


def delete_image(filename):
    if not filename:
        return
    path = Path(UPLOAD_FOLDER) / filename
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def parse_comma_list(raw):
    if not raw:
        return []
    seen = []
    for part in raw.split(","):
        val = part.strip()
        if val and val not in seen:
            seen.append(val)
    return seen


def upsert_tags(db, fragrance_id, tag_names):
    db.execute("DELETE FROM fragrance_tags WHERE fragrance_id = ?", (fragrance_id,))
    for name in tag_names:
        db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_row = db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        db.execute(
            "INSERT OR IGNORE INTO fragrance_tags (fragrance_id, tag_id) VALUES (?, ?)",
            (fragrance_id, tag_row["id"]),
        )


def _prune_orphaned_tags(db):
    """Removes any tag no longer attached to any fragrance. Called after
    editing a fragrance's tags (a save can remove the last use of a tag) and
    after deleting a fragrance outright — keeps the tag list from silently
    accumulating unused entries over time instead of requiring manual
    cleanup for every edit."""
    db.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM fragrance_tags)")


def upsert_accords(db, fragrance_id, accord_entries):
    """Accord entries arrive as plain names, optionally suffixed with a
    strength as 'woody:82'. Order is preserved via `position` because
    accords are ranked by prominence, not alphabetical."""
    db.execute("DELETE FROM fragrance_accords WHERE fragrance_id = ?", (fragrance_id,))
    for i, entry in enumerate(accord_entries):
        name, _, strength_raw = entry.partition(":")
        name = name.strip().lower()
        if not name:
            continue
        strength = _parse_optional_float(strength_raw)
        db.execute("INSERT OR IGNORE INTO accords (name) VALUES (?)", (name,))
        row = db.execute("SELECT id FROM accords WHERE name = ?", (name,)).fetchone()
        db.execute(
            "INSERT OR IGNORE INTO fragrance_accords (fragrance_id, accord_id, strength, position) "
            "VALUES (?, ?, ?, ?)",
            (fragrance_id, row["id"], strength, i),
        )


def replace_seasons(db, fragrance_id, seasons):
    db.execute("DELETE FROM seasons WHERE fragrance_id = ?", (fragrance_id,))
    for s in seasons:
        if s in SEASON_CHOICES:
            db.execute(
                "INSERT INTO seasons (fragrance_id, season) VALUES (?, ?)",
                (fragrance_id, s),
            )


def replace_notes(db, fragrance_id, notes_dict):
    db.execute("DELETE FROM notes WHERE fragrance_id = ?", (fragrance_id,))
    for tier in ("top", "middle", "base"):
        for i, text in enumerate(notes_dict.get(tier, [])):
            db.execute(
                "INSERT INTO notes (fragrance_id, tier, note_text, position) VALUES (?, ?, ?, ?)",
                (fragrance_id, tier, text, i),
            )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.route("/")
def home():
    fragrances = get_all_fragrances_light(respect_owned_filter=True)
    return render_template("index.html", fragrances=fragrances)


def _safe_next_url(next_url):
    """Only redirect to same-site relative paths — never to an absolute or
    protocol-relative URL, which would make `next` an open-redirect vector."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return url_for("home")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("logged_in"):
            return redirect(_safe_next_url(request.args.get("next", "")))
        return render_template("login.html", next=request.args.get("next", ""))

    username = request.form.get("username", "")
    password = request.form.get("password", "")
    next_url = request.form.get("next", "")

    valid = (
        hmac.compare_digest(username, ADMIN_USERNAME)
        and hmac.compare_digest(password, ADMIN_PASSWORD)
    )
    if valid:
        session.clear()
        session["logged_in"] = True
        session.permanent = True
        flash("Logged in.", "success")
        return redirect(_safe_next_url(next_url))

    flash("Invalid username or password.", "error")
    return render_template("login.html", next=next_url), 401


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("home"))


@app.route("/fragrance/<int:fragrance_id>")
def detail(fragrance_id):
    frag = get_fragrance_full(fragrance_id)
    if frag is None:
        abort(404)
    addable_shelves = []
    all_shelves_count = 0
    if session.get("logged_in"):
        db = get_db()
        on_shelf_ids = {s["id"] for s in frag["shelves"]}
        rows = db.execute("SELECT id, name, icon, is_private FROM shelves ORDER BY name COLLATE NOCASE").fetchall()
        all_shelves_count = len(rows)
        addable_shelves = [dict(r) for r in rows if r["id"] not in on_shelf_ids]
    return render_template(
        "detail.html", f=frag, current_id=fragrance_id,
        addable_shelves=addable_shelves, all_shelves_count=all_shelves_count,
        similar_fragrances=_find_similar_fragrances(fragrance_id),
    )


@app.route("/add", methods=["GET", "POST"])
@login_required
def add():
    if request.method == "GET":
        bookmarklet_href = build_bookmarklet_href(url_for("add", _external=True))
        prefill_wishlist = request.args.get("wishlist") == "1"
        return render_template(
            "form.html", mode="add", f=None, bookmarklet_href=bookmarklet_href,
            prefill_wishlist=prefill_wishlist,
        )

    return _handle_form_submit(mode="add", fragrance_id=None)


@app.route("/edit/<int:fragrance_id>", methods=["GET", "POST"])
@login_required
def edit(fragrance_id):
    frag = get_fragrance_full(fragrance_id)
    if frag is None:
        abort(404)

    if request.method == "GET":
        return render_template("form.html", mode="edit", f=frag)

    return _handle_form_submit(mode="edit", fragrance_id=fragrance_id, existing=frag)


@app.route("/api/lookup")
@login_required
def api_lookup():
    """Used by the Fragrantica import flow: before filling a blank Add form, the
    page checks whether a fragrance already exists, so a re-import can be
    routed to /edit instead of creating a duplicate. Checks the Fragrantica
    URL first — that doesn't change even if you've renamed the fragrance
    locally — and falls back to brand+name for older entries that don't have
    a URL stored yet."""
    url = request.args.get("url", "").strip()
    brand = request.args.get("brand", "").strip()
    name = request.args.get("name", "").strip()
    db = get_db()

    normalized = _normalize_fragrantica_url(url)
    if normalized:
        rows = db.execute(
            "SELECT id, fragrantica_url FROM fragrances WHERE fragrantica_url IS NOT NULL"
        ).fetchall()
        for r in rows:
            if _normalize_fragrantica_url(r["fragrantica_url"]) == normalized:
                return {"found": True, "id": r["id"], "matched_by": "url"}

    if brand and name:
        row = db.execute(
            "SELECT id FROM fragrances WHERE LOWER(TRIM(brand)) = LOWER(TRIM(?)) AND LOWER(TRIM(name)) = LOWER(TRIM(?)) LIMIT 1",
            (brand, name),
        ).fetchone()
        if row:
            return {"found": True, "id": row["id"], "matched_by": "name"}

    return {"found": False}


def _handle_form_submit(mode, fragrance_id, existing=None):
    brand = request.form.get("brand", "").strip()
    name = request.form.get("name", "").strip()
    subname = request.form.get("subname", "").strip()
    description = request.form.get("description", "").strip()
    daynight = request.form.get("daynight", "").strip()
    seasons = request.form.getlist("seasons")
    tags = parse_comma_list(request.form.get("tags_csv", ""))
    notes = {
        "top": parse_comma_list(request.form.get("notes_top_csv", "")),
        "middle": parse_comma_list(request.form.get("notes_middle_csv", "")),
        "base": parse_comma_list(request.form.get("notes_base_csv", "")),
    }

    def _parse_price(raw):
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            val = round(float(raw), 2)
            return val if val >= 0 else None
        except ValueError:
            return None

    price = _parse_price(request.form.get("price", ""))
    purchase_price = _parse_price(request.form.get("purchase_price", ""))
    is_gift = bool(request.form.get("is_gift"))
    is_discontinued = bool(request.form.get("is_discontinued"))
    is_wishlist = bool(request.form.get("is_wishlist"))
    if is_gift:
        # A gift has no purchase price by definition — enforced here too, not
        # just by the form disabling the field, in case that's ever bypassed.
        purchase_price = None

    currently_owned = bool(request.form.get("currently_owned"))
    gave_away = bool(request.form.get("gave_away"))
    if currently_owned:
        # Can't have given it away while still owning it — enforced here too,
        # not just by the form's own logic, same pattern as gift/purchase_price.
        gave_away = False

    bottles_owned_raw = (request.form.get("bottles_owned", "") or "").strip()
    try:
        bottles_owned = int(bottles_owned_raw) if bottles_owned_raw else 1
        if bottles_owned < 0:
            bottles_owned = 1
    except ValueError:
        bottles_owned = 1

    fragrantica_url = request.form.get("fragrantica_url", "").strip() or None
    private_notes = request.form.get("private_notes", "").strip() or None

    rating_raw = request.form.get("rating", "").strip()
    try:
        rating = int(rating_raw) if rating_raw else None
        if rating is not None and not (1 <= rating <= 5):
            rating = None
    except ValueError:
        rating = None

    def _perf_rating(field):
        v = _parse_optional_int(request.form.get(field))
        return v if (v is not None and 1 <= v <= 5) else None

    longevity_rating = _perf_rating("longevity_rating")
    sillage_rating = _perf_rating("sillage_rating")
    projection_rating = _perf_rating("projection_rating")

    fill_level_raw = request.form.get("fill_level", "").strip()
    try:
        fill_level = int(fill_level_raw) if fill_level_raw else 100
        if not (0 <= fill_level <= 100):
            fill_level = 100
    except ValueError:
        fill_level = 100

    size_ml_raw = request.form.get("size_ml", "").strip()
    try:
        size_ml = int(size_ml_raw) if size_ml_raw else None
        if size_ml is not None and size_ml <= 0:
            size_ml = None
    except ValueError:
        size_ml = None

    if not brand or not name:
        flash("Brand and Name are required.", "error")
        stub = existing or {}
        stub.update(
            brand=brand, name=name, subname=subname, description=description,
            daynight=daynight, seasons=seasons, tags=tags, notes=notes,
            price=price, purchase_price=purchase_price,
            is_gift=is_gift, is_discontinued=is_discontinued, is_wishlist=is_wishlist,
            currently_owned=currently_owned, gave_away=gave_away, bottles_owned=bottles_owned,
            fragrantica_url=fragrantica_url, rating=rating, fill_level=fill_level,
            private_notes=private_notes, size_ml=size_ml,
        )
        return render_template("form.html", mode=mode, f=stub), 400

    db = get_db()
    image_filename = existing["image_filename"] if existing else None
    uploaded = request.files.get("image")
    image_url = request.form.get("image_url", "").strip()

    if uploaded and uploaded.filename:
        new_filename = save_image(uploaded)
        if new_filename:
            if image_filename:
                delete_image(image_filename)
            image_filename = new_filename
    elif image_url and not image_filename:
        # Only used when nothing was uploaded and there's no existing photo —
        # e.g. right after a Fragrantica import. Fails silently if the fetch
        # doesn't work out; the user can always upload manually instead.
        new_filename = fetch_remote_image_filename(image_url, remove_bg=True)
        if new_filename:
            image_filename = new_filename
        else:
            flash("Couldn't auto-import the photo — feel free to upload it manually.", "error")

    possible_dup = _find_possible_duplicate(
        db, brand, name, exclude_id=(fragrance_id if mode == "edit" else None)
    )
    if possible_dup:
        flash(
            f'You already have "{possible_dup["brand"]} {possible_dup["name"]}" in your '
            f'collection — this might be a duplicate. Saved anyway; edit or delete '
            f'whichever one was the mistake, if any.',
            "warning",
        )

    if mode == "add":
        cur = db.execute(
            """
            INSERT INTO fragrances
                (brand, name, subname, image_filename, description, daynight,
                 price, is_gift, is_discontinued, is_wishlist,
                 currently_owned, gave_away, bottles_owned, fragrantica_url,
                 rating, private_notes,
                 longevity_rating, sillage_rating, projection_rating)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (brand, name, subname, image_filename, description, daynight,
             price, is_gift, is_discontinued, is_wishlist,
             currently_owned, gave_away, bottles_owned, fragrantica_url,
             rating, private_notes,
             longevity_rating, sillage_rating, projection_rating),
        )
        fragrance_id = cur.lastrowid
    else:
        db.execute(
            """
            UPDATE fragrances
            SET brand = ?, name = ?, subname = ?, image_filename = ?, description = ?, daynight = ?,
                price = ?, is_gift = ?, is_discontinued = ?, is_wishlist = ?,
                currently_owned = ?, gave_away = ?, bottles_owned = ?, fragrantica_url = ?,
                rating = ?, private_notes = ?,
                longevity_rating = ?, sillage_rating = ?, projection_rating = ?
            WHERE id = ?
            """,
            (brand, name, subname, image_filename, description, daynight,
             price, is_gift, is_discontinued, is_wishlist,
             currently_owned, gave_away, bottles_owned, fragrantica_url,
             rating, private_notes,
             longevity_rating, sillage_rating, projection_rating, fragrance_id),
        )

    replace_seasons(db, fragrance_id, seasons)
    replace_notes(db, fragrance_id, notes)
    upsert_tags(db, fragrance_id, tags)
    _prune_orphaned_tags(db)
    upsert_accords(db, fragrance_id, parse_comma_list(request.form.get("accords_csv", "")))

    # A fragrance you own should always have at least one container: that's
    # where size, fill, price, purchase date and batch code live, and without
    # one it has no stash, no cost-per-wear, and never appears in the
    # timeline. Adding one directly seeds it from the form. Moving one off
    # the wishlist has to seed it here instead, because the Edit form
    # deliberately hides those fields (they'd be ambiguous once a fragrance
    # can hold several containers), so there's nothing to copy from — the
    # container starts bare and gets filled in from Your Stash.
    if not is_wishlist:
        has_container = db.execute(
            "SELECT 1 FROM containers WHERE fragrance_id = ? LIMIT 1", (fragrance_id,)
        ).fetchone()
        if not has_container:
            db.execute(
                """
                INSERT INTO containers
                    (fragrance_id, container_type, size_ml, fill_level, purchase_price)
                VALUES (?, 'bottle', ?, ?, ?)
                """,
                (fragrance_id, size_ml, fill_level, purchase_price),
            )
            if mode == "edit":
                flash(
                    "Moved into your collection — add the bottle's size and price "
                    "under Your Stash to get cost per wear.",
                    "success",
                )

    notes_images_raw = request.form.get("notes_images_json", "").strip()
    if notes_images_raw:
        try:
            cache_note_images(db, json.loads(notes_images_raw))
        except (ValueError, TypeError):
            pass  # malformed payload — never let this block saving the fragrance

    db.commit()

    flash(f'Saved "{name}".', "success")
    return redirect(url_for("detail", fragrance_id=fragrance_id))


@app.route("/fragrance/<int:fragrance_id>/delete", methods=["POST"])
@login_required
def delete(fragrance_id):
    db = get_db()
    frag = db.execute("SELECT image_filename FROM fragrances WHERE id = ?", (fragrance_id,)).fetchone()
    if frag is None:
        abort(404)
    delete_image(frag["image_filename"])
    db.execute("DELETE FROM fragrances WHERE id = ?", (fragrance_id,))
    _prune_orphaned_tags(db)
    db.commit()
    flash("Fragrance removed.", "success")
    return redirect(url_for("home"))


@app.route("/tags/<int:tag_id>/delete", methods=["POST"])
@login_required
def delete_tag(tag_id):
    db = get_db()
    row = db.execute("SELECT name FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))  # cascades to fragrance_tags
    db.commit()
    flash(f'Removed tag "{row["name"]}".', "success")
    return redirect(_safe_next_url(request.form.get("next", "")))


@app.route("/notes")
@login_required
def notes_library():
    """Every distinct note used anywhere in the collection, with its cached
    icon if it has one. Notes missing an icon sort first, since finding and
    filling those in is the actual point of this page. Fragrantica's own
    note database doesn't cover everything — this is where you fill the
    gaps in by hand, from a photo or a URL you found elsewhere."""
    db = get_db()
    rows = db.execute(
        """
        SELECT LOWER(TRIM(n.note_text)) AS note_key,
               MIN(n.note_text) AS display_name,
               COUNT(DISTINCT n.fragrance_id) AS cnt,
               MAX(ni.image_filename) AS image_filename
        FROM notes n
        LEFT JOIN note_images ni ON ni.note_key = LOWER(TRIM(n.note_text))
        GROUP BY LOWER(TRIM(n.note_text))
        ORDER BY (MAX(ni.image_filename) IS NOT NULL), MIN(n.note_text) COLLATE NOCASE
        """
    ).fetchall()
    notes = [dict(r) for r in rows]
    missing_count = sum(1 for n in notes if not n["image_filename"])
    return render_template("notes.html", notes=notes, missing_count=missing_count)


@app.route("/notes/upload", methods=["POST"])
@login_required
def upload_note_image():
    """Sets (or replaces) the icon for one note, by upload or by URL. Unlike
    bottle photos, these don't get automatic background removal — they're
    already small, cropped icons whether Fragrantica supplied them or you
    did."""
    note_key = request.form.get("note_key", "").strip().lower()
    if not note_key:
        abort(400)

    db = get_db()
    existing = db.execute(
        "SELECT image_filename FROM note_images WHERE note_key = ?", (note_key,)
    ).fetchone()

    uploaded = request.files.get("image")
    url = request.form.get("image_url", "").strip()
    filename = None

    if uploaded and uploaded.filename:
        if allowed_file(uploaded.filename):
            img = Image.open(uploaded.stream)
            filename = _resize_and_store(img, max_width=NOTE_IMAGE_MAX_WIDTH)
        else:
            flash("Image must be a PNG, JPG, or WEBP file.", "error")
            return redirect(url_for("notes_library"))
    elif url:
        filename = fetch_remote_image_filename(url, max_width=NOTE_IMAGE_MAX_WIDTH)
        if not filename:
            flash("Couldn't fetch an image from that URL.", "error")
            return redirect(url_for("notes_library"))
    else:
        flash("Provide a file or a URL.", "error")
        return redirect(url_for("notes_library"))

    # Only remove the old file once the new one is safely saved — a failed
    # fetch/upload above already returned before this point, so the note
    # never ends up with neither.
    db.execute(
        "INSERT INTO note_images (note_key, image_filename) VALUES (?, ?) "
        "ON CONFLICT(note_key) DO UPDATE SET image_filename = excluded.image_filename",
        (note_key, filename),
    )
    db.commit()
    if existing and existing["image_filename"] and existing["image_filename"] != filename:
        delete_image(existing["image_filename"])

    flash(f'{"Replaced" if existing else "Added"} the image for "{note_key}".', "success")
    return redirect(url_for("notes_library"))


@app.route("/notes/merge", methods=["POST"])
@login_required
def merge_note():
    """Merges one note into another everywhere it's used — e.g. "Ceylonese
    Cinnamon" and "Cinnamon" being the same thing under two different
    spellings. The source note disappears; every fragrance that had it now
    has the target note's text instead. If that leaves a fragrance with the
    same note twice in one tier (it had both spellings there already), the
    duplicate is dropped rather than showing the same note twice in the
    pyramid. Icons are handled without losing a good one: if only the
    source note had an icon, the target adopts it; otherwise the source's
    icon (now orphaned) is removed."""
    source_key = request.form.get("source_key", "").strip().lower()
    target_key = request.form.get("target_key", "").strip().lower()
    if not source_key or not target_key or source_key == target_key:
        flash("Pick two different notes to merge.", "error")
        return redirect(url_for("notes_library"))

    db = get_db()
    source_row = db.execute(
        "SELECT note_text FROM notes WHERE LOWER(TRIM(note_text)) = ? LIMIT 1", (source_key,)
    ).fetchone()
    target_row = db.execute(
        "SELECT note_text FROM notes WHERE LOWER(TRIM(note_text)) = ? LIMIT 1", (target_key,)
    ).fetchone()
    if source_row is None or target_row is None:
        flash("Couldn't find one of those notes.", "error")
        return redirect(url_for("notes_library"))
    source_display, target_text = source_row["note_text"], target_row["note_text"]

    db.execute(
        "UPDATE notes SET note_text = ? WHERE LOWER(TRIM(note_text)) = ?",
        (target_text, source_key),
    )
    # A fragrance that listed both spellings in the same tier now has the
    # same note twice there — keep just one.
    db.execute(
        """
        DELETE FROM notes
        WHERE id NOT IN (
            SELECT MIN(id) FROM notes GROUP BY fragrance_id, tier, LOWER(TRIM(note_text))
        )
        """
    )

    target_icon = db.execute("SELECT image_filename FROM note_images WHERE note_key = ?", (target_key,)).fetchone()
    source_icon = db.execute("SELECT image_filename FROM note_images WHERE note_key = ?", (source_key,)).fetchone()
    if source_icon:
        if target_icon:
            delete_image(source_icon["image_filename"])
            db.execute("DELETE FROM note_images WHERE note_key = ?", (source_key,))
        else:
            db.execute("UPDATE note_images SET note_key = ? WHERE note_key = ?", (target_key, source_key))

    db.commit()
    flash(f'Merged "{source_display}" into "{target_text}".', "success")
    return redirect(url_for("notes_library"))


@app.route("/shelves")
def shelves_list():
    db = get_db()
    query = """
        SELECT s.id, s.name, s.icon, s.is_private, COUNT(sf.fragrance_id) AS cnt
        FROM shelves s
        LEFT JOIN shelf_fragrances sf ON sf.shelf_id = s.id
    """
    if not session.get("logged_in"):
        query += " WHERE s.is_private = 0"
    query += " GROUP BY s.id ORDER BY s.name COLLATE NOCASE"
    rows = db.execute(query).fetchall()
    shelves = [dict(r) for r in rows]
    return render_template("shelves.html", shelves=shelves)


def _validate_fa_icon(raw):
    """A Font Awesome icon is 1-3 space-separated classes like "fa-solid
    fa-heart" — validates the shape rather than checking against a real
    icon list (there's no local copy of Font Awesome's icon metadata to
    check against), so a typo just means no icon shows, not a rejected
    shelf. Never blocks creating/saving a shelf either way."""
    raw = (raw or "").strip()
    if not raw:
        return None
    tokens = raw.split()
    if not (1 <= len(tokens) <= 3):
        return None
    for t in tokens:
        if not re.fullmatch(r"fa-[a-z0-9-]+", t):
            return None
    return " ".join(tokens)


@app.route("/shelves/add", methods=["GET", "POST"])
@login_required
def add_shelf():
    if request.method == "GET":
        return render_template("shelf_form.html")
    name = request.form.get("name", "").strip()
    icon = _validate_fa_icon(request.form.get("icon", ""))
    is_private = bool(request.form.get("is_private"))
    if not name:
        flash("Shelf name is required.", "error")
        return render_template("shelf_form.html", name=name, icon=icon, is_private=is_private), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO shelves (name, icon, is_private) VALUES (?, ?, ?)", (name, icon, is_private)
    )
    db.commit()
    flash(f'Created shelf "{name}".', "success")
    return redirect(url_for("shelf_detail", shelf_id=cur.lastrowid))


@app.route("/shelves/<int:shelf_id>")
def shelf_detail(shelf_id):
    db = get_db()
    shelf = db.execute("SELECT id, name, icon, is_private FROM shelves WHERE id = ?", (shelf_id,)).fetchone()
    if shelf is None:
        abort(404)
    if shelf["is_private"] and not session.get("logged_in"):
        # Same response as "doesn't exist" — a private shelf's existence
        # isn't revealed to a visitor who isn't allowed to see it.
        abort(404)
    on_shelf_rows = db.execute(
        """
        SELECT f.id, f.brand, f.name, f.subname, f.image_filename
        FROM fragrances f
        JOIN shelf_fragrances sf ON sf.fragrance_id = f.id
        WHERE sf.shelf_id = ?
        ORDER BY f.brand COLLATE NOCASE, f.name COLLATE NOCASE
        """,
        (shelf_id,),
    ).fetchall()
    fragrances = [dict(r) for r in on_shelf_rows]
    on_shelf_ids = {r["id"] for r in on_shelf_rows}
    all_grouped = get_all_fragrances_grouped_by_brand() if session.get("logged_in") else []
    return render_template(
        "shelf_detail.html", shelf=shelf, fragrances=fragrances,
        all_grouped=all_grouped, on_shelf_ids=on_shelf_ids,
    )


@app.route("/shelves/<int:shelf_id>/toggle-private", methods=["POST"])
@login_required
def toggle_shelf_private(shelf_id):
    db = get_db()
    shelf = db.execute("SELECT is_private FROM shelves WHERE id = ?", (shelf_id,)).fetchone()
    if shelf is None:
        abort(404)
    db.execute(
        "UPDATE shelves SET is_private = ? WHERE id = ?",
        (0 if shelf["is_private"] else 1, shelf_id),
    )
    db.commit()
    return redirect(url_for("shelf_detail", shelf_id=shelf_id))


@app.route("/bulk/add-to-shelf", methods=["POST"])
@login_required
def bulk_add_to_shelf():
    shelf_id = request.form.get("shelf_id", type=int)
    fragrance_ids = request.form.getlist("fragrance_ids", type=int)
    next_url = _safe_next_url(request.form.get("next", ""))
    if not shelf_id or not fragrance_ids:
        flash("Pick a shelf and select at least one fragrance first.", "error")
        return redirect(next_url)
    db = get_db()
    shelf = db.execute("SELECT name FROM shelves WHERE id = ?", (shelf_id,)).fetchone()
    if shelf is None:
        abort(404)
    for fid in fragrance_ids:
        db.execute(
            "INSERT OR IGNORE INTO shelf_fragrances (shelf_id, fragrance_id) VALUES (?, ?)",
            (shelf_id, fid),
        )
    db.commit()
    flash(f'Added {len(fragrance_ids)} fragrance(s) to "{shelf["name"]}".', "success")
    return redirect(next_url)


@app.route("/bulk/add-tag", methods=["POST"])
@login_required
def bulk_add_tag():
    tag_name = request.form.get("tag_name", "").strip()
    fragrance_ids = request.form.getlist("fragrance_ids", type=int)
    next_url = _safe_next_url(request.form.get("next", ""))
    if not tag_name or not fragrance_ids:
        flash("Enter a tag and select at least one fragrance first.", "error")
        return redirect(next_url)
    db = get_db()
    db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
    tag_row = db.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
    for fid in fragrance_ids:
        db.execute(
            "INSERT OR IGNORE INTO fragrance_tags (fragrance_id, tag_id) VALUES (?, ?)",
            (fid, tag_row["id"]),
        )
    db.commit()
    flash(f'Tagged {len(fragrance_ids)} fragrance(s) with "{tag_name}".', "success")
    return redirect(next_url)


@app.route("/compare")
def compare():
    ids_param = request.args.get("ids", "")
    try:
        ids = [int(x) for x in ids_param.split(",") if x.strip()][:3]
    except ValueError:
        ids = []
    if len(ids) < 2:
        flash("Select 2-3 fragrances to compare.", "error")
        return redirect(url_for("home"))
    fragrances = [f for f in (get_fragrance_full(fid) for fid in ids) if f is not None]
    if len(fragrances) < 2:
        flash("Couldn't find enough of those fragrances to compare.", "error")
        return redirect(url_for("home"))
    return render_template("compare.html", fragrances=fragrances)


@app.route("/shelves/<int:shelf_id>/toggle/<int:fragrance_id>", methods=["POST"])
@login_required
def toggle_shelf_fragrance(shelf_id, fragrance_id):
    db = get_db()
    shelf = db.execute("SELECT id FROM shelves WHERE id = ?", (shelf_id,)).fetchone()
    frag = db.execute("SELECT id FROM fragrances WHERE id = ?", (fragrance_id,)).fetchone()
    if shelf is None or frag is None:
        abort(404)
    existing = db.execute(
        "SELECT 1 FROM shelf_fragrances WHERE shelf_id = ? AND fragrance_id = ?",
        (shelf_id, fragrance_id),
    ).fetchone()
    if existing:
        db.execute(
            "DELETE FROM shelf_fragrances WHERE shelf_id = ? AND fragrance_id = ?",
            (shelf_id, fragrance_id),
        )
    else:
        db.execute(
            "INSERT INTO shelf_fragrances (shelf_id, fragrance_id) VALUES (?, ?)",
            (shelf_id, fragrance_id),
        )
    db.commit()
    # Defaults to the shelf's own page (the original behavior, from the
    # shelf's add/remove list) — but honors an explicit same-site "next"
    # so this can also be triggered from a fragrance's own page and land
    # back there instead.
    next_param = request.form.get("next", "").strip()
    if next_param and next_param.startswith("/") and not next_param.startswith("//"):
        return redirect(next_param)
    return redirect(url_for("shelf_detail", shelf_id=shelf_id))


@app.route("/shelves/<int:shelf_id>/delete", methods=["POST"])
@login_required
def delete_shelf(shelf_id):
    db = get_db()
    shelf = db.execute("SELECT name FROM shelves WHERE id = ?", (shelf_id,)).fetchone()
    if shelf is None:
        abort(404)
    db.execute("DELETE FROM shelves WHERE id = ?", (shelf_id,))  # cascades to shelf_fragrances
    db.commit()
    flash(f'Deleted shelf "{shelf["name"]}".', "success")
    return redirect(url_for("shelves_list"))


@app.route("/fragrance/<int:fragrance_id>/wear", methods=["POST"])
@login_required
def log_wear(fragrance_id):
    db = get_db()
    frag = db.execute("SELECT id FROM fragrances WHERE id = ?", (fragrance_id,)).fetchone()
    if frag is None:
        abort(404)

    # Every field here is optional — posting the bare form (the one-click
    # "I Wore This Today" button) still works exactly as it did before.
    occasion = (request.form.get("occasion", "") or "").strip() or None
    weather_summary = (request.form.get("weather_summary", "") or "").strip() or None
    wear_note = (request.form.get("wear_note", "") or "").strip() or None
    complimented = 1 if request.form.get("complimented") else 0
    container_id = _parse_optional_int(request.form.get("container_id"))
    sprays = _parse_optional_int(request.form.get("sprays"))
    temp_f = _parse_optional_float(request.form.get("weather_temp_f"))
    longevity_hours = _parse_optional_float(request.form.get("longevity_hours"))

    # Only accept a container that actually belongs to this fragrance —
    # a hand-crafted POST shouldn't be able to drain someone else's bottle.
    if container_id is not None:
        owns = db.execute(
            "SELECT id, size_ml, fill_level FROM containers WHERE id = ? AND fragrance_id = ?",
            (container_id, fragrance_id),
        ).fetchone()
        if owns is None:
            container_id = None

    db.execute(
        """
        INSERT INTO wear_log
            (fragrance_id, container_id, occasion, weather_temp_f, weather_summary,
             sprays, longevity_hours, complimented, wear_note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (fragrance_id, container_id, occasion, temp_f, weather_summary,
         sprays, longevity_hours, complimented, wear_note),
    )

    depleted_msg = ""
    if container_id is not None:
        depleted_msg = _decrement_container_fill(db, container_id, sprays or SPRAYS_PER_WEAR)

    _set_todays_fragrance(fragrance_id)
    db.commit()
    flash("Logged today's wear." + depleted_msg, "success")
    return redirect(url_for("detail", fragrance_id=fragrance_id))


def _parse_optional_int(raw):
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_optional_float(raw):
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _decrement_container_fill(db, container_id, sprays):
    """Close the loop: wearing from a container actually uses it up.

    Only meaningful when the container has a known size — without one
    there's no way to turn sprays into a percentage. Clamps at 0 and
    auto-marks the container finished when it empties, returning a short
    string to append to the flash message so the user finds out at the
    moment it happens rather than noticing later.
    """
    row = db.execute(
        "SELECT size_ml, fill_level, is_finished FROM containers WHERE id = ?", (container_id,)
    ).fetchone()
    if row is None or not row["size_ml"] or row["is_finished"]:
        return ""

    total_sprays = row["size_ml"] * SPRAYS_PER_ML
    if total_sprays <= 0:
        return ""

    pct_used = (sprays / total_sprays) * 100.0
    new_fill = max(0, round(row["fill_level"] - pct_used))

    if new_fill <= 0:
        db.execute("UPDATE containers SET fill_level = 0, is_finished = 1 WHERE id = ?", (container_id,))
        return " That container is now empty — marked as finished."
    db.execute("UPDATE containers SET fill_level = ? WHERE id = ?", (new_fill, container_id))
    return ""


def _container_form_values(form):
    ctype = (form.get("container_type", "") or "bottle").strip()
    if ctype not in CONTAINER_TYPES:
        ctype = "bottle"
    fill = _parse_optional_int(form.get("fill_level"))
    if fill is None or not (0 <= fill <= 100):
        fill = 100
    return {
        "container_type": ctype,
        "size_ml": _parse_optional_float(form.get("size_ml")),
        "fill_level": fill,
        "purchase_price": _parse_optional_float(form.get("purchase_price")),
        "purchase_date": (form.get("purchase_date", "") or "").strip() or None,
        "batch_code": (form.get("batch_code", "") or "").strip() or None,
        "label": (form.get("label", "") or "").strip() or None,
        "is_finished": 1 if form.get("is_finished") else 0,
    }


@app.route("/fragrance/<int:fragrance_id>/containers/add", methods=["POST"])
@login_required
def add_container(fragrance_id):
    db = get_db()
    if db.execute("SELECT id FROM fragrances WHERE id = ?", (fragrance_id,)).fetchone() is None:
        abort(404)
    v = _container_form_values(request.form)
    db.execute(
        """
        INSERT INTO containers
            (fragrance_id, container_type, size_ml, fill_level, purchase_price,
             purchase_date, batch_code, label, is_finished)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (fragrance_id, v["container_type"], v["size_ml"], v["fill_level"], v["purchase_price"],
         v["purchase_date"], v["batch_code"], v["label"], v["is_finished"]),
    )
    db.commit()
    flash(f"Added a {CONTAINER_TYPE_LABELS[v['container_type']].lower()}.", "success")
    return redirect(url_for("detail", fragrance_id=fragrance_id))


@app.route("/containers/<int:container_id>/edit", methods=["POST"])
@login_required
def edit_container(container_id):
    db = get_db()
    row = db.execute("SELECT fragrance_id FROM containers WHERE id = ?", (container_id,)).fetchone()
    if row is None:
        abort(404)
    v = _container_form_values(request.form)
    db.execute(
        """
        UPDATE containers
        SET container_type = ?, size_ml = ?, fill_level = ?, purchase_price = ?,
            purchase_date = ?, batch_code = ?, label = ?, is_finished = ?
        WHERE id = ?
        """,
        (v["container_type"], v["size_ml"], v["fill_level"], v["purchase_price"],
         v["purchase_date"], v["batch_code"], v["label"], v["is_finished"], container_id),
    )
    db.commit()
    flash("Container updated.", "success")
    return redirect(url_for("detail", fragrance_id=row["fragrance_id"]))


@app.route("/containers/<int:container_id>/delete", methods=["POST"])
@login_required
def delete_container(container_id):
    db = get_db()
    row = db.execute("SELECT fragrance_id FROM containers WHERE id = ?", (container_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("DELETE FROM containers WHERE id = ?", (container_id,))
    db.commit()
    flash("Container removed. Its wear history was kept.", "success")
    return redirect(url_for("detail", fragrance_id=row["fragrance_id"]))


@app.route("/wear-log/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_wear_entry(entry_id):
    db = get_db()
    entry = db.execute("SELECT fragrance_id FROM wear_log WHERE id = ?", (entry_id,)).fetchone()
    if entry is None:
        abort(404)
    db.execute("DELETE FROM wear_log WHERE id = ?", (entry_id,))
    db.commit()
    return redirect(url_for("detail", fragrance_id=entry["fragrance_id"]))


# Best-effort Southern-Hemisphere IANA zone identifiers, for guessing season
# correctly regardless of where the TZ setting points. Not exhaustive down to
# every small territory, and inherently fuzzy near the equator (e.g. "season"
# doesn't mean much in Singapore either way) — but far better than always
# assuming Northern, which was the only option before TZ was configurable.
# Australia/* and Antarctica/* are handled separately below since every zone
# in those two regions is Southern.
_SOUTHERN_HEMISPHERE_ZONES = frozenset({
    "Pacific/Auckland", "Pacific/Chatham", "Pacific/Fiji", "Pacific/Tongatapu",
    "Pacific/Noumea", "Pacific/Port_Moresby", "Pacific/Guadalcanal", "Pacific/Efate",
    "Pacific/Apia", "Pacific/Easter", "Pacific/Pitcairn", "Pacific/Norfolk",
    "Pacific/Marquesas", "Pacific/Tahiti", "Pacific/Rarotonga", "Pacific/Wallis",
    "Pacific/Funafuti", "Pacific/Nauru", "Pacific/Galapagos", "Pacific/Pago_Pago",
    "Pacific/Niue",
    "Indian/Antananarivo", "Indian/Mauritius", "Indian/Reunion", "Indian/Comoro",
    "Indian/Mayotte", "Indian/Chagos", "Indian/Kerguelen", "Indian/Christmas",
    "Indian/Cocos",
    "Africa/Johannesburg", "Africa/Maputo", "Africa/Windhoek", "Africa/Gaborone",
    "Africa/Harare", "Africa/Lusaka", "Africa/Maseru", "Africa/Mbabane",
    "Africa/Blantyre", "Africa/Lubumbashi", "Africa/Luanda", "Africa/Kinshasa",
    "America/Sao_Paulo", "America/Argentina/Buenos_Aires", "America/Argentina/Cordoba",
    "America/Argentina/Salta", "America/Argentina/Jujuy", "America/Argentina/Tucuman",
    "America/Argentina/Catamarca", "America/Argentina/La_Rioja",
    "America/Argentina/San_Juan", "America/Argentina/Mendoza",
    "America/Argentina/San_Luis", "America/Argentina/Rio_Gallegos",
    "America/Argentina/Ushuaia", "America/Santiago", "America/Punta_Arenas",
    "America/Asuncion", "America/Montevideo", "America/La_Paz", "America/Lima",
    "America/Bahia", "America/Recife", "America/Fortaleza", "America/Cuiaba",
    "America/Campo_Grande", "America/Porto_Velho", "America/Rio_Branco",
    "America/Noronha", "America/Belem", "America/Araguaina", "America/Maceio",
    "America/Santarem",
    "Atlantic/South_Georgia", "Atlantic/Stanley",
})


def _is_southern_hemisphere(tz_name):
    return tz_name.startswith("Australia/") or tz_name.startswith("Antarctica/") \
        or tz_name in _SOUTHERN_HEMISPHERE_ZONES


def _current_season_and_daynight():
    """Meteorological season and day/night, using the configured TZ (see
    the TZ environment variable / app_settings 'timezone') for the actual
    current local time rather than the server's own clock — and a
    best-effort hemisphere guess from that same zone name, rather than
    always assuming Northern. Falls back to UTC if the configured value
    isn't a valid zone name."""
    tz_name = _get_setting("timezone", "Etc/UTC") or "Etc/UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz_name, tz = "Etc/UTC", ZoneInfo("UTC")

    now = datetime.now(tz)
    month, hour = now.month, now.hour

    if month in (3, 4, 5):
        season = "Fall" if _is_southern_hemisphere(tz_name) else "Spring"
    elif month in (6, 7, 8):
        season = "Winter" if _is_southern_hemisphere(tz_name) else "Summer"
    elif month in (9, 10, 11):
        season = "Spring" if _is_southern_hemisphere(tz_name) else "Fall"
    else:
        season = "Summer" if _is_southern_hemisphere(tz_name) else "Winter"

    daynight = "Day" if 6 <= hour < 18 else "Night"
    return season, daynight


def _pick_random_fragrance():
    """Picks a fragrance from what's currently owned, weighted toward the
    current season/day-night when possible, falling back to any owned
    fragrance if nothing matches (or if none has season/day-night set at
    all). Returns (fragrance_id_or_None, season, daynight)."""
    db = get_db()
    season, daynight = _current_season_and_daynight()
    weighted_rows = db.execute(
        """
        SELECT DISTINCT f.id
        FROM fragrances f
        JOIN seasons s ON s.fragrance_id = f.id
        WHERE f.is_wishlist = 0 AND f.currently_owned = 1 AND s.season = ?
          AND (f.daynight = ? OR f.daynight = 'Both' OR f.daynight IS NULL OR f.daynight = '')
        """,
        (season, daynight),
    ).fetchall()
    candidates = [r["id"] for r in weighted_rows]
    if not candidates:
        rows = db.execute(
            "SELECT id FROM fragrances WHERE is_wishlist = 0 AND currently_owned = 1"
        ).fetchall()
        candidates = [r["id"] for r in rows]
    if not candidates:
        return None, season, daynight
    return random.choice(candidates), season, daynight


@app.route("/random")
def random_pick():
    fragrance_id, season, daynight = _pick_random_fragrance()
    if fragrance_id is None:
        flash("Nothing currently owned to pick from.", "error")
        return redirect(url_for("home"))
    flash(f"Today's pick — weighted for {season}, {daynight.lower()}.", "success")
    return redirect(url_for("detail", fragrance_id=fragrance_id))


@app.route("/export/csv")
@login_required
def export_csv():
    db = get_db()
    fragrances = db.execute(
        "SELECT * FROM fragrances ORDER BY brand COLLATE NOCASE, name COLLATE NOCASE"
    ).fetchall()

    # Batched lookups (one query each, not one per fragrance) — mirrors the
    # GROUP_CONCAT/JOIN pattern get_all_fragrances_light() already uses.
    SEP = chr(0x1F)  # ASCII unit separator: won't collide with real note/tag text

    seasons_by_fid = {}
    for r in db.execute(
        """
        SELECT fragrance_id, season FROM seasons
        ORDER BY fragrance_id,
                 CASE season WHEN 'Spring' THEN 0 WHEN 'Summer' THEN 1 WHEN 'Fall' THEN 2 WHEN 'Winter' THEN 3 ELSE 4 END
        """
    ).fetchall():
        seasons_by_fid.setdefault(r["fragrance_id"], []).append(r["season"])

    notes_by_fid = {}
    for r in db.execute(
        f"""
        SELECT fragrance_id, tier, GROUP_CONCAT(note_text, '{SEP}') AS notes_concat
        FROM (SELECT fragrance_id, tier, note_text FROM notes ORDER BY fragrance_id, tier, position)
        GROUP BY fragrance_id, tier
        """
    ).fetchall():
        notes_by_fid.setdefault(r["fragrance_id"], {})[r["tier"]] = r["notes_concat"].split(SEP)

    tags_by_fid = {}
    for r in db.execute(
        f"""
        SELECT ft.fragrance_id AS fragrance_id, GROUP_CONCAT(t.name, '{SEP}') AS tags_concat
        FROM fragrance_tags ft JOIN tags t ON t.id = ft.tag_id
        GROUP BY ft.fragrance_id
        """
    ).fetchall():
        tags_by_fid[r["fragrance_id"]] = r["tags_concat"].split(SEP)

    # Containers own size/fill/price now, so the CSV reports the rollup:
    # total paid across every container, total ml, and a weighted average
    # fill across the ones still in use.
    containers_by_fid = {}
    for r in db.execute(
        """
        SELECT fragrance_id,
               SUM(purchase_price) AS total_paid,
               SUM(size_ml) AS total_ml,
               COUNT(*) AS container_count,
               AVG(CASE WHEN is_finished = 0 THEN fill_level END) AS avg_fill
        FROM containers GROUP BY fragrance_id
        """
    ).fetchall():
        containers_by_fid[r["fragrance_id"]] = dict(r)

    wear_by_fid = {
        r["fragrance_id"]: (r["wear_count"], r["last_worn"])
        for r in db.execute(
            "SELECT fragrance_id, COUNT(*) AS wear_count, MAX(worn_at) AS last_worn FROM wear_log GROUP BY fragrance_id"
        ).fetchall()
    }

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Brand", "Name", "Subname", "Description", "Day/Night", "Seasons",
        "Top Notes", "Middle Notes", "Base Notes", "Tags",
        "Current Price", "Purchase Price", "Rating", "Fill Level %",
        "Currently Owned", "Bottles Owned", "Is Gift", "Is Discontinued",
        "Gave Away", "Is Wishlist", "Fragrantica URL", "Wear Count", "Last Worn",
    ])
    for f in fragrances:
        fid = f["id"]
        seasons = seasons_by_fid.get(fid, [])
        notes_by_tier = notes_by_fid.get(fid, {})
        tags = tags_by_fid.get(fid, [])
        wear_count, last_worn = wear_by_fid.get(fid, (0, None))
        cont = containers_by_fid.get(fid, {})
        writer.writerow([
            f["brand"], f["name"], f["subname"] or "", f["description"] or "", f["daynight"] or "",
            ", ".join(seasons),
            "; ".join(notes_by_tier.get("top", [])),
            "; ".join(notes_by_tier.get("middle", [])),
            "; ".join(notes_by_tier.get("base", [])),
            ", ".join(tags),
            f["price"] if f["price"] is not None else "",
            cont.get("total_paid") if cont.get("total_paid") is not None else "",
            f["rating"] if f["rating"] is not None else "",
            round(cont["avg_fill"]) if cont.get("avg_fill") is not None else "",
            "Yes" if f["currently_owned"] else "No",
            f["bottles_owned"],
            "Yes" if f["is_gift"] else "No",
            "Yes" if f["is_discontinued"] else "No",
            "Yes" if f["gave_away"] else "No",
            "Yes" if f["is_wishlist"] else "No",
            f["fragrantica_url"] or "",
            wear_count,
            _utc_to_local_display(last_worn) if last_worn else "",
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=scent_ledger_export.csv"},
    )


@app.route("/wishlist")
def wishlist():
    fragrances = get_all_fragrances_light(wishlist=True)
    return render_template("wishlist.html", fragrances=fragrances)


@app.route("/toggle-owned-filter")
def toggle_owned_filter():
    """Flips the sidebar's "owned only" display preference. Session-based, so
    it works for any visitor (not gated behind admin login) and persists
    across navigation without needing a query param on every link. Only
    affects the sidebar's brand list — home, search, and stats always show
    the full (non-wishlist) collection regardless."""
    session["sidebar_owned_only"] = not session.get("sidebar_owned_only", False)
    return redirect(_safe_next_url(request.args.get("next", "")))


@app.route("/search")
def search():
    q = request.args.get("q", "").strip().lower()
    tags_param = request.args.get("tags", "")
    active_tags = [t for t in tags_param.split(",") if t]
    active_seasons = [s for s in request.args.getlist("season") if s in SEASON_CHOICES]
    active_daynight = request.args.get("daynight", "").strip()
    price_min_raw = request.args.get("price_min", "").strip()
    price_max_raw = request.args.get("price_max", "").strip()

    fragrances = get_all_fragrances_light()

    if q:
        fragrances = [
            f for f in fragrances
            if q in f["brand"].lower()
            or q in f["name"].lower()
            or q in (f["subname"] or "").lower()
            or any(q in t.lower() for t in f["tags"])
        ]

    if active_tags:
        active_tags_lower = [t.lower() for t in active_tags]
        fragrances = [
            f for f in fragrances
            if all(t in [x.lower() for x in f["tags"]] for t in active_tags_lower)
        ]

    if active_seasons:
        # OR across seasons — a fragrance usually has one or two, requiring
        # every checked season at once would rarely match anything.
        fragrances = [f for f in fragrances if any(s in f["seasons"] for s in active_seasons)]

    if active_daynight in DAYNIGHT_CHOICES:
        # "Both" on the fragrance counts as a match for either Day or Night.
        fragrances = [
            f for f in fragrances
            if f["daynight"] == active_daynight or f["daynight"] == "Both"
        ]

    price_min = None
    price_max = None
    try:
        if price_min_raw:
            price_min = float(price_min_raw)
    except ValueError:
        price_min = None
    try:
        if price_max_raw:
            price_max = float(price_max_raw)
    except ValueError:
        price_max = None
    if price_min is not None:
        fragrances = [f for f in fragrances if f["price"] is not None and f["price"] >= price_min]
    if price_max is not None:
        fragrances = [f for f in fragrances if f["price"] is not None and f["price"] <= price_max]

    search_terms = get_search_term_counts()

    return render_template(
        "search.html",
        fragrances=fragrances,
        q=q,
        active_tags=active_tags,
        search_terms=search_terms,
        active_seasons=active_seasons,
        active_daynight=active_daynight,
        price_min=price_min_raw,
        price_max=price_max_raw,
        season_choices=SEASON_CHOICES,
        daynight_choices=DAYNIGHT_CHOICES,
    )


@app.route("/stats")
def stats():
    summary = stats_summary()
    daynight_segments, daynight_gradient, daynight_total = stats_daynight_donut()
    most_expensive, cheapest = stats_price_extremes()
    _temp_split = stats_by_temperature(5)
    return render_template(
        "stats.html",
        **summary,
        top_notes=stats_top_notes(5),
        top_tags=stats_top_tags(5),
        season_dist=stats_season_distribution(),
        top_houses=stats_top_houses(5),
        daynight_segments=daynight_segments,
        daynight_gradient=daynight_gradient,
        daynight_total=daynight_total,
        most_expensive=most_expensive,
        cheapest=cheapest,
        most_worn=stats_most_worn(5),
        needs_love=stats_needs_love(8),
        best_value=stats_best_value(5),
        timeline=stats_timeline(12),
        by_occasion=stats_by_occasion(8),
        compliment_leaders=stats_compliment_leaders(5),
        temp_coldest=_temp_split[0],
        temp_warmest=_temp_split[1],
        running_low=stats_running_low(),
    )


@app.route("/scraps")
def scraps_list():
    db = get_db()
    query = "SELECT id, title, body_markdown, is_private, created_at, updated_at FROM scraps"
    if not session.get("logged_in"):
        query += " WHERE is_private = 0"
    query += " ORDER BY created_at DESC"
    scraps = db.execute(query).fetchall()
    result = []
    for s in scraps:
        d = dict(s)
        plain = bleach.clean(render_scrap_markdown(d["body_markdown"]), tags=[], strip=True)
        plain = " ".join(plain.split())
        d["preview"] = (plain[:180] + "…") if len(plain) > 180 else plain
        d["created_at_display"] = _utc_to_local_display(d["created_at"])
        result.append(d)
    return render_template("scraps.html", scraps=result)


@app.route("/scraps/<int:scrap_id>")
def scrap_detail(scrap_id):
    db = get_db()
    scrap = db.execute("SELECT * FROM scraps WHERE id = ?", (scrap_id,)).fetchone()
    if scrap is None:
        abort(404)
    if scrap["is_private"] and not session.get("logged_in"):
        # Same response as "doesn't exist" — a private scrap's existence
        # isn't revealed to a visitor who isn't allowed to see it.
        abort(404)
    d = dict(scrap)
    d["created_at_display"] = _utc_to_local_display(d["created_at"])
    d["updated_at_display"] = _utc_to_local_display(d["updated_at"])
    return render_template("scrap_detail.html", scrap=d)


@app.route("/scraps/add", methods=["GET", "POST"])
@login_required
def add_scrap():
    if request.method == "GET":
        return render_template("scrap_form.html", mode="add", scrap=None)

    title = request.form.get("title", "").strip()
    body = request.form.get("body_markdown", "").strip()
    is_private = bool(request.form.get("is_private"))
    if not title:
        flash("Give it a title.", "error")
        return render_template(
            "scrap_form.html", mode="add",
            scrap={"title": title, "body_markdown": body, "is_private": is_private},
        ), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO scraps (title, body_markdown, is_private) VALUES (?, ?, ?)",
        (title, body, is_private),
    )
    db.commit()
    flash(f'Saved "{title}".', "success")
    return redirect(url_for("scrap_detail", scrap_id=cur.lastrowid))


@app.route("/scraps/<int:scrap_id>/edit", methods=["GET", "POST"])
@login_required
def edit_scrap(scrap_id):
    db = get_db()
    scrap = db.execute("SELECT * FROM scraps WHERE id = ?", (scrap_id,)).fetchone()
    if scrap is None:
        abort(404)

    if request.method == "GET":
        return render_template("scrap_form.html", mode="edit", scrap=dict(scrap))

    title = request.form.get("title", "").strip()
    body = request.form.get("body_markdown", "").strip()
    is_private = bool(request.form.get("is_private"))
    if not title:
        flash("Give it a title.", "error")
        stub = dict(scrap)
        stub.update(title=title, body_markdown=body, is_private=is_private)
        return render_template("scrap_form.html", mode="edit", scrap=stub), 400

    db.execute(
        "UPDATE scraps SET title = ?, body_markdown = ?, is_private = ?, updated_at = datetime('now') WHERE id = ?",
        (title, body, is_private, scrap_id),
    )
    db.commit()
    flash(f'Saved "{title}".', "success")
    return redirect(url_for("scrap_detail", scrap_id=scrap_id))


@app.route("/scraps/<int:scrap_id>/toggle-private", methods=["POST"])
@login_required
def toggle_scrap_private(scrap_id):
    db = get_db()
    scrap = db.execute("SELECT is_private FROM scraps WHERE id = ?", (scrap_id,)).fetchone()
    if scrap is None:
        abort(404)
    db.execute(
        "UPDATE scraps SET is_private = ? WHERE id = ?",
        (0 if scrap["is_private"] else 1, scrap_id),
    )
    db.commit()
    return redirect(url_for("scrap_detail", scrap_id=scrap_id))


@app.route("/scraps/<int:scrap_id>/delete", methods=["POST"])
@login_required
def delete_scrap(scrap_id):
    db = get_db()
    scrap = db.execute("SELECT title FROM scraps WHERE id = ?", (scrap_id,)).fetchone()
    if scrap is None:
        abort(404)
    db.execute("DELETE FROM scraps WHERE id = ?", (scrap_id,))
    db.commit()
    flash(f'Deleted "{scrap["title"]}".', "success")
    return redirect(url_for("scraps_list"))


@app.route("/scraps/preview", methods=["POST"])
@login_required
def preview_scrap():
    body = request.form.get("body_markdown", "")
    return render_scrap_markdown(body)


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8000)
