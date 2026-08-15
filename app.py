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
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

from flask import (
    Flask, g, render_template, request, redirect, url_for, flash, abort, session, Response
)
from werkzeug.utils import secure_filename
from PIL import Image

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
    if "purchase_price" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN purchase_price REAL")
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
    if "fill_level" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN fill_level INTEGER NOT NULL DEFAULT 100")
    if "private_notes" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN private_notes TEXT")
    if "size_ml" not in existing_cols:
        conn.execute("ALTER TABLE fragrances ADD COLUMN size_ml INTEGER")


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
    return {
        "sidebar_groups": groups, "sidebar_total": total, "sidebar_brand_count": len(groups),
        "wishlist_count": wishlist_count, "sidebar_owned_only": bool(session.get("sidebar_owned_only")),
        "sidebar_total_unfiltered": total_unfiltered, "missing_notes_count": missing_notes_count,
        "shelves_count": shelves_count, "all_shelves_for_bulk": all_shelves_for_bulk,
        "todays_frag": todays_frag, "todays_frag_label": todays_frag_label,
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


def _cost_per_wear_from_size(price, size_ml):
    """Cost per wear from bottle economics — 10 sprays per ml (1,000
    sprays in a 100ml bottle), 6 sprays per wear — rather than actual
    purchase price divided by times logged worn. This is a fixed number
    based on price and bottle size alone; it doesn't change as you log
    more wears, and doesn't need any wear log data to compute at all.
    Needs both a price and a size to mean anything."""
    if not price or not size_ml:
        return None
    total_sprays = size_ml * 10
    cost_per_spray = price / total_sprays
    return cost_per_spray * 6


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
    frag["cost_per_wear"] = _cost_per_wear_from_size(
        frag.get("purchase_price") or frag.get("price"), frag.get("size_ml")
    )

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
        "SELECT COUNT(purchase_price) AS purchased_count, SUM(purchase_price) AS total_spent "
        "FROM fragrances WHERE is_wishlist = 0"
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


def stats_timeline(months=12):
    """How the collection grew, by month, oldest to newest — the one thing
    created_at was tracked for but never actually shown anywhere until now.
    Always returns exactly `months` calendar-month buckets ending this month,
    zero-filled where nothing was added — a real gap should show as an empty
    bar, not be silently skipped and make the timeline look compressed."""
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
        "SELECT strftime('%Y-%m', created_at) AS ym, COUNT(*) AS cnt "
        "FROM fragrances WHERE is_wishlist = 0 GROUP BY ym"
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
    times the size/price-derived cost per wear (see
    _cost_per_wear_from_size). Needs a size and a price (purchase price,
    falling back to reference price) to compute at all; wear count alone
    isn't enough since a fragrance with no price/size attached can't have
    a dollar value attached to its wears. Computed in Python rather than
    SQL since the per-row formula has an intermediate step (cost per wear)
    that's clearer to reason about outside a single query, and this is
    personal-collection scale, not a performance-sensitive path."""
    db = get_db()
    rows = db.execute(
        """
        SELECT f.id, f.brand, f.name,
               COALESCE(f.purchase_price, f.price) AS price_for_calc,
               f.size_ml, COUNT(w.id) AS wear_count
        FROM fragrances f
        JOIN wear_log w ON w.fragrance_id = f.id
        WHERE f.is_wishlist = 0 AND f.size_ml IS NOT NULL
          AND COALESCE(f.purchase_price, f.price) IS NOT NULL
        GROUP BY f.id
        """
    ).fetchall()
    result = []
    for r in rows:
        cost_per_wear = _cost_per_wear_from_size(r["price_for_calc"], r["size_ml"])
        if cost_per_wear is None:
            continue
        result.append({
            "id": r["id"], "brand": r["brand"], "name": r["name"],
            "wear_count": r["wear_count"], "cost_per_wear": cost_per_wear,
            "amount_worn": cost_per_wear * r["wear_count"],
        })
    result.sort(key=lambda x: x["amount_worn"], reverse=True)
    return result[:limit]


def stats_price_extremes():
    """Most/least expensive by purchase_price (what you actually paid), not the
    Fragrantica reference price — matches "Total Spent" using the same basis.
    Wishlist items are excluded (nothing's been paid for those yet). Returns
    (most_expensive, cheapest), either possibly None if nothing qualifies."""
    db = get_db()
    most_expensive = db.execute(
        """
        SELECT id, brand, name, image_filename, purchase_price
        FROM fragrances
        WHERE purchase_price IS NOT NULL AND is_wishlist = 0
        ORDER BY purchase_price DESC, name ASC
        LIMIT 1
        """
    ).fetchone()
    cheapest = db.execute(
        """
        SELECT id, brand, name, image_filename, purchase_price
        FROM fragrances
        WHERE purchase_price IS NOT NULL AND is_wishlist = 0
        ORDER BY purchase_price ASC, name ASC
        LIMIT 1
        """
    ).fetchone()
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


def build_bookmarklet_href(add_url):
    """Reads the extraction script and inlines it as a javascript: bookmarklet URI,
    pointed at this deployment's own /add URL."""
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
                 price, purchase_price, is_gift, is_discontinued, is_wishlist,
                 currently_owned, gave_away, bottles_owned, fragrantica_url,
                 rating, fill_level, private_notes, size_ml)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (brand, name, subname, image_filename, description, daynight,
             price, purchase_price, is_gift, is_discontinued, is_wishlist,
             currently_owned, gave_away, bottles_owned, fragrantica_url,
             rating, fill_level, private_notes, size_ml),
        )
        fragrance_id = cur.lastrowid
    else:
        db.execute(
            """
            UPDATE fragrances
            SET brand = ?, name = ?, subname = ?, image_filename = ?, description = ?, daynight = ?,
                price = ?, purchase_price = ?, is_gift = ?, is_discontinued = ?, is_wishlist = ?,
                currently_owned = ?, gave_away = ?, bottles_owned = ?, fragrantica_url = ?,
                rating = ?, fill_level = ?, private_notes = ?, size_ml = ?
            WHERE id = ?
            """,
            (brand, name, subname, image_filename, description, daynight,
             price, purchase_price, is_gift, is_discontinued, is_wishlist,
             currently_owned, gave_away, bottles_owned, fragrantica_url,
             rating, fill_level, private_notes, size_ml, fragrance_id),
        )

    replace_seasons(db, fragrance_id, seasons)
    replace_notes(db, fragrance_id, notes)
    upsert_tags(db, fragrance_id, tags)
    _prune_orphaned_tags(db)

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
    db.execute("INSERT INTO wear_log (fragrance_id) VALUES (?)", (fragrance_id,))
    _set_todays_fragrance(fragrance_id)
    db.commit()
    flash("Logged today's wear.", "success")
    return redirect(url_for("detail", fragrance_id=fragrance_id))


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
        seasons = [r["season"] for r in db.execute(
            "SELECT season FROM seasons WHERE fragrance_id = ?", (fid,)
        ).fetchall()]
        notes_rows = db.execute(
            "SELECT tier, note_text FROM notes WHERE fragrance_id = ? ORDER BY tier, position", (fid,)
        ).fetchall()
        notes_by_tier = {"top": [], "middle": [], "base": []}
        for n in notes_rows:
            notes_by_tier[n["tier"]].append(n["note_text"])
        tags = [r["name"] for r in db.execute(
            "SELECT t.name FROM tags t JOIN fragrance_tags ft ON ft.tag_id = t.id WHERE ft.fragrance_id = ?",
            (fid,),
        ).fetchall()]
        wear_rows = db.execute(
            "SELECT worn_at FROM wear_log WHERE fragrance_id = ? ORDER BY worn_at DESC", (fid,)
        ).fetchall()
        writer.writerow([
            f["brand"], f["name"], f["subname"] or "", f["description"] or "", f["daynight"] or "",
            ", ".join(seasons),
            "; ".join(notes_by_tier["top"]), "; ".join(notes_by_tier["middle"]), "; ".join(notes_by_tier["base"]),
            ", ".join(tags),
            f["price"] if f["price"] is not None else "",
            f["purchase_price"] if f["purchase_price"] is not None else "",
            f["rating"] if f["rating"] is not None else "",
            f["fill_level"],
            "Yes" if f["currently_owned"] else "No",
            f["bottles_owned"],
            "Yes" if f["is_gift"] else "No",
            "Yes" if f["is_discontinued"] else "No",
            "Yes" if f["gave_away"] else "No",
            "Yes" if f["is_wishlist"] else "No",
            f["fragrantica_url"] or "",
            len(wear_rows),
            _utc_to_local_display(wear_rows[0]["worn_at"]) if wear_rows else "",
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
    )


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8000)
