# bluos_play_helpers.py
# Utilities to start BluOS playback in a way that preserves rich metadata
# for Last.fm (i.e., by using queue action URLs from /Browse).
#
# Usage from your POST /bluos/play-local handler:
#   result = play_local_track_with_metadata(db, record_id=..., track_title=...)
#   return JSONResponse(result)
#
# Assumes the table "bluos_maps(record_id, folder, play_map JSONB)" exists and
# is populated by your sync job (sync_bluos_for_collection).

from __future__ import annotations
import json
import time
import logging
from typing import Optional, Dict, Any, Tuple
from sqlalchemy import text
from sqlalchemy.orm import Session
from difflib import SequenceMatcher

from .normalize import normalize_title
from .bluos import BluOSClient

logger = logging.getLogger(__name__)


def _choose_action_url(attrs: Dict[str, str], prefer_autoplay: bool = True) -> Optional[str]:
    """
    Given a dict of attributes from a /Browse <item>,
    return the best action path/URL to invoke.
    """
    # Attributes may be present as URL or Path (older/newer firmware variants).
    autoplay = attrs.get("autoplayURL") or attrs.get("autoplayPath")
    play = attrs.get("playURL") or attrs.get("actionURL")
    if prefer_autoplay:
        return autoplay or play
    return play or autoplay


def _best_map_match(play_map: Dict[str, str], target_norm: str) -> Optional[str]:
    """
    Find the best matching action path from a normalized-title -> action map.
    Prefers exact match; otherwise fuzzy match with a threshold.
    """
    if target_norm in play_map:
        return play_map[target_norm]
    # Fuzzy fallback: find best ratio among keys
    best_key, best_ratio = None, 0.0
    for k in play_map.keys():
        r = SequenceMatcher(None, target_norm, k).ratio()
        if r > best_ratio:
            best_key, best_ratio = k, r
    if best_key and best_ratio >= 0.80:
        return play_map[best_key]
    return None


def _load_bluos_map_row(db: Session, record_id: int) -> Tuple[Optional[str], Dict[str, str]]:
    """
    Load (folder, play_map) for a record_id from bluos_maps.
    """
    row = db.execute(
        text("SELECT folder, play_map FROM bluos_maps WHERE record_id = :rid"),
        {"rid": record_id},
    ).mappings().first()
    if not row:
        return None, {}
    folder = row.get("folder")
    play_map = row.get("play_map") or {}
    # If play_map is a JSON string, decode it
    if isinstance(play_map, str):
        try:
            play_map = json.loads(play_map)
        except Exception:
            play_map = {}
    return folder, dict(play_map or {})


def _rebuild_map_from_browse(client: BluOSClient, folder: Optional[str]) -> Dict[str, str]:
    """
    If the stored map is stale/missing, re-browse the LocalMusic folder and build a fresh map.
    Returns normalized-title -> action-path/URL (prefers autoplay).
    """
    if not folder:
        return {}
    # The sync code browsed using key = f"LocalMusic:{remote_folder}"
    broot = client.browse(f"LocalMusic:{folder}")

    new_map: Dict[str, str] = {}
    for el in broot.iter():
        if el.tag != "item":
            continue
        # Filter to track-like items
        t = el.attrib.get("type")
        if t not in (None, "audio", "song", "track"):
            continue
        title = (el.attrib.get("text") or "").strip()
        if not title:
            continue
        action = _choose_action_url(el.attrib, prefer_autoplay=True)
        if not action:
            continue
        new_map[normalize_title(title)] = action
    return new_map


def play_local_track_with_metadata(
    db: Session,
    *,
    record_id: int,
    track_title: str,
    prefer_autoplay: bool = True,
    clear_queue: bool = False,
    settle_seconds: float = 0.5,
) -> Dict[str, Any]:
    """
    Play a local track with **rich metadata** by invoking the BluOS action URL
    obtained via /Browse (autoplayURL/playURL), not /Play?url=...

    Returns a parsed /Status dict, with fields:
      - track, artist, album, is_stream, image, secs, totlen, etc.
    """
    client = BluOSClient()

    # 1) Get stored mapping (folder + normalized-title -> action-path/URL)
    folder, play_map = _load_bluos_map_row(db, record_id)

    # 2) Find the best action URL for the requested track
    target_norm = normalize_title(track_title or "")
    action = _best_map_match(play_map, target_norm)

    # 3) If missing, try to rebuild map from /Browse and retry
    if not action:
        rebuilt = _rebuild_map_from_browse(client, folder)
        if rebuilt:
            play_map = rebuilt
            action = _best_map_match(play_map, target_norm)

    if not action:
        # Still nothing — fail fast with a helpful message
        raise RuntimeError(
            f"No BluOS action URL found for track '{track_title}' (record_id={record_id}). "
            f"Try re-running the BluOS sync to refresh mappings."
        )

    # 4) Optionally clear queue (safer when you want a clean now-playing)
    if clear_queue:
        try:
            client.clear()
        except Exception as e:
            logger.debug(f"/Clear failed (continuing): {e}")

    # 5) Invoke the queue action (this is what produces good metadata)
    client.call_action_path(action)

    # 6) Give the player a moment to enqueue & start playback
    time.sleep(max(0.0, settle_seconds))

    # 7) Fetch status and return a robust dict for Last.fm
    status_root = client.status()
    status = client.status_to_dict(status_root)

    # 8) Guard: if we're still in stream mode, surface that loudly
    # (This would indicate someone still used /Play?url=... upstream.)
    if status.get("is_stream"):
        status["warning"] = (
            "Playback appears to be a raw stream (is_stream=true). "
            "Ensure you're invoking a 'playURL'/'autoplayURL' from /Browse, not /Play?url=..."
        )

    # Helpful echo for debugging which action was used
    status["_action_used"] = action
    status["_folder"] = folder
    return status

