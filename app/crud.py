from __future__ import annotations
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging

# Add this logger setup
logger = logging.getLogger(__name__)

def list_records_all(db: Session, sort_by="artist", order="asc", format_filter: Optional[str]=None, q: Optional[str]=None) -> Dict[str, Any]:
    sort_map = {"artist":"artist_name","album":"title","year":"year"}
    col = sort_map.get(sort_by, "artist_name")
    ord_sql = "ASC" if order.lower()=="asc" else "DESC"
    where = ["TRUE"]
    params = {}
    if format_filter:
        where.append("format = :fmt")
        params["fmt"] = format_filter
    if q:
        where.append("(LOWER(artist_name) LIKE :q OR LOWER(title) LIKE :q)")
        params["q"] = f"%{q.lower()}%"
    sql = f"""        SELECT id, discogs_id, title, artist_name, year, label, country, format, genre, style,
               cover_art_url,
               cover_thumb_url,
               artwork_url,
               mb_release_group_id,
               artist_id
        FROM records
        WHERE {' AND '.join(where)}
        ORDER BY {col} {ord_sql}, id ASC
    """
    items = [dict(row) for row in db.execute(text(sql), params).mappings().all()]
    return {"total": len(items), "records": items}

def format_counts(db: Session) -> List[Dict[str, Any]]:
    sql = "SELECT format, COUNT(*) AS count FROM records GROUP BY format ORDER BY format NULLS LAST"
    return [dict(row) for row in db.execute(text(sql)).mappings().all()]

def upsert_record(db: Session, rec: Dict[str, Any]):
    sql = text(
        """
        INSERT INTO records (
            discogs_id, title, artist_name, year, label, country, format, genre, style,
            mb_release_group_id, cover_art_url, cover_thumb_url, artist_id
        )
        VALUES (
            :discogs_id, :title, :artist_name, :year, :label, :country, :format, :genre, :style,
            :mb_release_group_id, :cover_art_url, :cover_thumb_url, :artist_id
        )
        ON CONFLICT (discogs_id) DO UPDATE SET
            title = EXCLUDED.title,
            artist_name = EXCLUDED.artist_name,
            year = EXCLUDED.year,
            label = EXCLUDED.label,
            country = EXCLUDED.country,
            format = EXCLUDED.format,
            genre = EXCLUDED.genre,
            style = EXCLUDED.style,
            mb_release_group_id = COALESCE(EXCLUDED.mb_release_group_id, records.mb_release_group_id),
            cover_art_url = COALESCE(EXCLUDED.cover_art_url, records.cover_art_url),
            cover_thumb_url = COALESCE(EXCLUDED.cover_thumb_url, records.cover_thumb_url),
            artist_id = COALESCE(EXCLUDED.artist_id, records.artist_id)
        """
    )
    db.execute(sql, rec)

def get_record_by_id(db: Session, rec_id: int) -> Optional[Dict[str, Any]]:
    sql = text(
        """
        SELECT id, discogs_id, artist_name, title AS album, year, format, label, country, genre, style,
               mb_release_group_id,
               cover_art_url AS artwork_full,
               COALESCE(cover_thumb_url, cover_art_url) AS artwork_thumb
        FROM records
        WHERE id = :id
        """
    )
    row = db.execute(sql, {"id": rec_id}).mappings().first()
    return dict(row) if row else None

from .models import Track
from sqlalchemy import text, delete

def get_record_tracks(db: Session, record_id: int):
    """Get all tracks for a record"""
    try:
        query = text("""SELECT position, title, duration, track_order
            FROM tracks 
            WHERE record_id = :record_id
            ORDER BY track_order ASC, position ASC""")
        
        result = db.execute(query, {"record_id": record_id}).fetchall()
        return [dict(row._mapping) for row in result]
        
    except Exception as e:
        logger.error(f"Error getting tracks for record {record_id}: {e}")
        return []

def save_record_tracks(db: Session, record_id: int, tracks_data: list):
    """Save tracks for a record (replaces existing tracks)"""
    try:
        # Delete existing tracks
        delete_query = text("DELETE FROM tracks WHERE record_id = :record_id")
        db.execute(delete_query, {"record_id": record_id})
        
        # Insert new tracks
        for index, track in enumerate(tracks_data):
            insert_query = text("""INSERT INTO tracks (record_id, position, title, duration, track_order)
                VALUES (:record_id, :position, :title, :duration, :track_order)""")
            
            db.execute(insert_query, {
                "record_id": record_id,
                "position": track.get("position", ""),
                "title": track.get("title", ""),
                "duration": track.get("duration", ""),
                "track_order": index + 1
            })
        
        db.commit()
        logger.info(f"Saved {len(tracks_data)} tracks for record {record_id}")
        return True
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error saving tracks for record {record_id}: {e}")
        return False

def fetch_and_store_tracklist(db: Session, record_id: int, discogs_id: int):
    """Fetch tracklist from Discogs API and store locally"""
    try:
        import requests
        import os
        
        discogs_token = os.getenv("DISCOGS_TOKEN")
        if not discogs_token:
            logger.warning("No Discogs token available")
            return False
        
        headers = {
            "Authorization": f"Discogs token={discogs_token}",
            "User-Agent": os.getenv("DISCOGS_USER_AGENT", "records-app/1.0")
        }
        
        response = requests.get(
            f"https://api.discogs.com/releases/{discogs_id}",
            headers=headers,
            timeout=10
        )
        
        if response.status_code != 200:
            logger.error(f"Discogs API error: {response.status_code}")
            return False
        
        data = response.json()
        tracklist = data.get("tracklist", [])
        
        if not tracklist:
            logger.info(f"No tracklist found for Discogs ID {discogs_id}")
            return True  # Not an error, just no tracks
        
        # Save to database
        tracks_data = []
        for track in tracklist:
            tracks_data.append({
                "position": track.get("position", ""),
                "title": track.get("title", ""),
                "duration": track.get("duration", "")
            })
        
        return save_record_tracks(db, record_id, tracks_data)
        
    except Exception as e:
        logger.error(f"Error fetching and storing tracklist for record {record_id}: {e}")
        return False
