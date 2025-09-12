# Records - Beta

A selfâ€‘hosted, singleâ€‘page app for browsing a Discogsâ€‘backed music collection with artwork management, local/Plex playback helpers, and optional BluOS control. Backend is FastAPI [...]


## Highlights

- Fullâ€‘collection UI: grid and list views with search, sort, and multiâ€‘format filters.
- Discogs sync: imports your collection with pagination handling and deâ€‘duplication.
- Artwork enrichment: MusicBrainz + Cover Art Archive lookups with local caching and thumbnails.
- Playback helpers: local file streaming (Range + HLS via ffmpeg) and Plex proxy/HLS.
- BluOS integration: browse folder mappings and trigger playback on a player.
- Tracklists: derive/save tracklists from Discogs, Plex, or local library; stored per record.
- Dark theme: responsive, keyboardâ€‘friendly UI; no external JS frameworks.


## Architecture

- Backend: FastAPI (`app/main.py`), SQLAlchemy models (`app/models.py`), CRUD (`app/crud.py`).
- Database: PostgreSQL 16 (Docker). Schema bootstrap in `db-init/*.sql`; runtime guards in `ensure_schema()`.
- Frontend: Static assets in `app/static` (`index.html`, `app.js`, `styles.css`).
- Integrations: Discogs (`app/discogs_client.py`), MusicBrainz/Cover Art (`app/artwork.py`), Plex (`app/plex.py`), BluOS (`app/bluos.py`), local library indexer (`app/local_media.py`).
- Media processing: Pillow for images, ffmpeg for HLS, mutagen for durations.
- Containers: `Dockerfile`, `docker-compose.yml` (ports, volumes, healthchecks).

### Diagram

The system runs two containers (API + Postgres) and integrates with external services. Edit the source at `docs/architecture.mmd`.

```mermaid
flowchart LR
    subgraph Compose[Docker Compose]
        API["FastAPI (uvicorn)<br/>app/main.py"]
        DB["PostgreSQL 16"]
        STATIC["app/static<br/>(artwork, thumbs, js, css)"]
        MUSIC["MUSIC_ROOT (host)<br/>mounted read-only"]
        FF["ffmpeg<br/>(HLS transcode)"]

        API --- DB
        API --- STATIC
        API --- FF
        MUSIC -. "scan/stream" .- API
    end

    BROWSER["Browser UI"]
    DISC["Discogs API"]
    MB["MusicBrainz"]
    CAA["Cover Art Archive"]
    PLEX["Plex Server"]
    BLUOS["BluOS Player"]

    BROWSER <--> API
    API --> DISC
    API --> MB
    API --> CAA
    API --> PLEX
    API --> BLUOS
```

## Quick Start (Docker Compose)

1) Create `.env.runtime` (not committed):

Copy from the provided template and edit values:

```sh
cp .env.template .env.runtime
# Windows (PowerShell)
copy .env.template .env.runtime
```

```env
DATABASE_URL=postgresql+psycopg2://records:records@db:5432/records

# Discogs API (required for collection sync)
DISCOGS_TOKEN=your_token
DISCOGS_USERNAME=your_discogs_username
DISCOGS_USER_AGENT=records-app/1.0 (+you@example.com)

# Optional: Plex integration
PLEX_URL=http://127.0.0.1:32400
PLEX_TOKEN=your_plex_token
PLEX_SECTION_ID=4

# Optional: BluOS integration
BLUOS_HOST=192.168.1.50
BLUOS_PORT=11000
BLUOS_LIBRARY_ROOT=/var/mnt/MUSIC  # folder as seen from BluOS player

# Optional: local library and public base URL
MUSIC_ROOT=/mnt/music              # mounted into container
APP_PUBLIC_BASE_URL=http://LAN-IP:8888
```

2) Start services:

```sh
docker compose --env-file .env.runtime up -d --build
```

3) Open the app: http://localhost:8888

Notes
- Compose maps `./app/static` into the container so artwork (`artwork/`) and thumbnails (`thumbs/`) persist on host.
- DB is initialized by `db-init/` SQL; user/db `records` is created on first start.


## Local Development (without Docker)

```sh
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export DATABASE_URL=postgresql+psycopg2://records:records@127.0.0.1:5432/records
export DISCOGS_TOKEN=...
export DISCOGS_USERNAME=...
export DISCOGS_USER_AGENT="records-app/1.0 (+you@example.com)"
export APP_HOST=127.0.0.1 APP_PORT=8888

uvicorn app.main:app --host $APP_HOST --port $APP_PORT --reload
```

Ensure you have a running PostgreSQL with the `records` database and `records` role (see `db-init/`). ffmpeg must be available on PATH for HLS features.


## UI Overview

- Grid view (200px) and List view (125px): toggle via toolbar.
- Sort: by artist, album, or year (asc/desc), with tieâ€‘breakers.
- Multiâ€‘select Format filter (Vinyl/CD/Cassetteâ€¦); supports CSV via API.
- Search: realâ€‘time by artist or album title.
- Overlay: artwork editor (search via MusicBrainz/Discogs, upload URL/file, remove/replace), tracklist display.
- Sync panel: unified control for Discogs/BluOS sync + inline progress logs.


## Major Workflows

1) Sync collection (Discogs)
- Start full sync: POST `/sync` (UI: Sync Collection). Imports/updates records via `discogs_client`.
- Newâ€‘only sync: POST `/sync/new-only` to stop early when only known pages are found.
- Partial/targeted: POST `/sync/partial` to enrich only missing/untouched fields.
- Progress and logs: GET `/sync/status`, `/sync/progress`, `/sync/logs`.

2) Artwork enrichment and management
- Autoâ€‘enrich per record using MusicBrainz releaseâ€‘group search + Cover Art Archive download.
- Manual search: POST `/artwork/search/musicbrainz` or `/artwork/search/discogs`.
- Validate/set/upload/remove: `/artwork/validate-url`, `/artwork/set`, `/artwork/upload`, `/artwork/remove`.
- Files saved to `app/static/artwork` (full) and `app/static/thumbs` (150px). URLs are served under `/static/...`.

3) Tracklists
- GET `/records/{id}/tracklist`: derive from local library (preferred), Discogs, or Plex if configured.
- POST `/records/{id}/tracklist`: save/replace tracks for a record (stored in `tracks` table).
- Local indexer: `app/local_media.py` scans `MUSIC_ROOT` folders to find albums and parse common filename patterns.

4) Playback helpers
- Local audio: GET `/local/stream?p=<relpath>` with Range support. HLS start at `/local/hls/start?p=...` (ffmpeg).
- Plex audio: direct proxy `/plex/stream/{rating_key}` or HLS via `/plex/hls/start/{rating_key}`.
- BluOS: control endpoints for play/pause/stop/seek, volume, presets, browse, and play URL. See `/bluos/*`.

5) BluOS mapping sync (optional)
- Requirements: `BLUOS_HOST`, `BLUOS_LIBRARY_ROOT`, and a populated local index (`MUSIC_ROOT`).
- Process: `app/bluos_sync.py` browses the corresponding LocalMusic folders and stores a normalized titleâ†’playURL map per record in table `bluos_maps` with a simple match score.


## API Surface (grouped)

- UI and health
  - GET `/` (index.html)
  - GET `/healthz` (static health JSON)
- Records
  - GET `/records/all` (paging/sorting/filtering via query), GET `/formats`
  - POST `/records/{id}/update`, GET/POST `/records/{id}/tracklist`, POST `/collection/reset`
- Sync
  - POST `/sync`, `/sync/new-only`, `/sync/partial`, `/sync/reset`, `/sync/discogs`, `/sync/bluos`, `/sync/cancel`
  - GET `/sync/status`, `/sync/progress`, `/sync/logs`, `/sync/logs/clear`, `/sync/count`
- Artwork
  - POST `/artwork/search/musicbrainz`, `/artwork/search/discogs`, `/artwork/search/*-flexible`
  - POST `/artwork/validate-url`, `/artwork/set`, `/artwork/upload`, `/artwork/remove`
  - GET `/artwork/{filename}` (serve stored images)
- Local media
  - GET `/local/ping`, `/local/album/{record_id}`, `/local/stream`, `/local/hls/*`, POST `/local/scan`
- Plex
  - GET `/plex/ping`, `/plex/album/{record_id}`, `/plex/stream/{rating_key}`, `/plex/hls/*`
- BluOS
  - GET `/bluos/ping`, `/bluos/status`, `/bluos/presets`, `/bluos/resolve/album/{record_id}`, `/bluos/map/{record_id}`
  - POST `/bluos/transport`, `/bluos/volume`, `/bluos/preset`, `/bluos/play-url`, `/bluos/play-plex/{rating_key}`, `/bluos/play-local`, `/bluos/action`


## Data Model (key tables)

- `records`: discogs_id, title, artist_name, artist_display_name, year, original_year, edition_year, label, country, format, genre, style, date_added, mb_release_group_id, cover_art_url, cover_thumb_u[...]
- `tracks`: record_id (FK), position, title, duration, track_order, created_at.
- `bluos_maps`: record_id (PK), folder, play_map (JSONB), matched, match_score, updated_at.

Schema evolves via `db-init/*.sql` and guarded additions in `ensure_schema()` on startup.


## Configuration

Environment variables (see `.env.runtime`):

- Database: `DATABASE_URL`
- Discogs: `DISCOGS_TOKEN`, `DISCOGS_USERNAME`, `DISCOGS_USER_AGENT`
- Local media: `MUSIC_ROOT`
- Public base URL: `APP_PUBLIC_BASE_URL` (used to produce absolute URLs for players)
- Plex: `PLEX_URL`, `PLEX_TOKEN`, `PLEX_SECTION_ID`
- BluOS: `BLUOS_HOST`, `BLUOS_PORT`, `BLUOS_LIBRARY_ROOT`

Security and persistence
- Secrets must not be committed. `.env.runtime` is gitâ€‘ignored.
- Artwork and thumbs live under `app/static/` (bindâ€‘mounted by compose).
- HLS segments are written under the system temp dir (e.g., `/tmp/hls`).


## Milestones & Release Workflow

Use `milestone.sh` to tag stable states and allow quick restore.

```sh
./milestone.sh create <name> "Milestone: <description>"
./milestone.sh list
./milestone.sh diff <name>
./milestone.sh restore <name>
```

Tags are created as `milestone-<name>` and pushed to `origin`. Restore performs a hard reset of `main` to the tag.


## Troubleshooting

- Database
  - `docker compose logs db` and `psql -U postgres -d records -c "SELECT COUNT(*) FROM records;"`
- Discogs auth
  - `curl -H "Authorization: Discogs token=..." https://api.discogs.com/oauth/identity`
- Artwork paths
  - Verify files in `app/static/artwork` and `app/static/thumbs`; check `/static/...` URLs
- ffmpeg/HLS
  - Ensure `ffmpeg` exists in container/host; inspect `/local/hls/start?p=...` response
- Plex
  - Check `PLEX_URL`/`PLEX_TOKEN`; test `/plex/ping`
- BluOS
  - Check `BLUOS_HOST`/`BLUOS_PORT`; test `/bluos/ping` and `/bluos/status`

Reset (destructive)
```sh
docker compose down
rm -rf pgdata
docker compose up -d --build
```


## Credits & Licenses

- Code: Original work for this project. No thirdâ€‘party source files were copied into the codebase.
- APIs: Discogs, MusicBrainz, Cover Art Archive, Plex, and BluOS are used via their public HTTP APIs.
- Docs: `docs/BluOS-Custom-Integration-API_v1.7.pdf` and `docs/bluos_api_v1_7.txt` are vendor documentation included for reference.
- Libraries: FastAPI, SQLAlchemy, psycopg2, httpx, requests, Pillow, mutagen. ffmpeg is used for HLS.

If you identify any snippet that should carry attribution, please open an issue with the file and line reference.


## License

Personal project â€” all rights reserved unless otherwise noted.
