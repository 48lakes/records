from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, Depends, Query, HTTPException, BackgroundTasks, Request
from fastapi import UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
import logging
import requests
import os
import json
import re
from urllib.parse import urlparse
from PIL import Image
from io import BytesIO
from datetime import datetime

# Import your modules
from .db import engine, get_db
from .crud import (
    list_records_all,
    get_record_tracks,
    save_record_tracks,
    fetch_and_store_tracklist,
    update_record_fields,
    get_local_override,
    set_local_override,
    delete_local_override,
    upsert_record,
)
from .importer import sync_discogs_collection, request_cancel, clear_cancel
from .artwork import enrich_with_artwork
from .sync_utils import discogs_payload_signature
from .plex import PlexClient
from .normalize import normalize_title, sanitize_for_local, fold_to_ascii
from .bluos import BluOSClient
from .local_media import (
    scan_library as local_scan,
    album_tracks as local_album_tracks,
    album_track_list as local_album_track_list,
    album_tracks_from_folder as local_album_tracks_from_folder,
    album_track_list_from_folder as local_album_track_list_from_folder,
    folder_for_album as local_folder_for_album,
    search_albums as local_search_albums,
    get_track_duration_seconds,
    read_metadata as local_read_metadata,
    format_duration,
    build_stream_url as local_stream_url,
    set_music_root as local_set_root,
)
from .bluos_sync import sync_bluos_for_collection
from pathlib import Path
import tempfile
import subprocess
import uuid
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Record Collection API")

# Ensure DB schema has expected columns
def ensure_schema():
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS original_year INTEGER"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS edition_year INTEGER"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS artist_display_name TEXT"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS date_added TEXT"))
            # Backfill display name where missing
            conn.execute(text(
                """
                UPDATE records
                SET artist_display_name = regexp_replace(artist_name, ' \\([0-9]+\\)$', '')
                WHERE (artist_display_name IS NULL OR artist_display_name = '') AND artist_name IS NOT NULL
                """
            ))
            # Add sync/edit tracking columns
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS user_modified_at TIMESTAMP NULL"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMP NULL"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS discogs_payload_hash TEXT"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS discogs_payload JSONB"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS artwork_synced_at TIMESTAMP NULL"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS artwork_source_url TEXT"))
            # BluOS mapping storage (separate table)
            conn.execute(text(
                """
                CREATE TABLE IF NOT EXISTS bluos_maps (
                    record_id INTEGER PRIMARY KEY,
                    folder TEXT,
                    play_map JSONB,
                    matched BOOLEAN,
                    match_score INTEGER,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
                """
            ))
            conn.execute(text(
                """
                CREATE TABLE IF NOT EXISTS local_album_overrides (
                    record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
                    folder TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
                """
            ))
    except Exception as e:
        logger.warning(f"Schema ensure failed (non-fatal): {e}")

# Run schema check at import time
ensure_schema()

# Static file paths
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Ensure artwork directories exist at startup
ARTWORK_DIR = STATIC_DIR / "artwork"
THUMBS_DIR = STATIC_DIR / "thumbs"
ARTWORK_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

# Initialize local media root (if provided)
try:
    local_set_root(os.getenv("MUSIC_ROOT"))
except Exception:
    pass

# Sync state for tracking progress
sync_state = {
    "status": "not_started",  # not_started, running, completed, error
    "progress": 0,
    "message": "",
    "recent_items": [],
    "stats": {"updated": 0, "unchanged": 0},
    "current": 0,
    "total": 0,
    "last_item": None,
}

# In-memory sync logs (per run)
sync_logs: list[dict] = []
sync_log_next_id: int = 1

def add_sync_log(message: str, level: str = "info"):
    global sync_log_next_id
    if not message:
        return
    entry = {
        "id": sync_log_next_id,
        "ts": datetime.utcnow().isoformat() + "Z",
        "level": level,
        "message": str(message)
    }
    sync_log_next_id += 1
    sync_logs.append(entry)


def _to_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _resolve_local_album(db: Session, record_id: int, artist: str, title: str) -> dict:
    artist = (artist or '').strip()
    title = (title or '').strip()
    artist_lookup = sanitize_for_local(artist) or artist
    title_lookup = fold_to_ascii(title).strip() or title
    override_folder = get_local_override(db, record_id)
    sanitized_override = (override_folder or '').strip() if override_folder else None
    if sanitized_override:
        sanitized_override = sanitized_override.strip('/\\').replace('\\', '/')
    ctx = {
        'tracks': None,
        'folder': sanitized_override,
        'source': 'auto',
        'override': sanitized_override,
        'override_requested': bool(sanitized_override),
        'override_active': False,
    }
    tracks = None
    chosen_folder = sanitized_override
    if sanitized_override:
        tracks = local_album_track_list_from_folder(sanitized_override)
        if tracks:
            ctx['source'] = 'manual'
            ctx['override_active'] = True
        else:
            ctx['source'] = 'manual'
    if not tracks and artist_lookup and title_lookup:
        tracks = local_album_track_list(artist_lookup, title_lookup)
        if not tracks and (artist_lookup != artist or title_lookup != title):
            tracks = local_album_track_list(artist, title)
        if tracks:
            auto_folder = local_folder_for_album(artist_lookup, title_lookup)
            if not auto_folder:
                auto_folder = local_folder_for_album(artist, title)
            if not auto_folder:
                any_rel = next((t.get('relpath') for t in tracks if t.get('relpath')), None)
                if any_rel:
                    auto_folder = os.path.dirname(any_rel).replace('\\', '/')
            chosen_folder = auto_folder or chosen_folder
            if not ctx['override_active']:
                ctx['source'] = 'auto'
    ctx['tracks'] = tracks
    ctx['folder'] = chosen_folder
    ctx['override_missing'] = ctx['override_requested'] and not ctx['override_active']
    return ctx

# Pydantic models
class ArtworkSearchRequest(BaseModel):
    artist: str
    title: str
    record_id: int

class SetArtworkRequest(BaseModel):
    record_id: int
    artwork_url: str
    source: str

class LocalAlbumMapRequest(BaseModel):
    folder: str



def _build_artwork_headers(url: str) -> dict[str, str]:
    """Return headers for external artwork requests with Discogs support."""
    headers: dict[str, str] = {}
    ua = os.getenv('DISCOGS_USER_AGENT')
    headers['User-Agent'] = ua if ua else 'records-app/1.0 (+local)'
    try:
        parsed = urlparse(url or '')
        host = (parsed.hostname or '').lower()
    except Exception:
        host = ''
    headers.setdefault('Accept', 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8')
    if host.endswith('discogs.com') or host.endswith('discogs.net') or '.discogs.com' in host:
        headers.setdefault('Referer', 'https://www.discogs.com/')
        token = os.getenv('DISCOGS_TOKEN')
        if token:
            headers.setdefault('Authorization', f'Discogs token={token}')
    return headers


def _extract_dimensions_from_url(url: str) -> tuple[int | None, int | None]:
    if not url:
        return None, None
    match = re.search(r'-(\d+)(?:x(\d+))?\.(?:jpe?g|png)$', url)
    if match:
        width = int(match.group(1))
        height = int(match.group(2)) if match.group(2) else width
        return width, height
    return None, None


def _format_bytes(num: int | None) -> str | None:
    if num is None:
        return None
    step = float(num)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if step < 1024 or unit == 'TB':
            if unit == 'B':
                return f"{int(step)} {unit}"
            return f"{step:.1f} {unit}"
        step /= 1024
    return None

class UpdateRecordRequest(BaseModel):
    artist_name: str | None = None
    title: str | None = None
    label: str | None = None
    format: str | None = None
    country: str | None = None
    year: int | None = None
    original_year: int | None = None
    edition_year: int | None = None
    genre: str | None = None
    style: str | None = None

# BluOS models
class BluOSTransportRequest(BaseModel):
    action: str  # play, pause, stop, skip, back, seek
    seek: int | None = None
    track_id: int | None = None

class BluOSVolumeRequest(BaseModel):
    level: int | None = None
    mute: bool | None = None
    db: float | None = None
    abs_db: float | None = None
    tell_slaves: bool | None = None

class BluOSPlayUrlRequest(BaseModel):
    url: str

class BluOSPlayLocalRequest(BaseModel):
    path: str  # relative path under MUSIC_ROOT


def _public_base_url(request: Request) -> str:
    """Return a base URL reachable by BluOS on the LAN.
    Prefers APP_PUBLIC_BASE_URL, falls back to request.base_url.
    """
    base = (os.getenv("APP_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base
    # Fallback: derive from the incoming request
    try:
        return str(request.base_url).rstrip("/")
    except Exception:
        return ""

def update_sync_progress(progress: int, message: str = ""):
    """Update sync progress state"""
    sync_state["progress"] = progress

    structured = False
    log_message = None
    friendly_message = message or ""

    if message and "|" in message:
        parts = message.split("|")
        if len(parts) >= 6 and parts[0] in {"UPDATED", "UNCHANGED"}:
            structured = True
            status_key = "updated" if parts[0] == "UPDATED" else "unchanged"
            discogs_id_raw = parts[1]
            title = parts[2]
            artist = parts[3]
            current = _to_int(parts[4])
            total = _to_int(parts[5])

            discogs_id = _to_int(discogs_id_raw)
            item = {
                "discogs_id": discogs_id if discogs_id is not None else discogs_id_raw,
                "title": title,
                "artist": artist,
                "status": status_key,
                "index": current,
                "total": total,
            }

            items = sync_state.setdefault("recent_items", [])
            items.append(item)
            if len(items) > 40:
                del items[:-40]

            stats = sync_state.setdefault("stats", {"updated": 0, "unchanged": 0})
            stats[status_key] = stats.get(status_key, 0) + 1

            if current is not None:
                sync_state["current"] = current
            if total is not None:
                sync_state["total"] = total
            sync_state["last_item"] = item

            display_parts = [p for p in (artist, title) if p]
            if display_parts:
                display_text = " - ".join(display_parts)
            elif discogs_id_raw:
                display_text = f"Discogs #{discogs_id_raw}"
            else:
                display_text = "Record"
            friendly_message = f"{'Updated' if status_key == 'updated' else 'Unchanged'} • {display_text}"
            log_message = friendly_message

    sync_state["message"] = friendly_message
    if not structured and message:
        log_message = message

    try:
        if log_message:
            add_sync_log(log_message, level="info")
    except Exception:
        pass

async def run_sync(db: Session):
    """Run the sync process"""
    try:
        import asyncio
        import inspect
        
        sync_state.update({
            "status": "in_progress",
            "progress": 0,
            "message": "Starting sync..."
        })
        sync_state["recent_items"] = []
        sync_state["stats"] = {"updated": 0, "unchanged": 0}
        sync_state["current"] = 0
        sync_state["total"] = 0
        sync_state["last_item"] = None
        clear_cancel()
        if inspect.iscoroutinefunction(sync_discogs_collection):
            await sync_discogs_collection(db, update_sync_progress)
        else:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, sync_discogs_collection, db, update_sync_progress)

        # After Discogs, attempt BluOS sync (optional, only if configured)
        try:
            update_sync_progress(sync_state.get("progress", 0), "Syncing BluOS mappings...")
            await loop.run_in_executor(None, sync_bluos_for_collection, db, update_sync_progress)
        except Exception as be:
            logger.warning(f"BluOS sync skipped/failed: {be}")
            
        sync_state.update({
            "status": "completed",
            "progress": 100,
            "message": "Sync completed successfully"
        })
    except Exception as e:
        sync_state.update({
            "status": "error",
            "progress": 0,
            "message": str(e)
        })
        logger.error(f"Sync failed: {e}")

async def run_sync_new_only(db: Session):
    """Sync only newly added items from Discogs collection, stopping early when encountering only known items."""
    try:
        import requests
        sync_state.update({
            "status": "in_progress",
            "progress": 0,
            "message": "Syncing new items…"
        })
        token = os.getenv("DISCOGS_TOKEN", "").strip()
        ua = os.getenv("DISCOGS_USER_AGENT", "").strip()
        username = os.getenv("DISCOGS_USERNAME", "").strip()
        if not (token and ua and username):
            raise RuntimeError("Missing Discogs credentials")
        headers = {"User-Agent": ua, "Authorization": f"Discogs token={token}"}
        base = "https://api.discogs.com"
        session = requests.Session()
        session.headers.update(headers)
        page, per_page = 1, 100
        processed = 0
        known_streak = 0
        total_items = 300  # rough target for progress
        while True:
            r = session.get(f"{base}/users/{username}/collection/folders/0/releases", params={"page": page, "per_page": per_page}, timeout=20)
            r.raise_for_status()
            data = r.json() or {}
            if page == 1:
                total_items = min(1000, data.get("pagination", {}).get("items", total_items))
            releases = data.get("releases", [])
            if not releases:
                break
            found_new_in_page = False
            for it in releases:
                b = it.get("basic_information", {})
                discogs_id = b.get("id")
                if not discogs_id:
                    continue
                exists = db.execute(text("SELECT 1 FROM records WHERE discogs_id = :d"), {"d": discogs_id}).first()
                if exists:
                    known_streak += 1
                    continue
                known_streak = 0
                found_new_in_page = True
                artist = (b.get("artists") or [{}])[0].get("name")
                fmt = ", ".join([f.get("name","") for f in b.get("formats", []) if isinstance(f, dict)])
                rec = {
                    "discogs_id": discogs_id,
                    "title": b.get("title"),
                    "artist_name": artist,
                    "artist_display_name": __import__('re').sub(r"\s*\(\d+\)\s*$", "", artist or "").strip(),
                    "year": b.get("year"),
                    "date_added": it.get("date_added"),
                    "label": ", ".join([l.get("name","") for l in b.get("labels", [])]),
                    "country": b.get("country"),
                    "format": fmt or None,
                    "genre": ", ".join(b.get("genres", []) or []) or None,
                    "style": ", ".join(b.get("styles", []) or []) or None,
                    "mb_release_group_id": None,
                    "cover_art_url": b.get("cover_image") or b.get("thumb"),
                    "cover_thumb_url": None,
                    "artist_id": None
                }
                snapshot, payload_hash = discogs_payload_signature(rec)
                # Enrich artwork
                try:
                    import asyncio
                    loop = asyncio.get_event_loop()
                    rec = loop.run_until_complete(enrich_with_artwork(rec))
                except RuntimeError:
                    pass
                refreshed = rec.pop("_artwork_refreshed", None)
                if refreshed:
                    rec["artwork_synced_at"] = datetime.utcnow()
                else:
                    rec["artwork_synced_at"] = None
                rec["discogs_payload"] = json.dumps(snapshot, sort_keys=True)
                rec["discogs_payload_hash"] = payload_hash
                upsert_record(db, rec)
                db.commit()
                processed += 1
                prog = min(99, int(processed / max(1, total_items) * 100))
                sync_state.update({"progress": prog, "message": f"Synced {processed} new items"})
            # If entire page was known, we can stop early
            if not found_new_in_page and known_streak >= per_page:
                break
            if page >= data.get("pagination", {}).get("pages", page):
                break
            page += 1
        sync_state.update({"status": "completed", "progress": 100, "message": f"New-only sync completed ({processed} items)"})
    except Exception as e:
        sync_state.update({"status": "error", "progress": 0, "message": str(e)})
        logger.error(f"New-only sync failed: {e}")

# Routes
@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main HTML page"""
    html_file = STATIC_DIR / "index.html"
    if html_file.exists():
        return FileResponse(html_file)
    return HTMLResponse("<h1>Records App</h1><p>HTML file not found</p>")

# -----------------------------
# BluOS integration
# -----------------------------

def _bluos_client() -> BluOSClient:
    return BluOSClient()

def _maybe_clear_before_play(c: BluOSClient) -> None:
    """Optionally clear the player's queue before starting new playback.
    Controlled by env var BLUOS_CLEAR_BEFORE_PLAY (default: '1').
    Sleep BLUOS_CLEAR_SLEEP_MS (default: 250) to avoid races.
    """
    try:
        enabled = (os.getenv("BLUOS_CLEAR_BEFORE_PLAY", "1").strip() or "1")
        if enabled not in ("1", "true", "True", "yes", "on"):
            return
        c.clear()
        try:
            ms = int(os.getenv("BLUOS_CLEAR_SLEEP_MS", "250") or "250")
        except Exception:
            ms = 250
        if ms > 0:
            try:
                time.sleep(min(2000, max(0, ms)) / 1000.0)
            except Exception:
                pass
    except Exception:
        # Non-fatal: ignore failures to clear
        pass

@app.get("/bluos/ping")
def bluos_ping():
    try:
        c = _bluos_client()
        root = c.status()
        d = BluOSClient.status_to_dict(root)
        return {"ok": True, "status": d}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/bluos/status")
def bluos_status(
    timeout: int | None = Query(None, ge=1, le=120),
    etag: str | None = Query(None),
):
    """Fetch BluOS status with optional long-poll parameters.

    timeout is forwarded to the player; per spec it should be <= 100 seconds.
    """
    try:
        c = _bluos_client()
        root = c.status(timeout=timeout, etag=etag)
        return BluOSClient.status_to_dict(root)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

@app.post("/bluos/transport")
def bluos_transport(req: BluOSTransportRequest):
    try:
        c = _bluos_client()
        a = (req.action or "").lower()
        last = None
        if a == "play":
            last = c.play(seek=req.seek, track_id=req.track_id)
        elif a == "pause":
            last = c.pause(toggle=True if req.seek is None else False)
        elif a == "stop":
            last = c.stop()
        elif a == "skip":
            last = c.skip()
        elif a == "back":
            last = c.back()
        elif a == "seek":
            if req.seek is None:
                raise HTTPException(status_code=400, detail="seek is required for action=seek")
            last = c.play(seek=req.seek)
        else:
            raise HTTPException(status_code=400, detail="Unknown action")
        # Give the player a brief moment to update state before querying status
        delay_ms = 0
        try:
            delay_ms = int(os.getenv("BLUOS_STATUS_DELAY_MS", "0") or 0)
        except Exception:
            delay_ms = 0
        if delay_ms > 0:
            try:
                time.sleep(min(delay_ms, 1000) / 1000.0)
            except Exception:
                pass
        try:
            status_root = c.status()
            return BluOSClient.status_to_dict(status_root)
        except Exception:
            if last is not None:
                return BluOSClient.status_to_dict(last)
            raise
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

@app.post("/bluos/volume")
def bluos_volume(req: BluOSVolumeRequest):
    try:
        c = _bluos_client()
        root = c.volume(level=req.level, mute=req.mute, db=req.db, abs_db=req.abs_db, tell_slaves=(1 if req.tell_slaves else 0) if req.tell_slaves is not None else None)
        return BluOSClient.status_to_dict(root)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

@app.get("/bluos/presets")
def bluos_presets():
    try:
        c = _bluos_client()
        root = c.presets()
        # Return a normalized list of presets
        items = []
        for p in root.findall("preset"):
            items.append({
                "id": p.attrib.get("id"),
                "name": p.attrib.get("name"),
                "url": p.attrib.get("url"),
                "image": p.attrib.get("image"),
            })
        return {"prid": root.attrib.get("prid"), "presets": items}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

@app.post("/bluos/preset")
def bluos_preset(body: dict):
    try:
        preset_id = str(body.get("id"))
        if not preset_id:
            raise HTTPException(status_code=400, detail="id required")
        c = _bluos_client()
        root = c.load_preset(preset_id)
        return BluOSClient.status_to_dict(root)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

@app.post("/bluos/play-url")
def bluos_play_url(req: BluOSPlayUrlRequest):
    try:
        c = _bluos_client()
        # Clear queue before starting new playback
        _maybe_clear_before_play(c)
        root = c.play_url(req.url)
        return BluOSClient.status_to_dict(root)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

@app.post("/bluos/play-plex/{rating_key}")
def bluos_play_plex(rating_key: str, request: Request):
    """Ask BluOS to play a Plex track by pointing it to our proxy stream URL.
    The player must be able to reach this server URL on the LAN.
    """
    try:
        # Build absolute URL to our plex stream endpoint using public base
        base = _public_base_url(request)
        abs_url = f"{base}/plex/stream/{rating_key}"
        c = _bluos_client()
        # Clear queue before starting new playback
        _maybe_clear_before_play(c)
        root = c.play_url(abs_url)
        return BluOSClient.status_to_dict(root)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

@app.post("/bluos/play-local")
def bluos_play_local(req: BluOSPlayLocalRequest, request: Request):
    """Play a local file on BluOS.

    Prefers invoking the player's own Browse actionURL for the matching track (best metadata on device displays).
    Falls back to LocalMusic: direct URL, then to HTTP stream via our /local/stream if no library root is set.
    """
    try:
        p = (req.path or "").replace("\\", "/")
        c = _bluos_client()
        lm_root = (os.getenv("BLUOS_LIBRARY_ROOT") or "").strip().rstrip("/\\")
        if lm_root:
            import os as _os, difflib as _difflib
            rel = p.lstrip("/\\")
            local_path = f"{lm_root}/{rel}"
            # Try to use Browse actionURL from parent folder for richer metadata
            played_via_action = False
            try:
                remote_folder = _os.path.dirname(local_path).replace("\\", "/")
                broot = c.browse(f"LocalMusic:{remote_folder}")
                target_file = _os.path.basename(local_path).lower()
                # First, attempt filename match via optional 'url' attribute
                for el in broot.iter():
                    if el.tag != 'item':
                        continue
                    t = el.attrib.get('type')
                    if t not in ('audio', 'song', 'track', None):
                        continue
                    file_attr = (el.attrib.get('url') or '').lower()
                    if file_attr and target_file and target_file in file_attr:
                        play_url = el.attrib.get('autoplayURL') or el.attrib.get('autoplayPath') or el.attrib.get('playURL') or el.attrib.get('actionURL') or ''
                        if play_url:
                            _maybe_clear_before_play(c)
                            c.call_action_path(play_url)
                            played_via_action = True
                            break
                # If not found, fuzzy match by normalized title
                if not played_via_action:
                    from .normalize import normalize_title as _norm
                    want = _norm(_os.path.splitext(target_file)[0])
                    best_el = None
                    best_ratio = 0.0
                    for el in broot.iter():
                        if el.tag != 'item':
                            continue
                        t = el.attrib.get('type')
                        if t not in ('audio', 'song', 'track', None):
                            continue
                        txt = (el.attrib.get('text') or '').strip()
                        if not txt:
                            continue
                        r = _difflib.SequenceMatcher(None, want, _norm(txt)).ratio()
                        if r > best_ratio:
                            best_ratio, best_el = r, el
                    if best_el and best_ratio >= float(os.getenv("BLUOS_FUZZY_THRESHOLD", "0.8")):
                        play_url = best_el.attrib.get('autoplayURL') or best_el.attrib.get('autoplayPath') or best_el.attrib.get('playURL') or best_el.attrib.get('actionURL') or ''
                        if play_url:
                            _maybe_clear_before_play(c)
                            c.call_action_path(play_url)
                            played_via_action = True
            except Exception:
                played_via_action = False

            if played_via_action:
                try:
                    root = c.status()
                except Exception:
                    root = None
            else:
                # Fallback to direct LocalMusic URL
                raw_url = f"LocalMusic:{local_path}"
                _maybe_clear_before_play(c)
                root = c.play_url(raw_url)
        else:
            # No library root: fallback to server HTTP stream
            base = _public_base_url(request)
            from urllib.parse import quote as _q
            abs_url = f"{base}/local/stream?p={_q(p, safe='')}"
            _maybe_clear_before_play(c)
            root = c.play_url(abs_url)
        return BluOSClient.status_to_dict(root)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/bluos/resolve/album/{record_id}")
def bluos_resolve_album(record_id: int, db: Session = Depends(get_db)):
    """Resolve a record's album folder on BluOS via /Browse and return track play actions.
    Prefers using local index to derive the exact 'Artist/Album' folder names.
    Requires BLUOS_LIBRARY_ROOT to be set.
    """
    try:
        lm_root = (os.getenv("BLUOS_LIBRARY_ROOT") or "").strip().rstrip("/\\")
        if not lm_root:
            raise HTTPException(status_code=400, detail="BLUOS_LIBRARY_ROOT not configured")

        # Find artist/title for logging
        row = db.execute(text(
            "SELECT id, COALESCE(artist_display_name, artist_name) AS artist, title FROM records WHERE id = :id"
        ), {"id": record_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found")


        ctx = _resolve_local_album(db, record_id, row["artist"] or "", row["title"] or "")
        track_list = ctx.get('tracks') if isinstance(ctx, dict) else None
        if not track_list:
            raise HTTPException(status_code=404, detail="Local album mapping not found; map local files first")
        folder_rel = ctx.get('folder')
        if not folder_rel:
            any_rel = next((t.get('relpath') for t in track_list if t.get('relpath')), None)
            if not any_rel:
                raise HTTPException(status_code=404, detail="No local relpath available")
            folder_rel = os.path.dirname(any_rel).replace("\\", "/")
        remote_folder = f"{lm_root}/{folder_rel}"

        c = _bluos_client()
        key = f"LocalMusic:{remote_folder}"
        root = c.browse(key)

        def _norm(s: str) -> str:
            return normalize_title(s or "")

        items = []
        browse_items: list[tuple[str, str]] = []
        for el in root.iter():
            if el.tag != 'item':
                continue
            t = el.attrib.get('type')
            if t not in ('audio', 'song', 'track', None):
                continue
            text_title = el.attrib.get('text') or ''
            play_url = el.attrib.get('playURL') or el.attrib.get('actionURL') or ''
            if not text_title or not play_url:
                continue
            items.append({'title': text_title, 'playURL': play_url})
            browse_items.append((_norm(text_title), play_url))

        mapping_browse = {k: v for (k, v) in browse_items}
        mapping: dict[str, str] = dict(mapping_browse)
        try:
            import difflib as _difflib
            local_map = local_album_tracks_from_folder(folder_rel) or local_album_tracks(row["artist"] or "", row["title"] or "") or {}
            local_titles = [_norm(k) for k in local_map.keys()]
            thresh = float(os.getenv("BLUOS_FUZZY_THRESHOLD", "0.8"))
            for lt in local_titles:
                if lt in mapping:
                    continue
                best_play = None
                best_ratio = 0.0
                for bt, play in browse_items:
                    r = _difflib.SequenceMatcher(None, lt, bt).ratio()
                    if r > best_ratio:
                        best_ratio, best_play = r, play
                if best_play and best_ratio >= thresh:
                    mapping[lt] = best_play
        except Exception:
            pass

        return {
            'folder': remote_folder,
            'items': items,
            'tracks': mapping,
            'artist': row["artist"],
            'album': row["title"],
            'source': ctx.get('source'),
            'override': ctx.get('override'),
            'override_missing': ctx.get('override_missing'),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


class BluOSActionRequest(BaseModel):
    path: str  # e.g., '/Add?...' or '/Play?...' from browse item


@app.post("/bluos/action")
def bluos_action(req: BluOSActionRequest):
    try:
        c = _bluos_client()
        path = (req.path or "").strip()
        # If the action implies immediate playback, clear first.
        pl = path.lower()
        should_clear = (
            "/play" in pl or
            "playnow" in pl or
            "add-now" in pl or
            "play=1" in pl or
            "now=true" in pl or
            "now=1" in pl
        )
        if should_clear:
            do_on_action = (os.getenv("BLUOS_CLEAR_ON_ACTION", os.getenv("BLUOS_CLEAR_BEFORE_PLAY", "1")) or "1").strip()
            if do_on_action in ("1", "true", "True", "yes", "on"):
                _maybe_clear_before_play(c)
        root = c.call_action_path(path)
        # Try to return a compact status if available
        try:
            return BluOSClient.status_to_dict(root)
        except Exception:
            return {'ok': True}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/bluos/debug/album/{record_id}")
def bluos_debug_album(
    record_id: int,
    title: str | None = None,  # optional: raw title to test
    do_play: bool = False,
    clear: bool = False,
    threshold: float | None = None,
    db: Session = Depends(get_db)
):
    """Debug BluOS resolution and matching for a given album.

    - Lists local titles (normalized) and BluOS browse items (normalized)
    - Shows best match and ratio for each local title
    - Optional: attempts to play the specified title via the resolved playURL
    """
    try:
        lm_root = (os.getenv("BLUOS_LIBRARY_ROOT") or "").strip().rstrip("/\\")
        if not lm_root:
            return {"error": "BLUOS_LIBRARY_ROOT not configured"}

        # Fetch record basic info
        row = db.execute(text(
            "SELECT id, COALESCE(artist_display_name, artist_name) AS artist, title FROM records WHERE id = :id"
        ), {"id": record_id}).mappings().first()
        if not row:
            return {"error": "Record not found"}

        artist = row["artist"] or ""
        album = row["title"] or ""

        ctx = _resolve_local_album(db, record_id, artist, album)
        local_list = ctx.get('tracks') if isinstance(ctx, dict) else None
        if not local_list:
            return {"error": "No local relpath available; map local files first", "override": ctx.get('override'), "override_missing": ctx.get('override_missing')}
        local_tracks = []  # [{raw, norm, index, relpath}]
        for t in local_list:
            raw = (t.get('title') or '').strip()
            local_tracks.append({
                'raw': raw,
                'norm': normalize_title(raw),
                'index': t.get('index'),
                'relpath': t.get('relpath'),
            })

        folder_rel = ctx.get('folder')
        if not folder_rel:
            any_rel = next((t.get('relpath') for t in local_tracks if t.get('relpath')), None)
            if not any_rel:
                return {"error": "No local relpath available", "override": ctx.get('override'), "override_missing": ctx.get('override_missing')}
            folder_rel = os.path.dirname(any_rel).replace('\\', '/')
        remote_folder = f"{lm_root}/{folder_rel}"

        # Browse BluOS folder
        c = _bluos_client()
        pre_status = {}
        pre_status_extra = {}
        try:
            sroot = c.status()
            pre_status = BluOSClient.status_to_dict(sroot)
            def _t(tag: str):
                el = sroot.find(tag)
                return el.text if el is not None else None
            pre_status_extra = {
                "title1": _t("title1"),
                "title": _t("title"),
                "song": _t("song"),
                "state": _t("state"),
            }
        except Exception:
            pre_status = {}
            pre_status_extra = {}
        broot = c.browse(f"LocalMusic:{remote_folder}")

        # Collect browse items
        browse_items = []  # [{raw, norm, playURL}]
        for el in broot.iter():
            if el.tag != 'item':
                continue
            t = el.attrib.get('type')
            if t not in ('audio', 'song', 'track', None):
                continue
            raw_title = el.attrib.get('text') or ''
            play = el.attrib.get('playURL') or el.attrib.get('actionURL') or ''
            if raw_title and play:
                browse_items.append({
                    "raw": raw_title,
                    "norm": normalize_title(raw_title),
                    "playURL": play
                })

        # Build match table (local -> best browse)
        import difflib as _difflib
        used_threshold = float(threshold if threshold is not None else (os.getenv("BLUOS_FUZZY_THRESHOLD", "0.8") or 0.8))
        match_table = []
        b_pairs = [(b["norm"], b["playURL"]) for b in browse_items]
        for lt in local_tracks:
            ln = lt["norm"]
            exact = next((b for b in browse_items if b["norm"] == ln), None)
            if exact:
                match_table.append({
                    "local_raw": lt["raw"],
                    "local_norm": ln,
                    "browse_raw": exact["raw"],
                    "browse_norm": exact["norm"],
                    "ratio": 1.0,
                    "exact": True,
                    "playURL": exact["playURL"],
                })
                continue
            best_play = None
            best_ratio = 0.0
            best_raw = None
            best_norm = None
            for bn, play in b_pairs:
                r = _difflib.SequenceMatcher(None, ln, bn).ratio()
                if r > best_ratio:
                    best_ratio, best_play, best_norm = r, play, bn
            if best_play and best_ratio >= used_threshold:
                # Find raw by norm
                raw = next((b["raw"] for b in browse_items if b["norm"] == best_norm), None)
                match_table.append({
                    "local_raw": lt["raw"],
                    "local_norm": ln,
                    "browse_raw": raw,
                    "browse_norm": best_norm,
                    "ratio": round(best_ratio, 4),
                    "exact": False,
                    "playURL": best_play,
                })
            else:
                match_table.append({
                    "local_raw": lt["raw"],
                    "local_norm": ln,
                    "browse_raw": None,
                    "browse_norm": None,
                    "ratio": round(best_ratio, 4),
                    "exact": False,
                    "playURL": None,
                })

        out = {
            "artist": artist,
            "album": album,
            "folder": remote_folder,
            "threshold": used_threshold,
            "pre_status": pre_status,
            "pre_status_extra": pre_status_extra,
            "local_tracks": local_tracks,
            "browse_items": browse_items,
            "matches": match_table,
            "source": ctx.get('source'),
            "override": ctx.get('override'),
            "override_missing": ctx.get('override_missing'),
        }

        # Optionally attempt to play a specific title
        if do_play and title:
            norm_title = normalize_title(title)
            chosen = next((m for m in match_table if m.get("local_norm") == norm_title and m.get("playURL")), None)
            if not chosen:
                out["play"] = {"error": f"No mapping found for '{title}' (norm '{norm_title}')"}
                return out
            play_path = chosen.get("playURL")
            # Clear if requested
            if clear:
                try:
                    _maybe_clear_before_play(c)
                except Exception:
                    pass
            # Execute action and capture raw response
            try:
                full_url = f"{c.base}{play_path if play_path.startswith('/') else '/' + play_path}"
                r = requests.get(full_url, timeout=10)
                post_status = {}
                post_status_extra = {}
                try:
                    # brief wait for status to update
                    time.sleep(0.25)
                    s2 = c.status()
                    post_status = BluOSClient.status_to_dict(s2)
                    def _t2(tag: str):
                        el = s2.find(tag)
                        return el.text if el is not None else None
                    post_status_extra = {
                        "title1": _t2("title1"),
                        "title": _t2("title"),
                        "song": _t2("song"),
                        "state": _t2("state"),
                    }
                except Exception:
                    post_status = {}
                    post_status_extra = {}
                out["play"] = {
                    "requested_title": title,
                    "normalized": norm_title,
                    "play_path": play_path,
                    "http_status": r.status_code,
                    "response": r.text[:2000],
                    "post_status": post_status,
                    "post_status_extra": post_status_extra,
                }
            except Exception as pe:
                out["play"] = {"error": str(pe)}

        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/bluos/map/{record_id}")
def bluos_map_get(record_id: int, db: Session = Depends(get_db)):
    try:
        row = db.execute(text(
            "SELECT record_id, folder, play_map, matched, match_score, updated_at FROM bluos_maps WHERE record_id = :rid"
        ), {"rid": record_id}).mappings().first()
        if not row:
            return {"found": False}
        # Convert JSONB to dict if needed
        pm = row["play_map"]
        if isinstance(pm, str):
            try:
                pm = __import__('json').loads(pm)
            except Exception:
                pm = {}
        return {
            "found": True,
            "record_id": row["record_id"],
            "folder": row["folder"],
            "play_map": pm,
            "matched": row["matched"],
            "match_score": row["match_score"],
            "updated_at": str(row["updated_at"]),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

@app.get("/records/all")
def get_all_records(
    sort: str = "artist",
    order: str = "asc",
    format: str = "",
    q: str = "",
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Get all records with optional filtering and sorting"""
    try:
        # Sanitize paging
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        result = list_records_all(db, sort, order, format, q, limit=limit, offset=offset)
        return result
    except Exception as e:
        logger.error(f"Error in get_all_records: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ----------------------
# BluOS-only sync control
# ----------------------

def run_bluos_sync(db: Session):
    try:
        sync_state.update({
            "status": "in_progress",
            "progress": 0,
            "message": "BluOS sync starting…"
        })
        sync_bluos_for_collection(db, update_sync_progress)
        sync_state.update({
            "status": "completed",
            "progress": 100,
            "message": "BluOS sync completed"
        })
    except Exception as e:
        sync_state.update({
            "status": "error",
            "progress": 0,
            "message": f"BluOS sync failed: {e}"
        })
        logger.error(f"BluOS sync failed: {e}")


def run_discogs_sync(db: Session):
    try:
        import inspect, asyncio
        sync_state.update({
            "status": "in_progress",
            "progress": 0,
            "message": "Discogs sync starting…"
        })
        if inspect.iscoroutinefunction(sync_discogs_collection):
            # Run coroutine in a new loop if needed
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            loop.run_until_complete(sync_discogs_collection(db, update_sync_progress))
        else:
            sync_discogs_collection(db, update_sync_progress)
        sync_state.update({
            "status": "completed",
            "progress": 100,
            "message": "Discogs sync completed"
        })
    except Exception as e:
        sync_state.update({
            "status": "error",
            "progress": 0,
            "message": f"Discogs sync failed: {e}"
        })
        logger.error(f"Discogs sync failed: {e}")

@app.get("/formats")
def get_formats(db: Session = Depends(get_db)):
    """Get available formats"""
    try:
        query = text("SELECT DISTINCT format FROM records WHERE format IS NOT NULL ORDER BY format")
        result = db.execute(query).fetchall()
        formats = [row[0] for row in result if row[0]]
        return formats
    except Exception as e:
        logger.error(f"Error getting formats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/sync")
async def start_sync(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Start Discogs sync in background"""
    if sync_state["status"] in ("running", "in_progress"):
        return {"message": "Sync already running", "status": sync_state["status"]}
    
    background_tasks.add_task(run_sync, db)
    return {"message": "Sync started", "status": "in_progress"}

@app.post("/sync/new-only")
async def start_sync_new_only(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Start new-only Discogs sync in background"""
    if sync_state["status"] in ("running", "in_progress"):
        return {"message": "Sync already running", "status": sync_state["status"]}
    background_tasks.add_task(run_sync_new_only, db)
    return {"message": "New-only sync started", "status": "in_progress"}

@app.post("/sync/bluos")
def start_sync_bluos(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Start BluOS-only sync in background."""
    if sync_state["status"] in ("running", "in_progress"):
        return {"message": "Sync already running", "status": sync_state["status"]}
    background_tasks.add_task(run_bluos_sync, db)
    return {"message": "BluOS sync started", "status": "in_progress"}

@app.post("/sync/discogs")
def start_sync_discogs(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Start Discogs-only sync in background."""
    if sync_state["status"] in ("running", "in_progress"):
        return {"message": "Sync already running", "status": sync_state["status"]}
    background_tasks.add_task(run_discogs_sync, db)
    return {"message": "Discogs sync started", "status": "in_progress"}

@app.post("/sync/cancel")
def cancel_sync():
    """Request cancellation of ongoing sync (full sync)."""
    try:
        request_cancel()
        sync_state.update({"status": "error", "message": "Sync cancelled by user", "progress": 0})
        return {"status": "cancelled"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/sync/status")
def get_sync_status():
    """Get current sync status"""
    return sync_state

@app.get("/sync/progress")
def get_sync_progress():
    """Get current sync progress - alias for /sync/status"""
    return sync_state

@app.get("/sync/count")
def sync_count(db: Session = Depends(get_db)):
    """Return Discogs collection total and app DB count."""
    try:
        import requests
        token = os.getenv("DISCOGS_TOKEN", "").strip()
        ua = os.getenv("DISCOGS_USER_AGENT", "").strip()
        username = os.getenv("DISCOGS_USERNAME", "").strip()
        if not (token and ua and username):
            raise RuntimeError("Missing Discogs credentials")
        headers = {"User-Agent": ua, "Authorization": f"Discogs token={token}"}
        base = "https://api.discogs.com"
        r = requests.get(f"{base}/users/{username}/collection/folders/0/releases", params={"page": 1, "per_page": 1}, headers=headers, timeout=15)
        r.raise_for_status()
        d = r.json() or {}
        discogs_total = d.get("pagination", {}).get("items", 0)
        app_total = db.execute(text("SELECT COUNT(*) FROM records")).scalar() or 0
        return {"discogs_count": int(discogs_total), "db_count": int(app_total)}
    except Exception as e:
        logger.error(f"sync_count failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ----------------------
# Sync logs endpoints
# ----------------------

@app.get("/sync/logs")
def get_sync_logs(since: int = 0, limit: int = 200):
    try:
        # Return entries with id > since, up to limit
        logs = [e for e in sync_logs if e.get("id", 0) > int(since)]
        if limit > 0:
            logs = logs[: max(1, min(1000, int(limit)))]
        last_id = sync_logs[-1]["id"] if sync_logs else 0
        return {"logs": logs, "last_id": last_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/sync/logs/clear")
def clear_sync_logs():
    try:
        sync_logs.clear()
        global sync_log_next_id
        sync_log_next_id = 1
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ----------------------
# Plex integration
# ----------------------

@app.get("/plex/ping")
def plex_ping():
    """Quick check if Plex config/connection works."""
    try:
        client = PlexClient()
        return {"ok": True, "machine_id": client.machine_id(), "music_section": client.music_section_id()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/plex/album/{record_id}")
def plex_album_lookup(record_id: int, db: Session = Depends(get_db)):
    """Lookup a record in Plex using artist_display_name and album title.
    Returns album match and track links if available.
    """
    try:
        # Fetch record data
        row = db.execute(text("""
            SELECT id, title, artist_name, COALESCE(artist_display_name, artist_name) AS artist_display_name
            FROM records WHERE id = :id
        """), {"id": record_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found")

        artist_display = (row["artist_display_name"] or "").strip()
        title = (row["title"] or "").strip()

        if not artist_display or not title:
            return {"found": False, "reason": "missing artist/title"}

        client = PlexClient()
        album = client.search_album(artist_display, title)
        if not album or not album.get("ratingKey"):
            return {"found": False}

        rating_key = album["ratingKey"]
        tracks = client.album_tracks(rating_key)

        # Build quick mapping by normalized title
        plex_map = {}
        for t in tracks:
            plex_map[normalize_title(t.get("title") or "")] = {
                "ratingKey": t.get("ratingKey"),
                "title": t.get("title"),
                "web_url": client.web_track_url(str(t.get("ratingKey"))),
            }

        # Also return album-level link
        album_payload = {
            "ratingKey": rating_key,
            "web_url": client.web_album_url(str(rating_key)),
            "title": album.get("title"),
            "year": album.get("year"),
        }

        return {
            "found": True,
            "album": album_payload,
            "tracks": plex_map
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Plex album lookup failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ----------------------
# Local files integration
# ----------------------

@app.get("/local/ping")
def local_ping():
    try:
        root = os.getenv("MUSIC_ROOT", "").strip()
        if not root:
            return {"ok": False, "error": "MUSIC_ROOT not set"}
        if not os.path.isdir(root):
            return {"ok": False, "error": f"Not a directory: {root}"}
        return {"ok": True, "root": root}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/local/albums/search")
def local_album_search(q: str = "", limit: int = 50):
    try:
        results = local_search_albums(q, limit)
        return {"items": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/local/scan")
def local_scan_endpoint():
    try:
        count = local_scan()
        return {"ok": True, "albums": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/local/folders")
def local_folders(path: str = ""):
    """List immediate subfolders under MUSIC_ROOT/path for folder mapping.

    Returns: { path, parent, items:[{text, folder, hasChildren}], breadcrumb:[{text, path}] }
    """
    try:
        root = get_music_root()
        if not root:
            raise HTTPException(status_code=400, detail="MUSIC_ROOT not configured")
        import os
        req = (path or "").strip().lstrip("/\\").replace("\\", "/")
        abs_path = os.path.normpath(os.path.join(root, req))
        root_norm = os.path.normpath(root)
        if not abs_path.startswith(root_norm):
            raise HTTPException(status_code=400, detail="Invalid path")
        if not os.path.isdir(abs_path):
            raise HTTPException(status_code=404, detail="Path not found")
        items = []
        try:
            for name in sorted(os.listdir(abs_path)):
                ap = os.path.join(abs_path, name)
                if not os.path.isdir(ap):
                    continue
                rel = os.path.relpath(ap, start=root_norm).replace("\\", "/")
                has_children = any(os.path.isdir(os.path.join(ap, _n)) for _n in os.listdir(ap))
                items.append({"text": name, "folder": rel, "hasChildren": has_children})
        except Exception:
            items = []
        parent = None
        if req:
            parent_rel = os.path.dirname(req.rstrip("/"))
            parent = parent_rel.replace("\\", "/")
        breadcrumb = [{"text": "Root", "path": ""}]
        if req:
            accum = []
            for part in req.split("/"):
                if not part:
                    continue
                accum.append(part)
                breadcrumb.append({"text": part, "path": "/".join(accum)})
        items.sort(key=lambda x: (x.get("text") or "").lower())
        return {"path": req, "parent": parent, "items": items, "breadcrumb": breadcrumb}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
@app.get("/local/scan")
def local_scan_get():
    try:
        count = local_scan()
        return {"ok": True, "albums": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/local/album/{record_id}")
def local_album_lookup(record_id: int, db: Session = Depends(get_db)):
    try:
        row = db.execute(text("""
            SELECT id, title, COALESCE(artist_display_name, artist_name) AS artist
            FROM records WHERE id = :id
        """), {"id": record_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found")
        artist = (row["artist"] or "").strip()
        title = (row["title"] or "").strip()
        if not artist or not title:
            return {"found": False}

        ctx = _resolve_local_album(db, record_id, artist, title)
        folder = ctx.get('folder')
        track_map = None
        if folder:
            track_map = local_album_tracks_from_folder(folder)
        if not track_map:
            artist_lookup = sanitize_for_local(artist) or artist
            title_lookup = fold_to_ascii(title).strip() or title
            track_map = local_album_tracks(artist_lookup, title_lookup) or {}
            if not track_map and (artist_lookup != artist or title_lookup != title):
                track_map = local_album_tracks(artist, title) or {}
        if not track_map:
            return {"found": False, "override": ctx.get('override'), "override_missing": ctx.get('override_missing'), "source": ctx.get('source')}

        mapped = {}
        for norm, info in track_map.items():
            rel = info.get('path') or info.get('relpath') or ''
            mapped[norm] = {
                'source': 'local',
                'ratingKey': rel,
                'web_url': local_stream_url(rel),
            }
        return {
            'found': True,
            'tracks': mapped,
            'folder': folder,
            'source': ctx.get('source'),
            'override': ctx.get('override'),
            'override_missing': ctx.get('override_missing')
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Local album lookup failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/local/album/{record_id}/map")
def local_album_map_set(record_id: int, body: LocalAlbumMapRequest, db: Session = Depends(get_db)):
    folder = (body.folder or '').strip() if body else ''
    if not folder:
        raise HTTPException(status_code=400, detail="Folder is required")
    sanitized = folder.strip('/\\').replace('\\', '/')
    if not sanitized:
        raise HTTPException(status_code=400, detail="Invalid folder")
    rec = db.execute(text(
        "SELECT COALESCE(artist_display_name, artist_name) AS artist, title FROM records WHERE id = :rid"
    ), {"rid": record_id}).mappings().first()
    if not rec:
        raise HTTPException(status_code=404, detail="Record not found")
    tracks = local_album_track_list_from_folder(sanitized)
    if not tracks:
        # ensure library is scanned before failing
        try:
            local_scan()
        except Exception:
            pass
        tracks = local_album_track_list_from_folder(sanitized)
    if not tracks:
        raise HTTPException(status_code=404, detail="Folder not indexed; run local scan")
    set_local_override(db, record_id, sanitized)
    ctx = _resolve_local_album(db, record_id, rec.get('artist') or '', rec.get('title') or '')
    return {
        'success': True,
        'folder': ctx.get('folder') or sanitized,
        'override': ctx.get('override') or sanitized,
        'override_missing': ctx.get('override_missing'),
        'track_count': len(ctx.get('tracks') or []),
        'source': ctx.get('source'),
    }


@app.delete("/local/album/{record_id}/map")
def local_album_map_clear(record_id: int, db: Session = Depends(get_db)):
    delete_local_override(db, record_id)
    return {'success': True}


@app.get("/local/album/{record_id}/metadata")
def local_album_metadata(record_id: int, db: Session = Depends(get_db)):
    """Return a list of per-track metadata for a record's local album, if available.
    Each item: { title, artist, album, albumartist, track, disc, duration, genre, path }
    """
    try:
        row = db.execute(text(
            "SELECT id, title, COALESCE(artist_display_name, artist_name) AS artist FROM records WHERE id = :id"
        ), {"id": record_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found")
        artist = (row["artist"] or "").strip()
        title = (row["title"] or "").strip()
        if not artist or not title:
            raise HTTPException(status_code=400, detail="Missing artist/title on record")
        ctx = _resolve_local_album(db, record_id, artist, title)
        track_list = ctx.get('tracks') if isinstance(ctx, dict) else None
        if not track_list:
            artist_lookup = sanitize_for_local(artist) or artist
            title_lookup = fold_to_ascii(title).strip() or title
            track_list = local_album_track_list(artist_lookup, title_lookup)
            if not track_list and (artist_lookup != artist or title_lookup != title):
                track_list = local_album_track_list(artist, title)
        if not track_list:
            return {"found": False, "tracks": [], "override": ctx.get('override'), "override_missing": ctx.get('override_missing'), "source": ctx.get('source')}
        out = []
        for info in track_list:
            rel = info.get('relpath') or info.get('path')
            if not rel:
                continue
            md = local_read_metadata(rel) or {}
            md['path'] = rel
            out.append(md)
        return {"found": True, "tracks": out, "folder": ctx.get('folder'), "override": ctx.get('override'), "override_missing": ctx.get('override_missing')}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Local album metadata failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _iter_file(path: str, start: int = 0, end: int | None = None):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = None if end is None else (end - start + 1)
        chunk = 64 * 1024
        while True:
            if remaining is not None and remaining <= 0:
                break
            size = chunk if remaining is None else min(chunk, remaining)
            data = f.read(size)
            if not data:
                break
            if remaining is not None:
                remaining -= len(data)
            yield data


@app.get("/local/stream")
def local_stream(request: Request):
    try:
        root = os.getenv("MUSIC_ROOT", "").rstrip("/\\")
        p = request.query_params.get("p") or ""
        if not root:
            raise HTTPException(status_code=400, detail="MUSIC_ROOT not set")
        # ensure safe join
        from urllib.parse import unquote
        rel = unquote(p)
        if rel.startswith("/") or rel.startswith(".."):
            raise HTTPException(status_code=400, detail="Invalid path")
        abs_path = os.path.abspath(os.path.join(root, rel))
        if not abs_path.startswith(os.path.abspath(root) + os.sep):
            raise HTTPException(status_code=400, detail="Path traversal detected")
        if not os.path.isfile(abs_path):
            raise HTTPException(status_code=404, detail="File not found")

        # Content-Type by extension (minimal)
        import mimetypes
        ctype, _ = mimetypes.guess_type(abs_path)
        if not ctype or not ctype.startswith("audio/"):
            ctype = "audio/mpeg"

        file_size = os.path.getsize(abs_path)
        range_header = request.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            rng = range_header.replace("bytes=", "").split("-", 1)
            try:
                start = int(rng[0]) if rng[0] else 0
                end = int(rng[1]) if rng[1] else file_size - 1
            except Exception:
                start, end = 0, file_size - 1
            start = max(0, min(start, file_size - 1))
            end = max(start, min(end, file_size - 1))
            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(end - start + 1),
            }
            return StreamingResponse(_iter_file(abs_path, start, end), status_code=206, media_type=ctype, headers=headers)
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(file_size)}
        return StreamingResponse(_iter_file(abs_path, 0, None), status_code=200, media_type=ctype, headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Local stream failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/local/metadata")
def local_metadata(p: str):
    """Read tags and technical metadata for a local file under MUSIC_ROOT.
    Query param 'p' is the relative path (as shown in /local/album mapping).
    """
    try:
        if not p:
            raise HTTPException(status_code=400, detail="Missing p")
        meta = local_read_metadata(p)
        if not meta:
            raise HTTPException(status_code=404, detail="Metadata not available")
        return meta
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"local_metadata failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------
# Local HLS (ffmpeg)
# ---------------
HLS_BASE = Path(tempfile.gettempdir()) / "hls"
HLS_BASE.mkdir(parents=True, exist_ok=True)


def _safe_abs_from_rel(rel: str) -> Path:
    root = os.getenv("MUSIC_ROOT", "").rstrip("/\\")
    if not root:
        raise RuntimeError("MUSIC_ROOT not set")
    from urllib.parse import unquote
    rel_dec = unquote(rel)
    if rel_dec.startswith("/") or rel_dec.startswith(".."):
        raise RuntimeError("Invalid path")
    abs_path = Path(os.path.abspath(os.path.join(root, rel_dec)))
    root_path = Path(os.path.abspath(root))
    if not str(abs_path).startswith(str(root_path) + os.sep):
        raise RuntimeError("Path traversal detected")
    if not abs_path.is_file():
        raise RuntimeError("File not found")
    return abs_path


@app.get("/local/hls/start")
def local_hls_start(p: str):
    try:
        src = _safe_abs_from_rel(p)
        session = f"s_{uuid.uuid4().hex[:10]}"
        out_dir = HLS_BASE / session
        out_dir.mkdir(parents=True, exist_ok=True)
        playlist = out_dir / "index.m3u8"
        seg_tpl = str(out_dir / "seg_%05d.ts")

        # Spawn ffmpeg to transcode to AAC HLS
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-vn",
            "-c:a", "aac", "-b:a", "256k",
            "-f", "hls",
            "-hls_time", "4",
            "-hls_playlist_type", "event",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", seg_tpl,
            str(playlist),
        ]
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"ffmpeg spawn failed: {e}")

        # Wait briefly for playlist to appear
        t0 = time.time()
        while not playlist.exists() and (time.time() - t0) < 3.0:
            time.sleep(0.1)

        if not playlist.exists():
            # Return an empty live m3u8 to let player retry quickly
            return Response(content="#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n", media_type="application/vnd.apple.mpegurl")

        # Read and rewrite segment URLs
        body_lines = []
        for raw in playlist.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                body_lines.append(raw)
            else:
                # rewrite to our proxy path
                body_lines.append(f"/local/hls/{session}/{line}")
        body = "\n".join(body_lines)
        return Response(content=body, media_type="application/vnd.apple.mpegurl")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Local HLS start failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/local/hls/{session}/{name}")
def local_hls_segment(session: str, name: str):
    try:
        # Security: simple whitelist of names
        if not session.startswith("s_"):
            raise HTTPException(status_code=400, detail="Invalid session")
        if "/" in name or ".." in name:
            raise HTTPException(status_code=400, detail="Invalid name")
        path = HLS_BASE / session / name
        if not path.exists():
            raise HTTPException(status_code=404, detail="Not found")
        # Content type based on extension
        ext = path.suffix.lower()
        if ext == ".m3u8":
            ctype = "application/vnd.apple.mpegurl"
        elif ext == ".ts":
            ctype = "video/mp2t"
        else:
            ctype = "application/octet-stream"
        # Stream file
        def _iter():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        return StreamingResponse(_iter(), media_type=ctype)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Local HLS segment failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _iter_stream(resp):
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if chunk:
            yield chunk


@app.get("/plex/stream/{rating_key}")
def plex_stream_track(rating_key: str, request: Request):
    """Proxy direct audio stream for a Plex track, supporting Range requests.
    Keeps token server-side and avoids CORS issues.
    """
    try:
        client = PlexClient()
        part = client.track_part(str(rating_key))
        if not part or not part.get("key"):
            raise HTTPException(status_code=404, detail="Track media not found")

        part_url = client.part_url(part["key"])  # e.g., /library/parts/... on Plex
        # Some Plex setups expect download=1 on direct part URLs
        if "?" in part_url:
            part_url = f"{part_url}&download=1"
        else:
            part_url = f"{part_url}?download=1"
        headers = {
            "X-Plex-Token": client.token,
            "X-Plex-Product": "records-app",
            "X-Plex-Client-Identifier": "records-app",
        }
        # Forward Range header if present
        range_header = request.headers.get("Range")
        if range_header:
            headers["Range"] = range_header

        # Stream from Plex
        r = requests.get(part_url, headers=headers, stream=True, timeout=30)

        # Prepare response with pass-through headers
        status_code = r.status_code
        # Accept both 200 and 206
        if status_code not in (200, 206):
            logger.warning(f"Plex stream returned status {status_code}")
        content_type = r.headers.get("Content-Type", "audio/mpeg")
        content_length = r.headers.get("Content-Length")
        content_range = r.headers.get("Content-Range")
        accept_ranges = r.headers.get("Accept-Ranges") or "bytes"

        resp = StreamingResponse(_iter_stream(r), media_type=content_type, status_code=status_code)
        if content_length:
            resp.headers["Content-Length"] = content_length
        if content_range:
            resp.headers["Content-Range"] = content_range
        resp.headers["Accept-Ranges"] = accept_ranges
        # Caching disabled for safety
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Plex direct stream failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/plex/hls/start/{rating_key}")
def plex_hls_start(rating_key: str):
    """Start an HLS transcode for a track and proxy the master m3u8,
    rewriting segment URLs to our /plex/hls proxy so the browser stays same-origin.
    """
    try:
        import uuid, re
        client = PlexClient()
        session = f"records-app-{uuid.uuid4().hex[:8]}"
        # Build start.m3u8 URL
        base = client.base_url
        # path expects a full URL to the metadata
        meta_url = f"{base}/library/metadata/{rating_key}"
        params = {
            "path": meta_url,
            "session": session,
            "protocol": "hls",
            "directPlay": 0,
            "directStream": 1,
            "fastSeek": 1,
            "audioCodec": "aac",
            "maxAudioBitrate": 320,
        }
        headers = {
            "X-Plex-Token": client.token,
            "X-Plex-Product": "records-app",
            "X-Plex-Client-Identifier": "records-app",
        }
        u = f"{base}/audio/:/transcode/universal/start.m3u8"
        r = requests.get(u, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        m3u8 = r.text

        # Rewrite absolute or root-relative URLs to our proxy path
        def repl_url(line: str) -> str:
            line = line.strip()
            if not line or line.startswith("#"):
                return line
            # Remove token from query if present
            line = re.sub(r"([?&])X-Plex-Token=[^&]+", r"", line)
            # Normalize base path: ensure it is root-relative
            # Replace http(s)://host:port with empty
            line = re.sub(r"^https?://[^/]+", "", line)
            if not line.startswith("/"):
                # make sure relative becomes absolute for our rewriting
                line = "/" + line
            return f"/plex/hls{line}"

        out_lines = []
        for raw in m3u8.splitlines():
            line = raw.strip()
            if line.startswith("#") or not line:
                out_lines.append(raw)
            else:
                out_lines.append(repl_url(line))
        body = "\n".join(out_lines)
        return Response(content=body, media_type="application/vnd.apple.mpegurl")
    except Exception as e:
        logger.error(f"Plex HLS start failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/plex/hls/{subpath:path}")
def plex_hls_proxy(subpath: str, request: Request):
    """Proxy HLS playlist and segment requests to Plex.
    Restrict to audio transcode paths for safety.
    """
    try:
        if not subpath.startswith("audio/:/transcode/universal/"):
            raise HTTPException(status_code=400, detail="Invalid HLS path")
        client = PlexClient()
        url = f"{client.base_url}/{subpath}"
        headers = {
            "X-Plex-Token": client.token,
            "X-Plex-Product": "records-app",
            "X-Plex-Client-Identifier": "records-app",
        }
        # forward Range for segments
        range_header = request.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
        # forward query
        query = str(request.url.query) if request.url.query else None
        if query:
            url = f"{url}?{query}"
        r = requests.get(url, headers=headers, stream=True, timeout=30)
        ct = r.headers.get("Content-Type", "application/octet-stream")
        status_code = r.status_code
        resp = StreamingResponse(_iter_stream(r), media_type=ct, status_code=status_code)
        # pass important headers
        for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
            v = r.headers.get(h)
            if v:
                resp.headers[h] = v
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Plex HLS proxy failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/sync/partial")
async def sync_partial(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Sync only incomplete and unmodified records in the background."""
    def _task():
        try:
            logger.info("Starting partial sync task")
            sync_state.update({"status": "in_progress", "progress": 0, "message": "Partial sync starting…"})
            # Select candidates
            q = text("""
                SELECT id, discogs_id
                FROM records
                WHERE user_modified_at IS NULL
                  AND (cover_art_url IS NULL OR title IS NULL OR artist_name IS NULL)
                  AND discogs_id IS NOT NULL
                LIMIT 200
            """)
            rows = db.execute(q).fetchall()
            total = len(rows)
            logger.info(f"Partial sync: {total} candidates")
            try:
                from .discogs_client import DiscogsClient
                client = DiscogsClient()
            except Exception as e:
                logger.error(f"Discogs client init failed: {e}")
                sync_state.update({"status": "error", "progress": 0, "message": str(e)})
                return
            done = 0
            for row in rows:
                try:
                    rec = client.fetch_release(int(row.discogs_id))
                    if not rec:
                        continue
                    snapshot, payload_hash = discogs_payload_signature(rec)
                    # Enrich with artwork
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                        existing_data = db.execute(
                            text("""
                                SELECT cover_art_url, cover_thumb_url, artwork_url, mb_release_group_id, original_year, artwork_synced_at
                                FROM records WHERE id = :id
                            """),
                            {"id": row.id}
                        ).mappings().first()
                        rec = loop.run_until_complete(
                            enrich_with_artwork(rec, existing=dict(existing_data) if existing_data else None)
                        )
                    except RuntimeError:
                        pass
                    refreshed = rec.pop("_artwork_refreshed", None)
                    if refreshed:
                        rec["artwork_synced_at"] = datetime.utcnow()
                    else:
                        rec["artwork_synced_at"] = None
                    rec["discogs_payload"] = json.dumps(snapshot, sort_keys=True)
                    rec["discogs_payload_hash"] = payload_hash
                    upsert_record(db, rec)
                    db.execute(text("UPDATE records SET last_synced_at = CURRENT_TIMESTAMP WHERE discogs_id = :d"), {"d": rec.get('discogs_id')})
                    db.commit()
                except Exception as e:
                    db.rollback()
                    logger.warning(f"Partial sync record failed (id={row.id}): {e}")
                    continue
                finally:
                    done += 1
                    if total:
                        prog = min(99, int(done / total * 100))
                        sync_state.update({"progress": prog, "message": f"Partial sync {done} / {total}"})
            logger.info(f"Partial sync finished: {done}/{total}")
            sync_state.update({"status": "completed", "progress": 100, "message": "Partial sync completed"})
        except Exception as e:
            logger.error(f"Partial sync task error: {e}")
            sync_state.update({"status": "error", "progress": 0, "message": str(e)})

    background_tasks.add_task(_task)
    return {"status": "started"}

@app.post("/collection/reset")
def reset_collection(db: Session = Depends(get_db)):
    """Reset/clear the collection"""
    try:
        db.execute(text("DELETE FROM records"))
        db.commit()
        
        global sync_state
        sync_state.update({
            "status": "not_started",
            "progress": 0,
            "message": ""
        })
        
        return {"status": "success", "message": "Collection reset successfully"}
        
    except Exception as e:
        db.rollback()
        logger.error(f"Reset error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sync/reset")
def reset_sync_state():
    """Reset only sync state and logs (does not touch the database)."""
    try:
        sync_state.update({
            "status": "not_started",
            "progress": 0,
            "message": ""
        })
        sync_state["recent_items"] = []
        sync_state["stats"] = {"updated": 0, "unchanged": 0}
        sync_state["current"] = 0
        sync_state["total"] = 0
        sync_state["last_item"] = None
        try:
            sync_logs.clear()
            global sync_log_next_id
            sync_log_next_id = 1
        except Exception:
            pass
        return {"status": "reset"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/artwork/{filename}")
async def serve_artwork(filename: str):
    """Serve artwork files directly"""
    try:
        # Ensure artwork directory exists
        artwork_dir = STATIC_DIR / "artwork"
        artwork_dir.mkdir(parents=True, exist_ok=True)
        
        artwork_path = artwork_dir / filename
        if not artwork_path.exists():
            logger.warning(f"Artwork file not found: {artwork_path}")
            raise HTTPException(status_code=404, detail="Artwork not found")
        
        return FileResponse(artwork_path)
    except Exception as e:
        logger.error(f"Error serving artwork {filename}: {e}")
        raise HTTPException(status_code=404, detail="Artwork not found")

@app.post("/records/{record_id}/update")
def update_record(record_id: int, req: UpdateRecordRequest, db: Session = Depends(get_db)):
    try:
        ok = update_record_fields(db, record_id, req.dict())
        return {"success": ok}
    except Exception as e:
        logger.error(f"Update record failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/records/{record_id}/sync")
def sync_single_record(record_id: int, db: Session = Depends(get_db)):
    """Sync just one record from Discogs by its discogs_id.
    If the record has user modifications, only fill missing fields to avoid overwriting user edits.
    """
    try:
        row = db.execute(text("SELECT * FROM records WHERE id = :id"), {"id": record_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found")
        discogs_id = getattr(row, 'discogs_id', None)
        if not discogs_id:
            raise HTTPException(status_code=400, detail="No Discogs ID for this record")
        from .discogs_client import DiscogsClient
        client = DiscogsClient()
        rec = client.fetch_release(int(discogs_id))
        if not rec:
            raise HTTPException(status_code=502, detail="Discogs fetch failed")
        snapshot, payload_hash = discogs_payload_signature(rec)
        # Enrich with artwork
        import asyncio
        existing_map = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
        try:
            loop = asyncio.get_event_loop()
            rec = loop.run_until_complete(enrich_with_artwork(rec, existing=existing_map))
        except RuntimeError:
            pass
        refreshed = rec.pop("_artwork_refreshed", None)
        if refreshed:
            rec["artwork_synced_at"] = datetime.utcnow()
        else:
            rec["artwork_synced_at"] = None
        rec["discogs_payload"] = json.dumps(snapshot, sort_keys=True)
        rec["discogs_payload_hash"] = payload_hash
        # If user modified, only fill missing fields
        if getattr(row, 'user_modified_at', None):
            filtered = {}
            for k, v in rec.items():
                if k in ('title','artist_name','artist_display_name','year','original_year','edition_year','label','country','format','genre','style','cover_art_url','cover_thumb_url','mb_release_group_id','artwork_url'):
                    existing = getattr(row, k, None)
                    if existing in (None, '', 'null') and v not in (None, '', 'null'):
                        filtered[k] = v
            update_parts = []
            params = {"id": record_id}
            for key, value in filtered.items():
                update_parts.append(f"{key} = :{key}")
                params[key] = value
            if rec.get("artwork_synced_at") is not None:
                update_parts.append("artwork_synced_at = :artwork_synced_at")
                params["artwork_synced_at"] = rec["artwork_synced_at"]
            update_parts.append("discogs_payload_hash = :discogs_payload_hash")
            update_parts.append("discogs_payload = :discogs_payload")
            params["discogs_payload_hash"] = rec["discogs_payload_hash"]
            params["discogs_payload"] = rec["discogs_payload"]
            if update_parts:
                sets = ", ".join(update_parts)
                db.execute(text(f"UPDATE records SET {sets}, last_synced_at = CURRENT_TIMESTAMP WHERE id = :id"), params)
                db.commit()
        else:
            upsert_record(db, rec)
            db.execute(text("UPDATE records SET last_synced_at = CURRENT_TIMESTAMP WHERE discogs_id = :d"), {"d": rec.get('discogs_id')})
            db.commit()

        out = db.execute(text("SELECT * FROM records WHERE id = :id"), {"id": record_id}).mappings().first()
        return {"success": True, "record": dict(out) if out else None}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Single record sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/artwork/search/musicbrainz")
async def search_musicbrainz_artwork(request: ArtworkSearchRequest, db: Session = Depends(get_db)):
    """Search for artwork on MusicBrainz Cover Art Archive"""
    try:
        # Log the incoming request for debugging
        logger.info(f"MusicBrainz search request: artist='{request.artist}', title='{request.title}', record_id={request.record_id}")
        
        search_queries = [
            f'artist:"{request.artist}" AND release:"{request.title}"',
            f'"{request.artist}" AND "{request.title}"',
            f'{request.artist} - {request.title}',
            f'artist:{request.artist} release:{request.title}'
        ]
        
        artworks = []
        
        for query in search_queries:
            if len(artworks) >= 10:
                break
                
            logger.info(f"MusicBrainz search query: {query}")
            
            mb_search_url = "https://musicbrainz.org/ws/2/release"
            params = {
                'query': query,
                'fmt': 'json',
                'limit': 10
            }
            
            response = requests.get(mb_search_url, params=params, timeout=10)
            if not response.ok:
                logger.warning(f"MusicBrainz search failed for query: {query}")
                continue
            
            releases = response.json().get('releases', [])
            logger.info(f"Found {len(releases)} releases for query: {query}")
            
            for release in releases:
                mb_id = release.get('id')
                if not mb_id:
                    continue
                    
                cover_art_url = f"https://coverartarchive.org/release/{mb_id}"
                try:
                    cover_response = requests.get(cover_art_url, timeout=10)
                    if cover_response.ok:
                        cover_data = cover_response.json()
                        images = cover_data.get('images', [])
                        logger.info(f"Found {len(images)} images for release {mb_id}")
                        
                        for image in images:
                            if image.get('front', False) or len(images) == 1:
                                image_url = (image.get('image') or '').replace('http://', 'https://', 1)
                                thumbs = image.get('thumbnails') or {}
                                thumb_map: dict[str, str] = {}
                                thumb_candidates: list[tuple[int, int, str]] = []
                                for key, t_url in thumbs.items():
                                    if not isinstance(t_url, str):
                                        continue
                                    t_url_https = t_url.replace('http://', 'https://', 1)
                                    thumb_map[str(key)] = t_url_https
                                    width_guess: int | None = None
                                    height_guess: int | None = None
                                    if isinstance(key, str) and key.isdigit():
                                        width_guess = height_guess = int(key)
                                    else:
                                        width_guess, height_guess = _extract_dimensions_from_url(t_url_https)
                                    if width_guess:
                                        thumb_candidates.append((width_guess, height_guess or width_guess, t_url_https))

                                width = height = None
                                best_thumb = None
                                if thumb_candidates:
                                    thumb_candidates.sort(key=lambda item: item[0], reverse=True)
                                    width, height, best_thumb = thumb_candidates[0]
                                else:
                                    width, height = _extract_dimensions_from_url(image_url)

                                if not best_thumb:
                                    best_thumb = (thumb_map.get('large') or thumb_map.get('1200') or thumb_map.get('1000') or
                                                   thumb_map.get('500') or thumb_map.get('small') or image_url)

                                size_bytes = None
                                if best_thumb:
                                    try:
                                        head = requests.head(best_thumb, timeout=8, allow_redirects=True, headers=_build_artwork_headers(best_thumb))
                                        cl = head.headers.get('Content-Length')
                                        if cl and cl.isdigit():
                                            size_bytes = int(cl)
                                    except requests.RequestException:
                                        pass

                                size_label = None
                                if width and height:
                                    size_label = f"{width}x{height}"
                                pretty = _format_bytes(size_bytes)
                                if pretty:
                                    size_label = f"{size_label or 'Unknown size'} ({pretty})"

                                artwork_info = {
                                    'url': image_url,
                                    'thumbnail': best_thumb,
                                    'width': width,
                                    'height': height,
                                    'size': size_label or 'Unknown size',
                                    'source': f"MusicBrainz - {release.get('title', 'Unknown')} ({release.get('date', 'Unknown date')})"
                                }
                                artworks.append(artwork_info)
                                
                except Exception as img_error:
                    logger.warning(f"Failed to get cover art for release {mb_id}: {img_error}")
                    continue
        
        logger.info(f"Total artworks found: {len(artworks)}")
        return artworks[:10]
        
    except Exception as e:
        logger.error(f"MusicBrainz artwork search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/artwork/search/discogs")
async def search_discogs_artwork(request: ArtworkSearchRequest, db: Session = Depends(get_db)):
    """Search for artwork on Discogs"""
    try:
        # Log the incoming request for debugging
        logger.info(f"Discogs search request: artist='{request.artist}', title='{request.title}', record_id={request.record_id}")
        
        try:
            from .discogs_client import DiscogsClient
            client = DiscogsClient()
            
            search_patterns = [
                {'artist': request.artist, 'release_title': request.title},
                {'q': f'"{request.artist}" "{request.title}"'},
                {'q': f'{request.artist} - {request.title}'},
                {'q': f'{request.artist} {request.title}'}
            ]
            
            artworks = []
            
            for pattern in search_patterns:
                if len(artworks) >= 10:
                    break
                    
                logger.info(f"Discogs search pattern: {pattern}")
                
                try:
                    search_results = client.search(type='release', **pattern)
                    logger.info(f"Found {len(search_results)} Discogs results")
                    
                    import math
                    import requests as sync_requests
                    for result in search_results[:5]:
                        if hasattr(result, 'images') and result.images:
                            for image in result.images:
                                if image.get('type') == 'primary' or len(result.images) == 1:
                                    width = image.get('width')
                                    height = image.get('height')
                                    url = image.get('uri')
                                    thumb = image.get('uri150')
                                    # Try to read Content-Length via HEAD for human-readable size
                                    size_bytes = None
                                    try:
                                        head = sync_requests.head(url, timeout=5)
                                        cl = head.headers.get('Content-Length')
                                        if cl and cl.isdigit():
                                            size_bytes = int(cl)
                                    except Exception:
                                        pass
                                    if size_bytes is not None:
                                        # human readable
                                        def _fmt_bytes(n):
                                            for unit in ['B','KB','MB','GB']:
                                                if n < 1024 or unit == 'GB':
                                                    return f"{n:.0f} {unit}" if unit=='B' else f"{n/1024:.1f} {unit}"
                                                n /= 1024
                                        size_str = f"{width}x{height} • {_fmt_bytes(size_bytes)}" if width and height else _fmt_bytes(size_bytes)
                                    else:
                                        size_str = f"{width}x{height}" if width and height else None
                                    artwork_info = {
                                        'url': url,
                                        'thumbnail': thumb,
                                        'width': width,
                                        'height': height,
                                        'size': size_str or 'Unknown size',
                                        'source': f"Discogs - {result.title}"
                                    }
                                    artworks.append(artwork_info)
                except Exception as search_error:
                    logger.warning(f"Discogs search failed for pattern {pattern}: {search_error}")
                    continue
            
            logger.info(f"Total Discogs artworks found: {len(artworks)}")
            return artworks[:10]
            
        except ImportError as import_error:
            logger.warning(f"Discogs client not available: {import_error}")
            return []
            
    except Exception as e:
        logger.error(f"Discogs artwork search error: {e}")
        return []

# Add a flexible endpoint that accepts different data formats
@app.post("/artwork/search/musicbrainz-flexible")
async def search_musicbrainz_flexible(data: dict, db: Session = Depends(get_db)):
    """Flexible MusicBrainz search that accepts various data formats"""
    try:
        logger.info(f"Flexible MusicBrainz search received data: {data}")
        
        # Extract fields with fallbacks
        artist = data.get('artist') or data.get('artist_name') or ""
        title = data.get('title') or data.get('album') or ""
        record_id = data.get('record_id') or data.get('id') or 0
        
        if not artist or not title:
            return {"error": "Missing artist or title", "received_data": data}
        
        # Create proper request object
        request = ArtworkSearchRequest(artist=artist, title=title, record_id=record_id)
        
        # Use the existing search function
        return await search_musicbrainz_artwork(request, db)
        
    except Exception as e:
        logger.error(f"Flexible MusicBrainz search error: {e}")
        return {"error": str(e), "received_data": data}

@app.post("/artwork/search/discogs-flexible")
async def search_discogs_flexible(data: dict, db: Session = Depends(get_db)):
    """Flexible Discogs search that accepts various data formats"""
    try:
        logger.info(f"Flexible Discogs search received data: {data}")
        
        # Extract fields with fallbacks
        artist = data.get('artist') or data.get('artist_name') or ""
        title = data.get('title') or data.get('album') or ""
        record_id = data.get('record_id') or data.get('id') or 0
        
        if not artist or not title:
            return {"error": "Missing artist or title", "received_data": data}
        
        # Create proper request object
        request = ArtworkSearchRequest(artist=artist, title=title, record_id=record_id)
        
        # Use the existing search function
        return await search_discogs_artwork(request, db)
        
    except Exception as e:
        logger.error(f"Flexible Discogs search error: {e}")
        return {"error": str(e), "received_data": data}

# Aggregated artwork search used by the frontend editor
@app.post("/artwork/search")
async def search_artwork(data: dict, db: Session = Depends(get_db)):
    try:
        source = (data.get("source") or "auto").lower()
        record_id = int(data.get("record_id") or 0)
        query = (data.get("query") or "").strip()

        # Resolve artist/title primarily from record_id for reliability
        artist = ""
        title = ""
        if record_id:
            row = db.execute(text("SELECT artist_name, title FROM records WHERE id = :id"), {"id": record_id}).fetchone()
            if row:
                artist = (row.artist_name or "").strip()
                title = (row.title or "").strip()

        # Heuristic parse from query if present and any field missing
        if query and (not artist or not title):
            if " - " in query:
                parts = query.split(" - ", 1)
                artist = artist or parts[0].strip()
                title = title or parts[1].strip()
            else:
                # Fallback: use the query for both fields
                artist = artist or query
                title = title or query

        req = ArtworkSearchRequest(artist=artist, title=title, record_id=record_id or 0)

        async def _mb():
            try:
                return await search_musicbrainz_artwork(req, db)
            except Exception:
                return []

        async def _dg():
            try:
                return await search_discogs_artwork(req, db)
            except Exception:
                return []

        results = []
        if source == "musicbrainz":
            results = await _mb()
        elif source == "discogs":
            results = await _dg()
        else:
            # auto: merge and de-duplicate by url
            mb = await _mb()
            dg = await _dg()
            seen = set()
            merged = []
            for item in (mb or []) + (dg or []):
                url = item.get("url") or item.get("thumbnail") or item.get("uri")
                if not url or url in seen:
                    continue
                seen.add(url)
                merged.append(item)
            results = merged

        return {"success": True, "results": results[:20]}
    except Exception as e:
        logger.error(f"Artwork search error: {e}")
        return {"success": False, "error": str(e)}

# Validate a remote image URL (CORS-friendly preview)
@app.post("/artwork/validate-url")
async def validate_artwork_url(data: dict):
    try:
        url = (data.get("url") or "").strip()
        if not url:
            return {"valid": False, "error": "Missing url"}

        headers = _build_artwork_headers(url)
        size = None

        try:
            head = requests.head(url, timeout=8, allow_redirects=True, headers=headers)
            ctype = (head.headers.get("Content-Type") or "")
            if head.status_code in (200, 206) and ctype.startswith("image/"):
                cl = head.headers.get("Content-Length")
                if cl and cl.isdigit():
                    size = int(cl)
                return {"valid": True, "size": size}
        except requests.RequestException:
            pass

        # Fallback: GET a small chunk
        try:
            get_headers = dict(headers)
            get_headers.setdefault("Range", "bytes=0-4095")
            with requests.get(url, timeout=10, stream=True, headers=get_headers) as resp:
                ctype = (resp.headers.get("Content-Type") or "")
                if resp.status_code not in (200, 206) or not ctype.startswith("image/"):
                    return {"valid": False}
                cl = resp.headers.get("Content-Length")
                if cl and cl.isdigit():
                    size = int(cl)
                return {"valid": True, "size": size}
        except requests.RequestException:
            return {"valid": False}
    except Exception as e:
        return {"valid": False, "error": str(e)}

# Update artwork from URL (frontend wrapper around set_artwork)
@app.post("/artwork/update")
async def update_artwork_from_url(payload: dict, db: Session = Depends(get_db)):
    try:
        record_id = int(payload.get("record_id"))
        artwork_url = (payload.get("artwork_url") or "").strip()
        res = await set_artwork(SetArtworkRequest(record_id=record_id, artwork_url=artwork_url, source="url"), db)
        # Return a simplified shape expected by the frontend
        return {
            "success": True,
            "artwork_url": res.get("artwork_full"),
            "artwork_thumb": res.get("artwork_thumb"),
            "artwork_source_url": res.get("artwork_source") or artwork_url
        }
    except HTTPException as http_err:
        logger.error(f"Update artwork error: {http_err.detail or http_err}")
        raise http_err
    except Exception as e:
        logger.error(f"Update artwork error: {e}")
        raise HTTPException(status_code=500, detail=str(e) or 'Unexpected error while updating artwork')

# Upload artwork file
@app.post("/artwork/upload")
async def upload_artwork(record_id: int = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        row = db.execute(text("SELECT id, discogs_id FROM records WHERE id = :id"), {"id": record_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found")

        discogs_id = row.discogs_id or row.id
        artwork_dir = STATIC_DIR / "artwork"
        thumbs_dir = STATIC_DIR / "thumbs"
        artwork_dir.mkdir(parents=True, exist_ok=True)
        thumbs_dir.mkdir(parents=True, exist_ok=True)

        # Load image into PIL
        raw = await file.read()
        img = Image.open(BytesIO(raw))
        if img.mode in ("RGBA", "P"):  # Convert to RGB for JPEG
            img = img.convert("RGB")

        # Save full-size JPEG
        filename = f"{discogs_id}.jpg"
        artwork_path = artwork_dir / filename
        img.save(artwork_path, format="JPEG", quality=92, optimize=True)

        # Create thumbnail (max width 150px, keep aspect)
        thumb = img.copy()
        thumb.thumbnail((150, 150))
        thumb_path = thumbs_dir / f"{discogs_id}_150.jpg"
        thumb.save(thumb_path, format="JPEG", quality=85, optimize=True)

        # Update database URLs
        artwork_url_path = f"/static/artwork/{filename}"
        thumb_url_path = f"/static/thumbs/{discogs_id}_150.jpg"

        update_query = text(
            """
            UPDATE records
            SET cover_art_url = :cover_art_url,
                cover_thumb_url = :cover_thumb_url,
                artwork_url = :source
            WHERE id = :record_id
            """
        )
        db.execute(update_query, {
            "cover_art_url": artwork_url_path,
            "cover_thumb_url": thumb_url_path,
            "source": "uploaded",
            "record_id": record_id,
        })
        db.commit()

        return {"success": True, "artwork_url": artwork_url_path, "artwork_thumb": thumb_url_path, "artwork_source_url": "uploaded"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Upload artwork error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Remove artwork files and clear DB
@app.post("/artwork/remove")
async def remove_artwork_api(payload: dict, db: Session = Depends(get_db)):
    try:
        record_id = int(payload.get("record_id"))
        row = db.execute(text("SELECT id, discogs_id FROM records WHERE id = :id"), {"id": record_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found")

        discogs_id = row.discogs_id or row.id
        artwork_path = STATIC_DIR / "artwork" / f"{discogs_id}.jpg"
        thumb_path = STATIC_DIR / "thumbs" / f"{discogs_id}_150.jpg"

        try:
            if artwork_path.exists():
                artwork_path.unlink()
        except Exception:
            pass
        try:
            if thumb_path.exists():
                thumb_path.unlink()
        except Exception:
            pass

        db.execute(text("""
            UPDATE records
            SET cover_art_url = NULL,
                cover_thumb_url = NULL,
                artwork_url = NULL,
                artwork_source_url = NULL,
                user_modified_at = COALESCE(user_modified_at, CURRENT_TIMESTAMP)
            WHERE id = :record_id
        """), {"record_id": record_id})
        db.commit()

        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Remove artwork error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/artwork/set")

async def set_artwork(request: SetArtworkRequest, db: Session = Depends(get_db)):
    """Set artwork for a record"""
    try:
        query = text("SELECT * FROM records WHERE id = :record_id")
        result = db.execute(query, {"record_id": request.record_id}).fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Record not found")

        artwork_dir = STATIC_DIR / "artwork"
        thumbs_dir = STATIC_DIR / "thumbs"
        artwork_dir.mkdir(exist_ok=True)
        thumbs_dir.mkdir(exist_ok=True)

        discogs_id = result.discogs_id if hasattr(result, 'discogs_id') and result.discogs_id else result.id
        filename = f"{discogs_id}.jpg"
        artwork_path = artwork_dir / filename
        thumb_path = thumbs_dir / f"{discogs_id}_150.jpg"

        logger.info(f"Downloading artwork from: {request.artwork_url}")
        logger.info(f"Saving to: {artwork_path}")

        headers = _build_artwork_headers(request.artwork_url)
        try:
            response = requests.get(request.artwork_url, timeout=30, headers=headers)
        except requests.RequestException as req_err:
            raise HTTPException(status_code=502, detail=f"Error fetching artwork: {req_err}") from req_err

        host = (urlparse(request.artwork_url or '').hostname or '').lower()
        if response.status_code == 403 and 'discogs' in host:
            raise HTTPException(
                status_code=403,
                detail="Discogs denied the artwork download (HTTP 403). Check DISCOGS_USER_AGENT configuration.",
            )
        if not response.ok:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to download artwork (HTTP {response.status_code})",
            )

        raw_bytes = response.content

        # Convert to JPEG and save; generate thumbnail
        try:
            img = Image.open(BytesIO(raw_bytes))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(artwork_path, format="JPEG", quality=92, optimize=True)

            thumb = img.copy()
            thumb.thumbnail((150, 150))
            thumb.save(thumb_path, format="JPEG", quality=85, optimize=True)
        except Exception as pil_err:
            logger.warning(f"PIL processing failed, saving raw bytes as fallback: {pil_err}")
            with open(artwork_path, 'wb') as f:
                f.write(raw_bytes)
            with open(thumb_path, 'wb') as f:
                f.write(raw_bytes)

        logger.info(f"Artwork saved successfully to {artwork_path}")

        update_query = text(
            """
            UPDATE records 
            SET artwork_url = :artwork_url,
                artwork_source_url = :artwork_source_url,
                cover_art_url = :cover_art_url,
                cover_thumb_url = :cover_thumb_url,
                user_modified_at = COALESCE(user_modified_at, CURRENT_TIMESTAMP)
            WHERE id = :record_id
            """
        )

        artwork_url_path = f"/static/artwork/{filename}"
        thumb_url_path = f"/static/thumbs/{filename.replace('.jpg', '_150.jpg')}"

        db.execute(update_query, {
            "artwork_url": artwork_url_path,
            "artwork_source_url": request.artwork_url,
            "cover_art_url": artwork_url_path,
            "cover_thumb_url": thumb_url_path,
            "record_id": request.record_id
        })
        db.commit()

        logger.info(f"Database updated with artwork URLs: {artwork_url_path}, {thumb_url_path}")

        return {
            "success": True,
            "artwork_full": artwork_url_path,
            "artwork_thumb": thumb_url_path,
            "artwork_source": request.artwork_url
        }

    except HTTPException as http_err:
        db.rollback()
        logger.error(f"Set artwork error: {http_err.detail or http_err}")
        raise http_err
    except Exception as e:
        db.rollback()
        logger.error(f"Set artwork error: {e}")
        raise HTTPException(status_code=500, detail=str(e) or "Unexpected error while setting artwork")
@app.get("/debug/record/{record_id}")
async def debug_record(record_id: int, db: Session = Depends(get_db)):
    """Debug endpoint to see record details"""
    try:
        query = text("SELECT * FROM records WHERE id = :record_id")
        result = db.execute(query, {"record_id": record_id}).fetchone()
        
        if not result:
            raise HTTPException(status_code=404, detail="Record not found")
        
        # Get the title/album field safely
        title_field = getattr(result, 'title', None) or getattr(result, 'album', None) or 'Unknown'
        
        return {
            "id": result.id,
            "artist_name": result.artist_name,
            "title": title_field,
            "discogs_id": getattr(result, 'discogs_id', None),
            "artwork_url": getattr(result, 'artwork_url', None),
            "artwork_source_url": getattr(result, 'artwork_source_url', None),
            "cover_art_url": getattr(result, 'cover_art_url', None),
            "cover_thumb_url": getattr(result, 'cover_thumb_url', None),
            "search_query_musicbrainz": f'artist:"{result.artist_name}" AND release:"{title_field}"',
            "search_query_simple": f'{result.artist_name} - {title_field}',
            "all_fields": dict(result._mapping) if hasattr(result, '_mapping') else "Unable to show all fields"
        }
        
    except Exception as e:
        logger.error(f"Debug record error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug/artwork-status")
async def debug_artwork_status(db: Session = Depends(get_db)):
    """Debug endpoint to check artwork status"""
    try:
        # First, let's check what columns actually exist
        columns_query = text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'records'
            ORDER BY ordinal_position
        """)
        columns_result = db.execute(columns_query).fetchall()
        available_columns = [row[0] for row in columns_result]
        
        # Now query with only existing columns
        query = text("""
            SELECT id, artist_name, title, artwork_url, artwork_source_url, cover_art_url, cover_thumb_url 
            FROM records 
            WHERE artwork_url IS NOT NULL OR cover_art_url IS NOT NULL
            LIMIT 10
        """)
        result = db.execute(query).fetchall()
        
        return {
            "available_columns": available_columns,
            "records_with_artwork": len(result),
            "sample_records": [
                {
                    "id": row.id,
                    "artist_name": row.artist_name,
                    "title": row.title,
                    "artwork_url": row.artwork_url,
                    "artwork_source_url": getattr(row, 'artwork_source_url', None),
                    "cover_art_url": row.cover_art_url,
                    "cover_thumb_url": row.cover_thumb_url
                }
                for row in result
            ]
        }
        
    except Exception as e:
        logger.error(f"Debug artwork status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug/test-artwork-search")
async def test_artwork_search(artist: str = "The Beatles", title: str = "Abbey Road", db: Session = Depends(get_db)):
    """Test artwork search functionality"""
    try:
        # Test MusicBrainz search
        request = ArtworkSearchRequest(artist=artist, title=title, record_id=1)
        results = await search_musicbrainz_artwork(request, db)
        
        return {
            "search_query": f"{artist} - {title}",
            "musicbrainz_results": len(results),
            "sample_results": results[:3] if results else "No results found",
            "test_info": "This tests if artwork search is working"
        }
        
    except Exception as e:
        logger.error(f"Test artwork search error: {e}")
        return {"error": str(e)}

@app.get("/debug/records-list")
async def debug_records_list(limit: int = 10, db: Session = Depends(get_db)):
    """Get a list of records with their IDs"""
    try:
        query = text("SELECT id, artist_name, title FROM records ORDER BY id LIMIT :limit")
        result = db.execute(query, {"limit": limit}).fetchall()
        
        return {
            "total_records_shown": len(result),
            "records": [
                {
                    "id": row.id,
                    "artist_name": row.artist_name,
                    "title": row.title
                }
                for row in result
            ]
        }
        
    except Exception as e:
        logger.error(f"Debug records list error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/artwork/download-existing")
async def download_existing_artwork(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Download artwork for records that have URLs but missing files"""
    try:
        # Find records with artwork URLs but missing files
        query = text("""
            SELECT id, discogs_id, cover_art_url, cover_thumb_url, artist_name, title
            FROM records 
            WHERE cover_art_url IS NOT NULL AND cover_art_url != ''
            LIMIT 20
        """)
        result = db.execute(query).fetchall()
        
        def download_artwork_task():
            """Background task to download artwork"""
            downloaded_count = 0
            
            for record in result:
                try:
                    # Extract filename from URL
                    if record.cover_art_url and record.cover_art_url.startswith('/static/artwork/'):
                        filename = record.cover_art_url.split('/')[-1]
                        artwork_path = STATIC_DIR / "artwork" / filename
                        thumb_path = STATIC_DIR / "thumbs" / filename.replace('.jpg', '_150.jpg')
                        
                        # Skip if file already exists
                        if artwork_path.exists():
                            continue
                        
                        # Try to get artwork from Discogs using the record's data
                        # For now, we'll skip this and let users manually set artwork
                        logger.info(f"Would download artwork for: {record.artist_name} - {record.title}")
                        
                except Exception as e:
                    logger.error(f"Error processing record {record.id}: {e}")
                    continue
            
            logger.info(f"Processed {len(result)} records for artwork download")
        
        background_tasks.add_task(download_artwork_task)
        
        return {
            "message": f"Started background task to process {len(result)} records",
            "records_to_process": len(result)
        }
        
    except Exception as e:
        logger.error(f"Download existing artwork error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/artwork/test-download")
async def test_artwork_download_get(record_id: int, db: Session = Depends(get_db)):
    """GET version - Test downloading artwork for a specific record using MusicBrainz"""
    return await test_artwork_download_post(record_id, db)

@app.post("/artwork/test-download")
async def test_artwork_download_post(record_id: int, db: Session = Depends(get_db)):
    """Test downloading artwork for a specific record using MusicBrainz"""
    try:
        # Get the record
        query = text("SELECT * FROM records WHERE id = :record_id")
        result = db.execute(query, {"record_id": record_id}).fetchone()
        
        if not result:
            raise HTTPException(status_code=404, detail="Record not found")
        
        # Ensure directories exist
        artwork_dir = STATIC_DIR / "artwork"
        thumbs_dir = STATIC_DIR / "thumbs"
        artwork_dir.mkdir(parents=True, exist_ok=True)
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        
        # Search for artwork
        search_request = ArtworkSearchRequest(
            artist=result.artist_name,
            title=result.title,
            record_id=record_id
        )
        
        artworks = await search_musicbrainz_artwork(search_request, db)
        
        if not artworks:
            return {
                "success": False,
                "message": "No artwork found",
                "record": {
                    "id": result.id,
                    "artist": result.artist_name,
                    "title": result.title
                }
            }
        
        # Use the first artwork found
        first_artwork = artworks[0]
        
        # Set the artwork
        set_request = SetArtworkRequest(
            record_id=record_id,
            artwork_url=first_artwork['url'],
            source="musicbrainz_auto"
        )
        
        artwork_result = await set_artwork(set_request, db)
        
        return {
            "success": True,
            "message": "Artwork downloaded and set successfully",
            "record": {
                "id": result.id,
                "artist": result.artist_name,
                "title": result.title
            },
            "artwork": artwork_result,
            "source_url": first_artwork['url']
        }
        
    except Exception as e:
        logger.error(f"Test artwork download error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/healthz")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "records-api"}

@app.get("/records/{record_id}/tracklist")
async def get_record_tracklist(record_id: int, db: Session = Depends(get_db)):
    """Get tracklist for a record (from local database or fetch from Discogs)"""
    try:
        # Prefer deriving tracklist from local library if available
        rec_row = db.execute(text("""
            SELECT id, COALESCE(artist_display_name, artist_name) AS artist, title, discogs_id
            FROM records WHERE id = :rid
        """), {"rid": record_id}).mappings().first()
        if not rec_row:
            raise HTTPException(status_code=404, detail="Record not found")

        artist = (rec_row["artist"] or "").strip()
        title = (rec_row["title"] or "").strip()

        ctx = _resolve_local_album(db, record_id, artist, title)
        local_list = ctx.get('tracks') if isinstance(ctx, dict) else None
        if local_list:
            tl = []
            for idx, t in enumerate(local_list, start=1):
                pos = t.get('index') if isinstance(t.get('index'), int) else idx
                dur_s = get_track_duration_seconds(t.get('relpath') or '')
                tl.append({
                    'position': str(pos),
                    'title': t.get('title') or f'Track {pos}',
                    'duration': format_duration(dur_s)
                })
            save_record_tracks(db, record_id, tl)
            return {
                'tracklist': tl,
                'source': 'local_files_manual' if ctx.get('source') == 'manual' else 'local_files',
                'folder': ctx.get('folder'),
                'override': ctx.get('override'),
                'override_missing': ctx.get('override_missing'),
                'message': f"Derived {len(tl)} tracks from local library ({ctx.get('source')})"
            }
        # Fall back to database-stored tracklist if present
        db_tracks = get_record_tracks(db, record_id)
        if db_tracks:
            logger.info(f"Found {len(db_tracks)} tracks in DB for record {record_id}")
            return {
                "tracklist": db_tracks,
                "source": "local",
                "folder": ctx.get('folder'),
                "override": ctx.get('override'),
                "override_missing": ctx.get('override_missing'),
                "message": f"Loaded {len(db_tracks)} tracks from local database"
            }

        # Finally, try Discogs if we have an id
        discogs_id = rec_row.get("discogs_id")
        if not discogs_id:
            return {
                "tracklist": [],
                "source": "none",
                "folder": ctx.get('folder'),
                "override": ctx.get('override'),
                "override_missing": ctx.get('override_missing'),
                "message": "No Discogs ID available and no local files found"
            }
        logger.info(f"Fetching tracklist from Discogs for record {record_id}, Discogs ID {discogs_id}")
        if fetch_and_store_tracklist(db, record_id, discogs_id):
            db_tracks = get_record_tracks(db, record_id)
            return {
                "tracklist": db_tracks,
                "source": "discogs",
                "folder": ctx.get('folder'),
                "override": ctx.get('override'),
                "override_missing": ctx.get('override_missing'),
                "message": f"Fetched and stored {len(db_tracks)} tracks from Discogs"
            }
        return {
            "tracklist": [],
            "source": "error",
            "folder": ctx.get('folder'),
            "override": ctx.get('override'),
            "override_missing": ctx.get('override_missing'),
            "message": "Failed to fetch tracklist from Discogs"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting tracklist for record {record_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/records/{record_id}/tracklist/refresh-local")
def refresh_tracklist_from_local(record_id: int, db: Session = Depends(get_db)):
    """Force-refresh the record's tracklist from local library files, if available."""
    try:
        row = db.execute(text(
            "SELECT COALESCE(artist_display_name, artist_name) AS artist, title FROM records WHERE id = :rid"
        ), {"rid": record_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found")
        artist = (row["artist"] or "").strip()
        title = (row["title"] or "").strip()
        if not artist or not title:
            raise HTTPException(status_code=400, detail="Missing artist/title on record")
        ctx = _resolve_local_album(db, record_id, artist, title)
        local_list = ctx.get('tracks') if isinstance(ctx, dict) else None
        if not local_list:
            return {"success": False, "message": "Local album not found", "override": ctx.get('override'), "override_missing": ctx.get('override_missing')}
        tl = []
        for idx, t in enumerate(local_list, start=1):
            pos = t.get('index') if isinstance(t.get('index'), int) else idx
            dur_s = get_track_duration_seconds(t.get('relpath') or '')
            tl.append({
                'position': str(pos),
                'title': t.get('title') or f'Track {pos}',
                'duration': format_duration(dur_s)
            })
        save_record_tracks(db, record_id, tl)
        return {"success": True, "source": "local_files_manual" if ctx.get('source') == 'manual' else "local_files", "message": f"Refreshed from local ({len(tl)} tracks)", "tracklist": tl, "override": ctx.get('override'), "override_missing": ctx.get('override_missing'), "folder": ctx.get('folder')}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Refresh tracklist from local failed for {record_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/records/{record_id}/tracklist")
async def update_record_tracklist(
    record_id: int,
    tracks_data: dict,
    db: Session = Depends(get_db)
):
    """Manually update tracklist for a record"""
    try:
        # Verify record exists
        record_query = text("SELECT id FROM records WHERE id = :record_id")
        result = db.execute(record_query, {"record_id": record_id}).fetchone()
        
        if not result:
            raise HTTPException(status_code=404, detail="Record not found")
        
        tracklist = tracks_data.get("tracklist", [])
        
        if save_record_tracks(db, record_id, tracklist):
            return {
                "success": True,
                "message": f"Updated tracklist with {len(tracklist)} tracks"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to save tracklist")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating tracklist for record {record_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/records/{record_id}/tracklist")
async def delete_record_tracklist(record_id: int, db: Session = Depends(get_db)):
    """Delete tracklist for a record"""
    try:
        delete_query = text("DELETE FROM tracks WHERE record_id = :record_id")
        result = db.execute(delete_query, {"record_id": record_id})
        db.commit()
        
        return {
            "success": True,
            "message": f"Deleted tracklist for record {record_id}"
        }
        
    except Exception as e:
        logger.error(f"Error deleting tracklist for record {record_id}: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/bluos/local/browse")
def bluos_local_browse(key: str = Query("LocalMusic:", alias="key")):
    """Return BluOS LocalMusic folder structure for mapping dialogs."""
    try:
        browse_key = (key or "").strip() or "LocalMusic:"
        c = _bluos_client()
        broot = c.browse(browse_key)
        items = []
        for el in broot.iter('item'):
            bkey = el.attrib.get('browseKey')
            if not bkey:
                continue
            if not bkey.startswith('LocalMusic:'):
                continue
            item_type = el.attrib.get('type')
            text_val = el.attrib.get('text') or el.attrib.get('title') or el.attrib.get('name') or ''
            folder_rel = bkey[len('LocalMusic:'):].lstrip('/')
            items.append({
                'text': text_val,
                'key': bkey,
                'folder': folder_rel,
                'type': item_type,
                'hasChildren': True,
                'playable': False,
            })
        parent = None
        if browse_key.startswith('LocalMusic:'):
            suffix = browse_key[len('LocalMusic:'):].lstrip('/')
            if suffix:
                parent_parts = suffix.rstrip('/').split('/')
                parent_parts = parent_parts[:-1]
                if parent_parts:
                    parent = 'LocalMusic:/' + '/'.join(parent_parts)
                else:
                    parent = 'LocalMusic:'
        breadcrumb = []
        if browse_key.startswith('LocalMusic:'):
            breadcrumb.append({'text': 'Library', 'key': 'LocalMusic:'})
            suffix = browse_key[len('LocalMusic:'):].lstrip('/')
            if suffix:
                accum = []
                for part in suffix.split('/'):
                    if not part:
                        continue
                    accum.append(part)
                    breadcrumb.append({'text': part, 'key': 'LocalMusic:/' + '/'.join(accum)})
        items.sort(key=lambda x: (x.get('text') or '').lower())
        return {
            'key': browse_key,
            'parent': parent,
            'items': items,
            'breadcrumb': breadcrumb,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
