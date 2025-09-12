from __future__ import annotations
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging

# Add this logger setup
logger = logging.getLogger(__name__)

def list_records_all(db: Session, sort_by="artist", order="asc", format_filter: Optional[str]=None, q: Optional[str]=None,
                     limit: Optional[int] = None, offset: Optional[int] = None) -> Dict[str, Any]:
    sort_map = {"artist": "artist_name", "album": "title", "year": "year"}
    ord_sql = "ASC" if order.lower() == "asc" else "DESC"
    where = ["TRUE"]
    params = {}
    if format_filter:
        # Support CSV multi-select; match if format string contains the value (e.g., 'Vinyl', 'CD', 'Cassette')
        fmts = [f.strip() for f in str(format_filter).split(',') if f and f.strip()]
        if fmts:
            conds = []
            for idx, val in enumerate(fmts):
                key = f"fmt{idx}"
                params[key] = f"%{val}%"
                conds.append(f"format ILIKE :{key}")
            where.append("(" + " OR ".join(conds) + ")")
    if q:
        where.append("(LOWER(artist_name) LIKE :q OR LOWER(title) LIKE :q)")
        params["q"] = f"%{q.lower()}%"
    # Build ORDER BY with sensible tie-breakers
    if sort_by == "artist":
        order_by = (
            f"COALESCE(artist_display_name, artist_name) {ord_sql}, "
            f"COALESCE(original_year, year) {ord_sql}, "
            f"COALESCE(edition_year, year) {ord_sql}, "
            f"title {ord_sql}, id ASC"
        )
    elif sort_by == "album":
        order_by = (
            f"title {ord_sql}, "
            f"COALESCE(artist_display_name, artist_name) {ord_sql}, "
            f"COALESCE(edition_year, year) {ord_sql}, id ASC"
        )
    elif sort_by == "year":
        order_by = (
            f"COALESCE(edition_year, year) {ord_sql}, "
            f"COALESCE(artist_display_name, artist_name) {ord_sql}, title {ord_sql}, id ASC"
        )
    else:
        col = sort_map.get(sort_by, "artist_name")
        order_by = f"{col} {ord_sql}, id ASC"

    base_select = f"""        SELECT id, discogs_id, title, artist_name, COALESCE(artist_display_name, artist_name) AS artist_display_name,
               year, original_year, edition_year, label, country, format, genre, style, date_added,
               cover_art_url,
               cover_thumb_url,
               artwork_url,
               mb_release_group_id,
               artist_id
        FROM records
        WHERE {' AND '.join(where)}
    """

    # Total count
    total_sql = f"SELECT COUNT(*) AS cnt FROM records WHERE {' AND '.join(where)}"
    total = db.execute(text(total_sql), params).scalar() or 0

    # Page with ORDER BY / LIMIT / OFFSET
    page_sql = base_select + f" ORDER BY {order_by}"
    if limit is not None:
        page_sql += " LIMIT :limit"
        params["limit"] = int(limit)
    if offset is not None:
        page_sql += " OFFSET :offset"
        params["offset"] = int(offset)

    items = [dict(row) for row in db.execute(text(page_sql), params).mappings().all()]
    return {"total": int(total), "records": items, "limit": limit, "offset": offset}

def format_counts(db: Session) -> List[Dict[str, Any]]:
    sql = "SELECT format, COUNT(*) AS count FROM records GROUP BY format ORDER BY format NULLS LAST"
    return [dict(row) for row in db.execute(text(sql)).mappings().all()]

def upsert_record(db: Session, rec: Dict[str, Any]):
    sql = text(
        """
        INSERT INTO records (
            discogs_id, title, artist_name, artist_display_name, year, original_year, edition_year, label, country, format, genre, style, date_added,
            mb_release_group_id, cover_art_url, cover_thumb_url, artist_id
        )
        VALUES (
            :discogs_id, :title, :artist_name, :artist_display_name, :year, :original_year, :edition_year, :label, :country, :format, :genre, :style, :date_added,
            :mb_release_group_id, :cover_art_url, :cover_thumb_url, :artist_id
        )
        ON CONFLICT (discogs_id) DO UPDATE SET
            title = EXCLUDED.title,
            artist_name = EXCLUDED.artist_name,
            artist_display_name = COALESCE(EXCLUDED.artist_display_name, records.artist_display_name),
            year = EXCLUDED.year,
            original_year = COALESCE(EXCLUDED.original_year, records.original_year),
            edition_year = COALESCE(EXCLUDED.edition_year, records.edition_year),
            label = EXCLUDED.label,
            country = EXCLUDED.country,
            format = EXCLUDED.format,
            genre = EXCLUDED.genre,
            style = EXCLUDED.style,
            date_added = COALESCE(EXCLUDED.date_added, records.date_added),
            mb_release_group_id = COALESCE(EXCLUDED.mb_release_group_id, records.mb_release_group_id),
            cover_art_url = COALESCE(EXCLUDED.cover_art_url, records.cover_art_url),
            cover_thumb_url = COALESCE(EXCLUDED.cover_thumb_url, records.cover_thumb_url),
            artist_id = COALESCE(EXCLUDED.artist_id, records.artist_id),
            last_synced_at = CURRENT_TIMESTAMP
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

def update_record_fields(db: Session, record_id: int, fields: Dict[str, Any]) -> bool:
    """Update whitelisted fields on a record."""
    allowed = [
        'artist_name', 'artist_display_name', 'title', 'label', 'format', 'country',
        'year', 'original_year', 'edition_year', 'genre', 'style'
    ]
    updates = []
    params = {'id': record_id}
    # If artist_name provided, compute display name automatically when not explicitly set
    artist_name_in = fields.get('artist_name')
    if artist_name_in is not None and not fields.get('artist_display_name'):
        import re
        fields['artist_display_name'] = re.sub(r"\s*\(\d+\)\s*$", "", str(artist_name_in or '')).strip()
    for k in allowed:
        if k in fields and fields[k] is not None:
            updates.append(f"{k} = :{k}")
            params[k] = fields[k]
    if not updates:
        return True
    # Always mark as user-modified on any field change
    updates.append("user_modified_at = CURRENT_TIMESTAMP")
    sql = text(f"UPDATE records SET {', '.join(updates)} WHERE id = :id")
    db.execute(sql, params)
    db.commit()
    return True
