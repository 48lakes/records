from __future__ import annotations
import os
import logging
from typing import Dict, List, Optional, Tuple
import os
from urllib.parse import quote

logger = logging.getLogger(__name__)


# In-memory index: (norm_artist, norm_album) -> list of tracks
# Track item: { 'title': str, 'relpath': str, 'index': int|None }
LOCAL_INDEX: Dict[Tuple[str, str], List[Dict]] = {}
MUSIC_ROOT: Optional[str] = None


def set_music_root(root: Optional[str]):
    global MUSIC_ROOT
    MUSIC_ROOT = (root or "").rstrip("\\/") or None


def get_music_root() -> Optional[str]:
    if MUSIC_ROOT:
        return MUSIC_ROOT
    root = os.getenv("MUSIC_ROOT", "").strip()
    return root or None


def normalize(s: str) -> str:
    try:
        import re
        s = (s or "").lower().strip()
        s = s.replace("_", " ")
        # unify separators: hyphen, colon, en/em dash
        s = re.sub(r"[\-:\u2013\u2014]+", " ", s)
        # remove disambiguation and parentheses
        s = re.sub(r"\s*\(.*?\)\s*", " ", s)
        s = re.sub(r"[^a-z0-9\s]", "", s)
        s = re.sub(r"\s+", " ", s)
        return s.strip()
    except Exception:
        return (s or "").lower().strip()


def is_audio(path: str) -> bool:
    exts = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac"}
    _, ext = os.path.splitext(path)
    return ext.lower() in exts


def parse_track_filename(name: str) -> Tuple[Optional[int], str]:
    """Extract a track number and title from common patterns like '01 - Title.ext' or '1 Title'."""
    import re
    base, _ = os.path.splitext(name)
    m = re.match(r"^\s*(\d{1,3})\s*[-_. ]\s*(.+)$", base)
    if m:
        try:
            return int(m.group(1)), m.group(2).strip()
        except Exception:
            return None, base.strip()
    return None, base.strip()


def scan_library(root: Optional[str] = None) -> int:
    """Scan MUSIC_ROOT for Artist/Album/Track.ext and build an in-memory index.
    Returns number of indexed albums.
    """
    root = root or get_music_root()
    if not root or not os.path.isdir(root):
        raise RuntimeError("MUSIC_ROOT not set or not a directory")

    logger.info(f"Scanning music library at: {root}")
    index: Dict[Tuple[str, str], List[Dict]] = {}
    # Walk two levels: Artist/Album then files
    for artist in sorted(os.listdir(root)):
        a_path = os.path.join(root, artist)
        if not os.path.isdir(a_path):
            continue
        for album in sorted(os.listdir(a_path)):
            al_path = os.path.join(a_path, album)
            if not os.path.isdir(al_path):
                continue
            key = (normalize(artist), normalize(album))
            tracks: List[Dict] = []
            try:
                for fname in sorted(os.listdir(al_path)):
                    if not is_audio(fname):
                        continue
                    tn, title = parse_track_filename(fname)
                    rel = os.path.join(artist, album, fname)
                    tracks.append({"title": title, "relpath": rel.replace("\\", "/"), "index": tn})
            except Exception as e:
                logger.warning(f"Error reading {al_path}: {e}")
                continue
            if tracks:
                # sort by explicit index then by filename
                tracks.sort(key=lambda t: (t["index"] if isinstance(t["index"], int) else 9999, t["title"]))
                index[key] = tracks

    # Also support flat album folders in the form "Artist-Album-<quality>"
    QUALITY_TOKENS = {
        "FLAC", "MP3", "AAC", "ALAC", "OGG", "OPUS", "WEB", "VINYL", "CD", "LP",
        "24BIT", "24BIT96KHZ", "96KHZ", "HIRES", "LOSSLESS", "REMASTER", "DELUXE"
    }

    def parse_album_dir(dir_name: str) -> Optional[Tuple[str, str]]:
        name = (dir_name or "").strip().replace("_", " ")
        if "-" not in name:
            return None
        parts = [p.strip() for p in name.split("-") if p.strip()]
        if len(parts) < 2:
            return None
        # Drop trailing quality tokens
        while len(parts) > 2 and parts[-1].upper() in QUALITY_TOKENS:
            parts.pop()
        artist = parts[0]
        album = "-".join(parts[1:]).strip()
        if not artist or not album:
            return None
        return artist, album

    for folder in sorted(os.listdir(root)):
        fpath = os.path.join(root, folder)
        if not os.path.isdir(fpath):
            continue
        parsed = parse_album_dir(folder)
        if not parsed:
            continue
        artist, album = parsed
        key = (normalize(artist), normalize(album))
        # Skip if we already indexed this key via Artist/Album scanning
        if key in index:
            continue
        tracks: List[Dict] = []
        try:
            for dirpath, _subdirs, files in os.walk(fpath):
                for fname in sorted(files):
                    if not is_audio(fname):
                        continue
                    tn, title = parse_track_filename(fname)
                    # relative path from root
                    rel = os.path.relpath(os.path.join(dirpath, fname), start=root)
                    tracks.append({
                        "title": title,
                        "relpath": rel.replace("\\", "/"),
                        "index": tn
                    })
        except Exception as e:
            logger.warning(f"Error reading {fpath}: {e}")
            continue
        if tracks:
            tracks.sort(key=lambda t: (t["index"] if isinstance(t["index"], int) else 9999, t["title"]))
            index[key] = tracks

    global LOCAL_INDEX
    LOCAL_INDEX = index
    logger.info(f"Scanned albums: {len(LOCAL_INDEX)}")
    return len(LOCAL_INDEX)


def album_tracks(artist_display: str, album_title: str) -> Optional[Dict[str, Dict]]:
    """Return dict mapping normalized track title -> info for play.
    info: { 'source': 'local', 'path': relpath }
    """
    if not LOCAL_INDEX:
        try:
            scan_library()
        except Exception as e:
            logger.warning(f"Local index not available: {e}")
            return None
    key = (normalize(artist_display), normalize(album_title))
    tracks = LOCAL_INDEX.get(key)
    if not tracks:
        return None
    out: Dict[str, Dict] = {}
    for t in tracks:
        norm = normalize(t.get("title") or "")
        if not norm:
            continue
        out[norm] = {
            "source": "local",
            "path": t.get("relpath"),
        }
    return out


def album_track_list(artist_display: str, album_title: str) -> Optional[List[Dict]]:
    """Return an ordered list of tracks for an album from the local index.

    Each list item has: { 'title': str, 'relpath': str, 'index': int|None }
    Returns None if the album is not found in the local index.
    """
    if not LOCAL_INDEX:
        try:
            scan_library()
        except Exception as e:
            logger.warning(f"Local index not available: {e}")
            return None
    key = (normalize(artist_display), normalize(album_title))
    tracks = LOCAL_INDEX.get(key)
    if not tracks:
        return None
    # Already ordered during scan; return a shallow copy to avoid external mutation
    return list(tracks)


def _abs_path_from_rel(relpath: str) -> Optional[str]:
    root = get_music_root()
    if not root:
        return None
    rel = (relpath or "").lstrip("/\\").replace("\\", "/")
    return os.path.join(root, rel)


def get_track_duration_seconds(relpath: str) -> Optional[float]:
    """Return duration in seconds for a relative media path under MUSIC_ROOT.
    Returns None on failure.
    """
    try:
        ap = _abs_path_from_rel(relpath)
        if not ap or not os.path.isfile(ap):
            return None
        from mutagen import File as MutagenFile  # lazy import
        mf = MutagenFile(ap)
        if not mf or not getattr(mf, "info", None):
            return None
        length = getattr(mf.info, "length", None)
        if length is None:
            return None
        return float(length)
    except Exception as e:
        logger.debug(f"Duration read failed for {relpath}: {e}")
        return None


def format_duration(seconds: Optional[float]) -> str:
    try:
        if seconds is None:
            return ""
        s = max(0, int(round(float(seconds))))
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"
    except Exception:
        return ""


def build_stream_url(relpath: str) -> str:
    # encode relative path to avoid unsafe chars
    # Use simple percent-encoding; prefix to denote it's relative
    p = quote(relpath)
    return f"/local/stream?p={p}"
