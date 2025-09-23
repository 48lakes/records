from __future__ import annotations
import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, Callable
from sqlalchemy import text
from sqlalchemy.orm import Session

from .bluos import BluOSClient
from .local_media import album_tracks as local_album_tracks, album_tracks_from_folder, folder_for_album
from .normalize import normalize_title
from .crud import get_local_override

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
            override_folder = get_local_override(db, rec_id)
            sanitized_override = (override_folder or '').strip('/\\').replace('\\', '/') if override_folder else None

            local_map = None
            folder_rel = None
            if sanitized_override:
                local_map = album_tracks_from_folder(sanitized_override)
                if local_map:
                    folder_rel = sanitized_override

            if not local_map:
                local_map = local_album_tracks(artist, album)
                if local_map:
                    folder_rel = folder_rel or folder_for_album(artist, album)
                    if not folder_rel:
                        any_path = next((v.get('path') for v in local_map.values() if v.get('path')), None)
                        if any_path:
                            import os as _os
                            folder_rel = _os.path.dirname(any_path).replace('\\', '/')
                elif sanitized_override:
                    folder_rel = sanitized_override

            if not local_map:
                remote_folder = f"{root}/{folder_rel}" if folder_rel else None
                _store(db, rec_id, remote_folder, {}, False, 0)
                done += 1
                if progress_cb and total:
                    prog = min(99, int((done / total) * 100))
                    progress_cb(prog, f"BluOS: {done}/{total}")
                continue

            any_rel = next((v.get('path') for v in local_map.values() if v.get('path')), None)
            if not any_rel:
                remote_folder = f"{root}/{folder_rel}" if folder_rel else None
                _store(db, rec_id, remote_folder, {}, False, 0)
                done += 1
                if progress_cb and total:
                    prog = min(99, int((done / total) * 100))
                    progress_cb(prog, f"BluOS: {done}/{total}")
                continue

            import os as _os
            if not folder_rel:
                folder_rel = _os.path.dirname(any_rel).replace('\\', '/')
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

            # Build normalized browse items list
            browse_items: list[tuple[str, str]] = []  # (norm_title, playURL)
            for el in broot.iter():
                if el.tag != 'item':
                    continue
                t = el.attrib.get('type')
                if t not in ('audio', 'song', 'track', None):
                    continue
                title = el.attrib.get('text') or ''
                play = el.attrib.get('autoplayURL') or el.attrib.get('autoplayPath') or el.attrib.get('playURL') or el.attrib.get('actionURL') or ''
                if title and play:
                    browse_items.append((normalize_title(title), play))

            # Exact map from browse titles
            mapping_browse: Dict[str, str] = {k: v for (k, v) in browse_items}

            # Fuzzy-align to local normalized titles
            import difflib as _difflib
            local_titles = [normalize_title(k) for k in local_map.keys()]
            final_map: Dict[str, str] = {}
            matched_count = 0
            for lt in local_titles:
                if lt in mapping_browse:
                    final_map[lt] = mapping_browse[lt]
                    matched_count += 1
                    continue
                # Fallback: best fuzzy match among browse items
                best_play = None
                best_ratio = 0.0
                for bt, play in browse_items:
                    r = _difflib.SequenceMatcher(None, lt, bt).ratio()
                    if r > best_ratio:
                        best_ratio, best_play = r, play
                if best_play and best_ratio >= float(os.getenv("BLUOS_FUZZY_THRESHOLD", "0.8")):
                    final_map[lt] = best_play
                    matched_count += 1

            score = int(100 * matched_count / max(1, len(local_titles)))
            matched = matched_count >= max(1, int(0.6 * len(local_titles)))

            _store(db, rec_id, remote_folder, final_map, matched, score)
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
