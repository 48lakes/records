from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, Depends, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
import logging
import requests
import os
from urllib.parse import urlparse

# Import your modules
from .db import engine, get_db
from .crud import list_records_all
from .importer import sync_discogs_collection

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Record Collection API")

# Static file paths
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Ensure artwork directories exist at startup
ARTWORK_DIR = STATIC_DIR / "artwork"
THUMBS_DIR = STATIC_DIR / "thumbs"
ARTWORK_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

# Sync state for tracking progress
sync_state = {
    "status": "not_started",  # not_started, running, completed, error
    "progress": 0,
    "message": ""
}

# Pydantic models
class ArtworkSearchRequest(BaseModel):
    artist: str
    title: str
    record_id: int

class SetArtworkRequest(BaseModel):
    record_id: int
    artwork_url: str
    source: str

def update_sync_progress(progress: int, message: str = ""):
    """Update sync progress state"""
    sync_state.update({
        "progress": progress,
        "message": message
    })

async def run_sync(db: Session):
    """Run the sync process"""
    try:
        import asyncio
        import inspect
        
        sync_state.update({
            "status": "running",
            "progress": 0,
            "message": "Starting sync..."
        })
        
        if inspect.iscoroutinefunction(sync_discogs_collection):
            await sync_discogs_collection(db, update_sync_progress)
        else:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, sync_discogs_collection, db, update_sync_progress)
            
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

# Routes
@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main HTML page"""
    html_file = STATIC_DIR / "index.html"
    if html_file.exists():
        return FileResponse(html_file)
    return HTMLResponse("<h1>Records App</h1><p>HTML file not found</p>")

@app.get("/records/all")
def get_all_records(
    sort: str = "artist",
    order: str = "asc",
    format: str = "",
    q: str = "",
    db: Session = Depends(get_db)
):
    """Get all records with optional filtering and sorting"""
    try:
        result = list_records_all(db, sort, order, format, q)
        return result
    except Exception as e:
        logger.error(f"Error in get_all_records: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
    if sync_state["status"] == "running":
        return {"message": "Sync already running", "status": sync_state["status"]}
    
    background_tasks.add_task(run_sync, db)
    return {"message": "Sync started", "status": "running"}

@app.get("/sync/status")
def get_sync_status():
    """Get current sync status"""
    return sync_state

@app.get("/sync/progress")
def get_sync_progress():
    """Get current sync progress - alias for /sync/status"""
    return sync_state

@app.post("/sync/reset")
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
                                artwork_info = {
                                    'url': image.get('image'),
                                    'thumbnail': image.get('thumbnails', {}).get('large') or image.get('thumbnails', {}).get('small'),
                                    'width': None,
                                    'height': None,
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
                    
                    for result in search_results[:5]:
                        if hasattr(result, 'images') and result.images:
                            for image in result.images:
                                if image.get('type') == 'primary' or len(result.images) == 1:
                                    artwork_info = {
                                        'url': image.get('uri'),
                                        'thumbnail': image.get('uri150'),
                                        'width': image.get('width'),
                                        'height': image.get('height'),
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
        
        if not artist or title:
            return {"error": "Missing artist or title", "received_data": data}
        
        # Create proper request object
        request = ArtworkSearchRequest(artist=artist, title=title, record_id=record_id)
        
        # Use the existing search function
        return await search_discogs_artwork(request, db)
        
    except Exception as e:
        logger.error(f"Flexible Discogs search error: {e}")
        return {"error": str(e), "received_data": data}

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
        
        response = requests.get(request.artwork_url, timeout=30)
        if not response.ok:
            raise HTTPException(status_code=400, detail="Failed to download artwork")
        
        with open(artwork_path, 'wb') as f:
            f.write(response.content)
        
        with open(thumb_path, 'wb') as f:
            f.write(response.content)
        
        logger.info(f"Artwork saved successfully to {artwork_path}")
        
        update_query = text("""
            UPDATE records 
            SET artwork_url = :artwork_url,
                cover_art_url = :cover_art_url,
                cover_thumb_url = :cover_thumb_url
            WHERE id = :record_id
        """)
        
        artwork_url_path = f"/static/artwork/{filename}"
        thumb_url_path = f"/static/thumbs/{filename.replace('.jpg', '_150.jpg')}"
        
        db.execute(update_query, {
            "artwork_url": request.artwork_url,
            "cover_art_url": artwork_url_path,
            "cover_thumb_url": thumb_url_path,
            "record_id": request.record_id
        })
        db.commit()
        
        logger.info(f"Database updated with artwork URLs: {artwork_url_path}, {thumb_url_path}")
        
        return {
            "success": True,
            "artwork_full": artwork_url_path,
            "artwork_thumb": thumb_url_path
        }
        
    except Exception as e:
        db.rollback()
        logger.error(f"Set artwork error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
            SELECT id, artist_name, title, artwork_url, cover_art_url, cover_thumb_url 
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

# Add this endpoint after the existing debug endpoints

@app.get("/debug/record-raw/{record_id}")
async def debug_record_raw(record_id: int, db: Session = Depends(get_db)):
    """Debug endpoint to see raw record data as returned by /records/all"""
    try:
        # First, let's see what list_records_all actually returns
        logger.info(f"Getting records data for record_id: {record_id}")
        
        try:
            result = list_records_all(db, "artist", "asc", "", "")
            logger.info(f"list_records_all returned type: {type(result)}")
            logger.info(f"list_records_all keys: {result.keys() if isinstance(result, dict) else 'Not a dict'}")
        except Exception as list_error:
            logger.error(f"Error calling list_records_all: {list_error}")
            
            # Fallback: get record directly from database
            query = text("SELECT * FROM records WHERE id = :record_id")
            db_result = db.execute(query, {"record_id": record_id}).fetchone()
            
            if not db_result:
                raise HTTPException(status_code=404, detail="Record not found in database")
            
            return {
                "record_from_db": dict(db_result._mapping) if hasattr(db_result, '_mapping') else str(db_result),
                "field_analysis": {
                    "has_artist_name": hasattr(db_result, 'artist_name'),
                    "has_title": hasattr(db_result, 'title'),
                    "artist_value": getattr(db_result, 'artist_name', 'MISSING'),
                    "title_value": getattr(db_result, 'title', 'MISSING'),
                    "all_fields": [attr for attr in dir(db_result) if not attr.startswith('_')]
                },
                "error": f"list_records_all failed: {list_error}",
                "source": "direct_db_query"
            }
        
        # Check if result has records
        if not isinstance(result, dict) or 'records' not in result:
            logger.error(f"Unexpected result structure: {result}")
            raise HTTPException(status_code=500, detail=f"Unexpected result structure from list_records_all")
        
        records_list = result.get('records', [])
        logger.info(f"Found {len(records_list)} records")
        
        # Find the specific record
        matching_record = None
        for record in records_list:
            logger.info(f"Checking record: {record.get('id')} == {record_id}")
            if record.get('id') == record_id:
                matching_record = record
                break
        
        if not matching_record:
            # Show a sample of available records for debugging
            sample_records = records_list[:3]
            raise HTTPException(
                status_code=404, 
                detail={
                    "message": "Record not found in records list",
                    "total_records": len(records_list),
                    "sample_records": sample_records,
                    "looking_for_id": record_id
                }
            )
        
        return {
            "record_from_api": matching_record,
            "field_analysis": {
                "has_artist_name": "artist_name" in matching_record,
                "has_title": "title" in matching_record,
                "has_album": "album" in matching_record,
                "artist_value": matching_record.get('artist_name', 'MISSING'),
                "title_value": matching_record.get('title', 'MISSING'),
                "all_keys": list(matching_record.keys()) if isinstance(matching_record, dict) else "Not a dict"
            },
            "source": "list_records_all"
        }
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Debug record raw error: {e}")
        logger.exception("Full exception details:")
        raise HTTPException(status_code=500, detail=f"Debug error: {str(e)}")

# Also add a simpler debug endpoint to check what records actually exist
@app.get("/debug/sample-records")
async def debug_sample_records(db: Session = Depends(get_db)):
    """Get a few sample records to see the data structure"""
    try:
        # Try list_records_all first
        try:
            result = list_records_all(db, "artist", "asc", "", "")
            records_list = result.get('records', [])[:5]  # Get first 5 records
            
            return {
                "source": "list_records_all",
                "total_found": len(result.get('records', [])),
                "sample_records": records_list,
                "data_structure": {
                    "result_type": type(result).__name__,
                    "result_keys": list(result.keys()) if isinstance(result, dict) else "Not a dict",
                    "first_record_keys": list(records_list[0].keys()) if records_list and isinstance(records_list[0], dict) else "No records or not dict"
                }
            }
            
        except Exception as list_error:
            logger.error(f"list_records_all failed: {list_error}")
            
            # Fallback to direct database query
            query = text("SELECT id, artist_name, title, discogs_id FROM records ORDER BY id LIMIT 5")
            db_result = db.execute(query).fetchall()
            
            sample_records = []
            for row in db_result:
                sample_records.append({
                    "id": row.id,
                    "artist_name": row.artist_name,
                    "title": row.title,
                    "discogs_id": row.discogs_id
                })
            
            return {
                "source": "direct_db_query",
                "total_found": len(sample_records),
                "sample_records": sample_records,
                "list_records_all_error": str(list_error)
            }
        
    except Exception as e:
        logger.error(f"Debug sample records error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
