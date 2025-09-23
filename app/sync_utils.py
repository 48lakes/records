from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Tuple


def discogs_payload_signature(rec: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Build a stable snapshot of Discogs-derived fields and return it with a SHA-1 hash."""
    snapshot = {
        "discogs_id": rec.get("discogs_id"),
        "title": rec.get("title"),
        "artist_name": rec.get("artist_name"),
        "artist_display_name": rec.get("artist_display_name"),
        "year": rec.get("year"),
        "edition_year": rec.get("edition_year"),
        "label": rec.get("label"),
        "country": rec.get("country"),
        "format": rec.get("format"),
        "genre": rec.get("genre"),
        "style": rec.get("style"),
        "date_added": rec.get("date_added"),
        "cover_art_url": rec.get("cover_art_url"),
    }
    payload_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    hasher = hashlib.sha1()
    hasher.update(payload_json.encode("utf-8"))
    return snapshot, hasher.hexdigest()
