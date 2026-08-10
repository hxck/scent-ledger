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
    purchase_price  REAL,
    is_gift         INTEGER NOT NULL DEFAULT 0,
    is_discontinued INTEGER NOT NULL DEFAULT 0,
    is_wishlist     INTEGER NOT NULL DEFAULT 0,
    currently_owned INTEGER NOT NULL DEFAULT 1,
    gave_away       INTEGER NOT NULL DEFAULT 0,
    bottles_owned   INTEGER NOT NULL DEFAULT 1,
    fragrantica_url TEXT,
    rating          INTEGER,
    fill_level      INTEGER NOT NULL DEFAULT 100,
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
CREATE TABLE IF NOT EXISTS wear_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fragrance_id    INTEGER NOT NULL REFERENCES fragrances(id) ON DELETE CASCADE,
    worn_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Small generic key-value store for app-level settings that come from
-- environment variables at startup but need to be readable at request time
-- without re-touching the environment on every call. First (and currently
-- only) use: the configured TZ, synced in on every app startup.
CREATE TABLE IF NOT EXISTS app_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fragrances_brand ON fragrances(brand);
CREATE INDEX IF NOT EXISTS idx_notes_fragrance ON notes(fragrance_id);
CREATE INDEX IF NOT EXISTS idx_fragrance_tags_fragrance ON fragrance_tags(fragrance_id);
CREATE INDEX IF NOT EXISTS idx_fragrance_tags_tag ON fragrance_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_shelf_fragrances_shelf ON shelf_fragrances(shelf_id);
CREATE INDEX IF NOT EXISTS idx_shelf_fragrances_fragrance ON shelf_fragrances(fragrance_id);
CREATE INDEX IF NOT EXISTS idx_wear_log_fragrance ON wear_log(fragrance_id);
