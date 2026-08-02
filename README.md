# The Scent Ledger

A personal fragrance collection catalog. Flask + SQLite backend, server-rendered
Jinja templates, packaged for gunicorn behind Docker.

This is 100% slop-coded and I won't lie or try to hide it. This was a personal
project that I uploaded for ease-of-use and a potential case that anyone might
want to use it. This repo is provided with no guarantees of any kind regarding
this project.

## Features

- Sidebar listing every fragrance, grouped and spaced by brand, with a
  toggle to show only what you currently own (see below)
- Fragrance detail pages: name, "inspired by" subheading, bottle photo,
  an ownership badge (with bottle count and a "gave this one away" note
  when relevant), description, season/day-night badges, a
  Top/Middle/Base note pyramid, and tags — plus a price panel under the
  photo showing the current (reference) price, what you actually paid
  (or 🎁 if it was a gift), and the +/- difference between them.
  Discontinued fragrances get a small ⭐ next to the name (hover for a
  tooltip)
- **Import from Fragrantica** — a bookmarklet that pre-fills the Add form
  from any Fragrantica fragrance page, including small note icons, and
  gap-fills an existing entry instead of duplicating it (see below)
- Add/Edit form with photo upload for manual adds (auto-resized server-side via Pillow, and
  automatically background-removed via `rembg`, chip-style
  note and tag entry, seasons, day/night, optional current
  price and purchase price, Gift/Discontinued flags, and ownership
  tracking (currently owned, gave away, bottles owned)
- Header search bar + a dedicated search page with a chainable tag table
  (select multiple tags, results must match **all** selected tags)
- Edit and delete on every fragrance page
- Collection stats page (`/stats`) — bottle/house/tag/note counts, top notes
  and tags, season and day/night distribution, top houses, total spent
  on the collection, and your most expensive/cheapest bottle (both by what
  you actually paid) — all live queries
- **Wishlist** (`/wishlist`) — fragrances you're eyeing but haven't bought,
  kept completely separate from the sidebar, home page, search, and stats
  until you move them into the collection for real (see below)
- **Single-admin login** gating Add/Edit/Delete, so the site can be made
  public while only you can change anything (see below)

## Admin login

**If you're putting this on the public internet, do these two things:**

1. **Change `ADMIN_PASSWORD` from the default.** The app prints a startup
   warning to the logs if you don't. `docker-compose.yml` ships with a
   placeholder you need to replace, same as `SECRET_KEY`.
2. **Put it behind HTTPS** (a reverse proxy like Caddy, nginx, or a
   Cloudflare Tunnel all work well) and set `SESSION_COOKIE_SECURE=true`
   once you do. Logging in over plain HTTP sends your password in the
   clear; the session cookie is already `HttpOnly` and `SameSite=Lax`
   regardless, but that's not a substitute for TLS.

## Project layout

```
scent-ledger/
├── app.py                 # Flask app, all routes + SQLite queries
├── schema.sql              # Database schema (fragrances, seasons, notes, tags, note_images)
├── requirements.txt
├── gunicorn.conf.py
├── Dockerfile
├── docker-compose.yml
├── static/
│   ├── style.css
│   ├── app.js               # sidebar toggle + chip input widget
│   └── uploads/              # uploaded bottle photos (persisted via volume)
├── templates/
│   ├── base.html, index.html, detail.html, form.html, search.html, 404.html
└── data/                    # SQLite database file lives here (persisted via volume)
```

## Run locally without Docker

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 app.py          # dev server on http://localhost:8000
```

The database is created automatically at `data/scent_ledger.db` on first run.

## Run with Docker Compose (recommended)

```bash
docker compose up --build
```

Visit `http://localhost:8000`. The SQLite file and uploaded images are stored in
named volumes (`scent_ledger_data`, `scent_ledger_uploads`) so they survive
`docker compose down` / rebuilds. Set a real `SECRET_KEY` in
`docker-compose.yml` before deploying anywhere it matters — it's only used to
sign the flash-message cookie, so it's low-stakes, but don't leave the default.

## Run with plain Docker

```bash
docker build -t scent-ledger .
docker run -d \
  -p 8000:8000 \
  -v scent_ledger_data:/app/data \
  -v scent_ledger_uploads:/app/static/uploads \
  -e SECRET_KEY="something-random" \
  -e ADMIN_USERNAME="admin" \
  -e ADMIN_PASSWORD="something-random" \
  --name scent-ledger \
  scent-ledger
```

## Configuration (environment variables)

| Variable               | Default                         | Purpose                                   |
|-------------------------|---------------------------------|--------------------------------------------|
| `DATABASE_PATH`         | `./data/scent_ledger.db`        | SQLite file location                       |
| `UPLOAD_FOLDER`         | `./static/uploads`              | Where bottle photos are saved              |
| `SECRET_KEY`            | `dev-secret-change-me`          | Flask session signing key — **set a real one** |
| `ADMIN_USERNAME`        | `admin`                         | Login for Add/Edit/Delete                  |
| `ADMIN_PASSWORD`        | `changeme`                      | **Change this.** Logs a startup warning if left default |
| `SESSION_COOKIE_SECURE` | `false`                         | Set `true` once served over HTTPS          |
| `PORT`                  | `8000`                          | Port gunicorn binds to                     |
| `GUNICORN_WORKERS`      | `1`                              | See note below on SQLite + concurrency     |
| `GUNICORN_THREADS`      | `4`                              | Threads per worker                         |

## Backing up your data

The whole database is a single file:
```bash
docker compose cp scent-ledger:/app/data/scent_ledger.db ./backup.db
```

Uploaded photos (bottle photos and cached note icons) live under the
`scent_ledger_uploads` volume:

```bash
docker compose cp scent-ledger:/app/static/uploads ./uploads_backup
```

(If you're not using Compose — e.g. you started the container with plain
`docker run` — swap `docker compose cp` for `docker cp <container-name>`,
finding the name with `docker ps`.)

## Restoring a backup into a fresh installation

Same two pieces, copied in the opposite direction. Get the fresh install
running first (an empty database is fine — you're about to overwrite it),
then copy your backup files in and restart:

```bash
# 1. Start the fresh instance so its volumes exist
docker compose up -d

# 2. Copy your backed-up database over the empty one it just created
docker compose cp ./backup.db scent-ledger:/app/data/scent_ledger.db

# 3. Copy your backed-up photos in (the trailing /. copies the folder's
#    *contents* into uploads/, rather than nesting a folder inside it)
docker compose cp ./uploads_backup/. scent-ledger:/app/static/uploads/

# 4. Restart so nothing's holding a stale handle to the empty DB it started with
docker compose restart
