# Records — Pre-Overlay Milestone

Two services: **db** (Postgres 16) + **api** (FastAPI). UI at `/`. Loads **full collection** via `/records/all` (no UI pagination). Thumbs are 150×150.

## Run (Synology SSH)

```sh
cd /mnt/data/records_pre_overlay
cat > .env.runtime <<'ENV'
DATABASE_URL=postgresql+psycopg2://records:records@db:5432/records
DISCOGS_TOKEN=YOUR_DISCOGS_TOKEN
DISCOGS_USERNAME=YOUR_DISCOGS_USERNAME
DISCOGS_USER_AGENT=records-app/1.0 (+you@example.com)
PLEX_URL=
PLEX_TOKEN=
ENV

docker compose --env-file .env.runtime up -d --build

# seed sample rows (optional)
docker compose exec -T db psql -U postgres -d records -f /docker-entrypoint-initdb.d/00-init.sql || true
docker compose exec -T db psql -U postgres -d records -f /app/db-dumps/records_sample.sql || true

# health
curl -s http://127.0.0.1:8888/healthz
```

## Setup

1. Clone the repository
2. Copy the environment template:
   ```bash
   cp .env.template .env.runtime
   ```
3. Edit `.env.runtime` and add your Discogs API credentials
4. Create required directories:
   ```bash
   mkdir -p artwork thumbs static pgdata
   ```
5. Start the application:
   ```bash
   docker compose up -d --build
   ```

## Endpoints
- `GET /healthz` → `{status,db_ok,discogs_config_ok}`
- `GET /formats`
- `GET /records/all?sort_by=artist|album|year&order=asc|desc&format=&q=`
- `POST /sync/start`

## Do not commit secrets
- `.env.runtime`, dumps, overrides are ignored by `.gitignore`.
