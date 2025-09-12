from __future__ import annotations
import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, Callable
from sqlalchemy import text
from sqlalchemy.orm import Session

from .bluos import BluOSClient
from .local_media import album_tracks as local_album_tracks
from .plex import normalize_title

logger = logging.getLogger(__name__)


def sync_bluos_for_collection(db: Session, progress_cb: Callable[[int, str], None] | None = None) -> None:
    """Build and store BluOS LocalMusic mappings for records in the DB.

    - Requires BLUOS_HOST and BLUOS_LIBRARY_ROOT
    - For each record with a local album mapping, browse the corresponding LocalMusic folder on BluOS
      and collect track play URLs, matched by normalized track titles.
    - Stores results in 'bluos_maps' table as JSON (title->playURL), with a simple match score.
    """
    root = (os.getenv("BLUOS_LIBRARY_ROOT") or "").strip().rstrip("/\\")
    if not root:
        raise RuntimeError("BLUOS_LIBRARY_ROOT not set; cannot sync BluOS mappings")

    client = BluOSClient()  # may raise if BLUOS_HOST missing

    # Fetch target records
    rows = db.execute(text(
        """
        SELECT id, COALESCE(artist_display_name, artist_name) AS artist, title
        FROM records
        WHERE COALESCE(artist_display_name, artist_name, '') <> ''
          AND COALESCE(title, '') <> ''
        ORDER BY id
        """
    )).mappings().all()

    total = len(rows)
    done = 0
    for row in rows:
        rec_id = int(row["id"]) if row.get("id") is not None else int(row["id"])
        artist = (row.get("artist") or "").strip()
        album = (row.get("title") or "").strip()
        try:
            # Derive the relative album folder based on local index
            local_map = local_album_tracks(artist, album)
            if not local_map:
                # No local mapping; skip
                _store(db, rec_id, None, {}, False, 0)
                done += 1
                if progress_cb and total:
                    prog = min(99, int((done / total) * 100))
                    progress_cb(prog, f"BluOS: {done}/{total}")
                continue

            # Compute folder relative path from any track entry
            any_rel = next(iter(local_map.values())).get("path")
            if not any_rel:
                _store(db, rec_id, None, {}, False, 0)
                done += 1
                if progress_cb and total:
                    prog = min(99, int((done / total) * 100))
                    progress_cb(prog, f"BluOS: {done}/{total}")
                continue

            import os as _os
            folder_rel = _os.path.dirname(any_rel).replace("\\", "/")
            remote_folder = f"{root}/{folder_rel}"

            # Browse this folder on BluOS
            key = f"LocalMusic:{remote_folder}"
            try:
                broot = client.browse(key)
            except Exception as e:
                logger.debug(f"/Browse failed for {remote_folder}: {e}")
                _store(db, rec_id, remote_folder, {}, False, 0)
                done += 1
                if progress_cb and total:
                    prog = min(99, int((done / total) * 100))
                    progress_cb(prog, f"BluOS: {done}/{total}")
                continue

            # Build normalized title -> playURL map from browse items
            mapping: Dict[str, str] = {}
            for el in broot.iter():
                if el.tag != 'item':
                    continue
                t = el.attrib.get('type')
                if t not in ('audio', 'song', 'track', None):
                    continue
                title = el.attrib.get('text') or ''
                play = el.attrib.get('playURL') or el.attrib.get('actionURL') or ''
                if title and play:
                    mapping[normalize_title(title)] = play

            # Score: fraction of locally indexed tracks covered by BluOS mapping
            local_titles = [normalize_title(k) for k in local_map.keys()]
            covered = sum(1 for t in local_titles if t in mapping)
            score = int(100 * covered / max(1, len(local_titles)))
            matched = covered >= max(1, int(0.6 * len(local_titles)))

            _store(db, rec_id, remote_folder, mapping, matched, score)
        except Exception as e:
            logger.debug(f"BluOS map error for record {rec_id}: {e}")
            _store(db, rec_id, None, {}, False, 0)
        finally:
            done += 1
            if progress_cb and total:
                prog = min(99, int((done / total) * 100))
                progress_cb(prog, f"BluOS: {done}/{total}")


def _store(db: Session, record_id: int, folder: str | None, play_map: Dict[str, str], matched: bool, score: int) -> None:
    try:
        payload = json.dumps(play_map)
        db.execute(text(
            """
            INSERT INTO bluos_maps (record_id, folder, play_map, matched, match_score, updated_at)
            VALUES (:rid, :folder, CAST(:p AS JSONB), :m, :s, NOW())
            ON CONFLICT (record_id) DO UPDATE SET
                folder = EXCLUDED.folder,
                play_map = EXCLUDED.play_map,
                matched = EXCLUDED.matched,
                match_score = EXCLUDED.match_score,
                updated_at = NOW()
            """
        ), {"rid": record_id, "folder": folder, "p": payload, "m": matched, "s": score})
        db.commit()
    except Exception:
        db.rollback()
        raise
