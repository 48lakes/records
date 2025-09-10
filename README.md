# Records — Pre-Overlay Milestone

Two services: **db** (Postgres 16) + **api** (FastAPI). UI at `/`. Loads **full collection** via `/records/all` (no UI pagination). Thumbs are 150×150.

## Features

✅ **Complete Artwork UI** - Grid (200x200) and List (125x125) views with artwork display  
✅ **Enhanced List View** - Shows format, label, genre, style details for each record  
✅ **Artwork Management** - MusicBrainz/Discogs search integration with artwork editing  
✅ **Dark Theme** - Complete responsive dark UI with proper contrast  
✅ **View Switching** - Toggle between grid and detailed list views  
✅ **Search & Filter** - Real-time search and format filtering  
✅ **751 Records** - Full collection loading and display  
✅ **Stable Codebase** - No JavaScript errors, clean field mapping  

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
# Access at http://your-synology-ip:8888
```

## Development Setup

```sh
# Clone repository
git clone https://github.com/48lakes/records.git
cd records

# Create environment file
cp .env.template .env.runtime
# Edit .env.runtime with your tokens and passwords

# Create required directories
mkdir -p artwork thumbs static pgdata

# Start services
docker compose up -d --build

# Check logs
docker logs -f records_api
docker logs -f records_db

# Access application
open http://localhost:8888
```

## Architecture

```
┌─────────────────┐    ┌─────────────────┐
│   Frontend      │    │   Backend       │
│   Static Files  │───▶│   FastAPI       │
│   - app.js      │    │   - main.py     │
│   - styles.css  │    │   - crud.py     │
│   - index.html  │    │   - models.py   │
└─────────────────┘    └─────────────────┘
                                │
                       ┌─────────────────┐
                       │   Database      │
                       │   PostgreSQL 16 │
                       │   - records     │
                       │   - artwork     │
                       └─────────────────┘
```

## API Endpoints

- **GET** `/` → Main UI application
- **GET** `/healthz` → Health check with database and Discogs status
- **GET** `/records/all` → All records with sorting/filtering
- **GET** `/formats` → Available record formats
- **POST** `/sync/start` → Start Discogs collection sync
- **GET** `/sync/status` → Current sync progress
- **POST** `/artwork/search/musicbrainz` → Search MusicBrainz for artwork
- **POST** `/artwork/search/discogs` → Search Discogs for artwork

## UI Views

### Grid View (200x200 artwork)
- Clean grid layout with album artwork
- Title, artist, and year displayed below artwork
- Click artwork placeholder to search for missing artwork
- Responsive grid adapts to screen size

### List View (125x125 artwork)
- Detailed list with larger artwork thumbnails
- Shows format, label, genre, style for each record
- Compact layout with hover effects
- Year displayed in separate column

### Features
- **Real-time search** - Filter by artist or album title
- **Format filtering** - Show only specific formats (Vinyl, CD, etc.)
- **Sorting options** - By artist, album, or year (ascending/descending)
- **Artwork management** - Search and replace missing artwork
- **Responsive design** - Works on desktop and mobile

## Database Schema

```sql
-- Core records table
CREATE TABLE records (
    id SERIAL PRIMARY KEY,
    discogs_id INTEGER UNIQUE,
    title VARCHAR(500),
    artist_name VARCHAR(500),
    year INTEGER,
    label VARCHAR(500),
    country VARCHAR(100),
    format VARCHAR(100),
    genre VARCHAR(200),
    style VARCHAR(200),
    cover_art_url VARCHAR(1000),
    cover_thumb_url VARCHAR(1000),
    artwork_url VARCHAR(1000),
    mb_release_group_id VARCHAR(100),
    artist_id INTEGER
);
```

## File Structure

```
records/
├── app/
│   ├── main.py              # FastAPI application
│   ├── crud.py              # Database operations
│   ├── models.py            # SQLAlchemy models
│   └── static/
│       ├── index.html       # Main UI
│       ├── app.js           # Frontend JavaScript
│       ├── styles.css       # Dark theme CSS
│       ├── sync.js          # Sync functionality
│       ├── artwork/         # Full-size artwork (ignored by git)
│       └── thumbs/          # 150x150 thumbnails (ignored by git)
├── docker-compose.yml       # Container orchestration
├── Dockerfile              # API container definition
├── requirements.txt        # Python dependencies
├── .env.runtime           # Environment variables (ignored by git)
├── .gitignore             # Git exclusions
├── milestone.sh           # Milestone management script
└── README.md              # This file
```

## Milestone Management

This project uses git tags for milestone management to ensure safe development with rollback capability.

### Create New Milestone
```sh
# When you reach a stable working state
./milestone.sh create <milestone-name> "<description>"

# Example
./milestone.sh create overlay-enhanced "MILESTONE: Enhanced overlay with full details"
```

### List Available Milestones
```sh
./milestone.sh list
```

### Return to Previous Working State
```sh
# If something breaks, restore to last working state
./milestone.sh restore <milestone-name>

# Example
./milestone.sh restore artwork-ui-complete
```

### Compare Changes
```sh
# See what changed since a milestone
./milestone.sh diff <milestone-name>

# Example
./milestone.sh diff artwork-ui-complete
```

### Current Milestones

- **`milestone-artwork-ui-complete`** - Complete artwork UI with enhanced list view
  - 751 records loading successfully
  - Grid view: 200x200 artwork display
  - List view: 125x125 artwork + detailed info (format, label, genre, style)
  - Artwork editing with MusicBrainz/Discogs search
  - Responsive dark theme, view switching, search/filter
  - No JavaScript console errors, stable checkpoint

### Milestone Workflow

1. **Work normally** - Regular commits as you develop
2. **Reach stable state** - All features working, no errors
3. **Create milestone** - `./milestone.sh create <name> "<description>"`
4. **Continue development** - Keep working on new features
5. **If something breaks** - `./milestone.sh restore <last-working-milestone>`
6. **Safe experimentation** - Always have a reliable fallback

## Development Workflow

```sh
# 1. Start development
docker compose up -d

# 2. Make changes to code
# Edit files in app/ directory

# 3. Test changes
# Refresh browser at http://localhost:8888

# 4. Regular commits
git add .
git commit -m "Add feature X"

# 5. Reach stable milestone
./milestone.sh create feature-complete "MILESTONE: Feature X working perfectly"

# 6. Continue development or restore if needed
./milestone.sh restore feature-complete  # if something breaks
```

## Environment Variables

```env
# Required for Discogs API
DATABASE_URL=postgresql+psycopg2://records:records@db:5432/records
DISCOGS_TOKEN=YOUR_DISCOGS_TOKEN
DISCOGS_USERNAME=YOUR_DISCOGS_USERNAME
DISCOGS_USER_AGENT=records-app/1.0 (+you@example.com)

# Optional services
PLEX_URL=
PLEX_TOKEN=

# Optional: MusicBrainz rate limiting
MUSICBRAINZ_RATE_LIMIT=1.0
```

## Troubleshooting

### Common Issues

**No records loading:**
```sh
# Check database connection
docker logs records_db
docker exec -it records_db psql -U postgres -d records -c "SELECT COUNT(*) FROM records;"
```

**Artwork not displaying:**
```sh
# Check artwork files exist
docker exec -it records_api ls -la /app/app/static/artwork/ | head -5
curl -I "http://localhost:8888/static/artwork/31464128.jpg"
```

**Sync not working:**
```sh
# Check Discogs token
curl -H "Authorization: Discogs token=YOUR_TOKEN" https://api.discogs.com/oauth/identity
```

### Reset Everything
```sh
# Stop containers
docker compose down

# Remove data (WARNING: destroys database)
docker volume rm records_pgdata

# Restart fresh
docker compose up -d --build
```

## Next Steps

- [ ] Enhanced overlay with full record details
- [ ] Advanced search with multiple filters
- [ ] Bulk artwork operations
- [ ] Export functionality
- [ ] Collection statistics and insights
- [ ] User preferences and settings

## Do not commit secrets
- `.env.runtime`, dumps, overrides are ignored by `.gitignore`.

## License

Personal project -
