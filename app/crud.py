from __future__ import annotations
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import text

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
    sql = f"""        SELECT id, artist_name, title AS album, year, format, label, country, genre, style,
               cover_art_url AS artwork_full,
               COALESCE(cover_thumb_url, cover_art_url, '') AS artwork_thumb
        FROM records
        WHERE {' AND '.join(where)}
        ORDER BY {col} {ord_sql}, id ASC
    """
    items = [dict(row) for row in db.execute(text(sql), params).mappings().all()]
    return {"total": len(items), "items": items}

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
