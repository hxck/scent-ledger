PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS fragrances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand           TEXT NOT NULL,
    name            TEXT NOT NULL,
    subname         TEXT,
    image_filename  TEXT,
    description     TEXT,
    daynight        TEXT,
    price           REAL,
    is_gift         INTEGER NOT NULL DEFAULT 0,
    is_discontinued INTEGER NOT NULL DEFAULT 0,
    is_wishlist     INTEGER NOT NULL DEFAULT 0,
    currently_owned INTEGER NOT NULL DEFAULT 1,
    gave_away       INTEGER NOT NULL DEFAULT 0,
    bottles_owned   INTEGER NOT NULL DEFAULT 1,
    fragrantica_url TEXT,
    rating          INTEGER,
    private_notes   TEXT,
    -- Your own 1-5 read on the three attributes every fragrance discussion
    -- turns on. Deliberately separate from `rating` (how much you like it):
    -- a scent can be a 5/5 favourite that projects like a 2.
    longevity_rating   INTEGER,
    sillage_rating     INTEGER,
    projection_rating  INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS seasons (
    fragrance_id    INTEGER NOT NULL REFERENCES fragrances(id) ON DELETE CASCADE,
    season          TEXT NOT NULL,
    PRIMARY KEY (fragrance_id, season)
);

CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fragrance_id    INTEGER NOT NULL REFERENCES fragrances(id) ON DELETE CASCADE,
    tier            TEXT NOT NULL CHECK (tier IN ('top', 'middle', 'base')),
    note_text       TEXT NOT NULL,
    position        INTEGER NOT NULL DEFAULT 0
);

-- Accords are not notes and not tags. A note is an ingredient (bergamot,
-- oud). An accord is the overall character the blend reads as (fresh,
-- woody, sweet) and carries a strength. Fragrantica shows them as ranked
-- bars; folding them into free-form tags loses both the ranking and the
-- distinction, which is why they get their own table.
CREATE TABLE IF NOT EXISTS accords (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS fragrance_accords (
    fragrance_id    INTEGER NOT NULL REFERENCES fragrances(id) ON DELETE CASCADE,
    accord_id       INTEGER NOT NULL REFERENCES accords(id) ON DELETE CASCADE,
    strength        REAL,
    position        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (fragrance_id, accord_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS fragrance_tags (
    fragrance_id    INTEGER NOT NULL REFERENCES fragrances(id) ON DELETE CASCADE,
    tag_id          INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (fragrance_id, tag_id)
);

-- Global note icon cache, keyed by normalized (lowercased, trimmed) note name.
-- One row per distinct note across the whole collection — "Musk" only gets
-- downloaded and stored once, however many fragrances use it.
CREATE TABLE IF NOT EXISTS note_images (
    note_key        TEXT PRIMARY KEY,
    image_filename  TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Custom named groupings of fragrances ("Date Night", "Signature Scents",
-- etc.) — independent of brand, tags, or wishlist status. A fragrance can be
-- on any number of shelves.
CREATE TABLE IF NOT EXISTS shelves (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    icon            TEXT,
    is_private      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shelf_fragrances (
    shelf_id        INTEGER NOT NULL REFERENCES shelves(id) ON DELETE CASCADE,
    fragrance_id    INTEGER NOT NULL REFERENCES fragrances(id) ON DELETE CASCADE,
    PRIMARY KEY (shelf_id, fragrance_id)
);

-- One row per "I wore this today" click — a log, not a single field, since
-- the point is tracking frequency and recency over time.
-- Every column past worn_at is optional context. Logging a wear stays a
-- one-click action; the extras are there when you feel like recording them.
-- This is the one dataset a public fragrance database can't give you — it's
-- what *you* actually reached for, in what conditions, and how it performed
-- on your skin rather than on the average commenter's.
--
-- container_id is nullable and ON DELETE SET NULL: retiring a finished
-- decant shouldn't erase the history of having worn it.
CREATE TABLE IF NOT EXISTS wear_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fragrance_id        INTEGER NOT NULL REFERENCES fragrances(id) ON DELETE CASCADE,
    container_id        INTEGER REFERENCES containers(id) ON DELETE SET NULL,
    worn_at             TEXT NOT NULL DEFAULT (datetime('now')),
    occasion            TEXT,
    weather_temp_f      REAL,
    weather_summary     TEXT,
    sprays              INTEGER,
    longevity_hours     REAL,
    complimented        INTEGER NOT NULL DEFAULT 0,
    wear_note           TEXT
);

-- Small generic key-value store for app-level settings that come from
-- environment variables at startup but need to be readable at request time
-- without re-touching the environment on every call. First (and currently
-- only) use: the configured TZ, synced in on every app startup.
CREATE TABLE IF NOT EXISTS app_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

-- One fragrance can be held in several physical containers at once: a full
-- bottle, a 5ml decant for travel, a 1.5ml sample you're still deciding on,
-- a sealed backup. Size/fill/price/batch all belong to the *container*, not
-- the fragrance — a $8 2ml sample and a $325 70ml bottle of the same juice
-- have completely different per-wear economics.
--
-- size_ml is REAL, not INTEGER: samples are routinely 0.7ml or 1.5ml.
CREATE TABLE IF NOT EXISTS containers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fragrance_id    INTEGER NOT NULL REFERENCES fragrances(id) ON DELETE CASCADE,
    container_type  TEXT NOT NULL DEFAULT 'bottle'
                    CHECK (container_type IN ('bottle', 'decant', 'sample', 'travel')),
    size_ml         REAL,
    fill_level      INTEGER NOT NULL DEFAULT 100,
    purchase_price  REAL,
    purchase_date   TEXT,
    batch_code      TEXT,
    is_finished     INTEGER NOT NULL DEFAULT 0,
    label           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Free-form personal notes ("Scraps") — future purchases, brainstorming,
-- anything the admin wants to jot down. Full Markdown body, rendered (and
-- sanitized) at display time, never stored as HTML.
CREATE TABLE IF NOT EXISTS scraps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    body_markdown   TEXT NOT NULL DEFAULT '',
    is_private      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_fragrances_brand ON fragrances(brand);
CREATE INDEX IF NOT EXISTS idx_notes_fragrance ON notes(fragrance_id);
CREATE INDEX IF NOT EXISTS idx_fragrance_tags_fragrance ON fragrance_tags(fragrance_id);
CREATE INDEX IF NOT EXISTS idx_fragrance_tags_tag ON fragrance_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_shelf_fragrances_shelf ON shelf_fragrances(shelf_id);
CREATE INDEX IF NOT EXISTS idx_shelf_fragrances_fragrance ON shelf_fragrances(fragrance_id);
CREATE INDEX IF NOT EXISTS idx_wear_log_fragrance ON wear_log(fragrance_id);
-- NB: the index on wear_log(container_id) is created in _run_migrations(),
-- not here. On an existing database that column is added by an ALTER that
-- runs *after* this script, so indexing it here fails on every upgrade.
CREATE INDEX IF NOT EXISTS idx_wear_log_worn_at ON wear_log(worn_at);
CREATE INDEX IF NOT EXISTS idx_containers_fragrance ON containers(fragrance_id);
CREATE INDEX IF NOT EXISTS idx_fragrance_accords_fragrance ON fragrance_accords(fragrance_id);
CREATE INDEX IF NOT EXISTS idx_fragrance_accords_accord ON fragrance_accords(accord_id);
CREATE INDEX IF NOT EXISTS idx_scraps_created_at ON scraps(created_at);
