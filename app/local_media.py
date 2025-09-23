from __future__ import annotations
import os
import logging
from typing import Dict, List, Optional, Tuple, Any
import re
from urllib.parse import quote

logger = logging.getLogger(__name__)


# In-memory index: (norm_artist, norm_album) -> list of tracks
# Track item: { 'title': str, 'relpath': str, 'index': int|None }
LOCAL_INDEX: Dict[Tuple[str, str], List[Dict]] = {}
LOCAL_KEY_TO_FOLDER: Dict[Tuple[str, str], str] = {}
LOCAL_FOLDER_INDEX: Dict[str, List[Dict]] = {}
LOCAL_FOLDER_META: Dict[str, Dict[str, Any]] = {}
MUSIC_ROOT: Optional[str] = None


def set_music_root(root: Optional[str]):
    global MUSIC_ROOT, LOCAL_INDEX, LOCAL_KEY_TO_FOLDER, LOCAL_FOLDER_INDEX, LOCAL_FOLDER_META
    MUSIC_ROOT = (root or "").rstrip("\\/") or None
    LOCAL_INDEX = {}
    LOCAL_KEY_TO_FOLDER = {}
    LOCAL_FOLDER_INDEX = {}
    LOCAL_FOLDER_META = {}


def get_music_root() -> Optional[str]:
    if MUSIC_ROOT:
        return MUSIC_ROOT
    root = os.getenv("MUSIC_ROOT", "").strip()
    return root or None


from .normalize import normalize_title as _normalize_title, sanitize_for_local as _sanitize_for_local, fold_to_ascii as _fold_ascii


def normalize(s: str) -> str:
    return _normalize_title(_sanitize_for_local(s))


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

DISC_FOLDER_PATTERN = re.compile(r'(?:disc|cd)\s*(\d+)', re.IGNORECASE)
DISC_NUMBER_PATTERN = re.compile(r'^(?:disc|cd)?\s*(\d+)$', re.IGNORECASE)


def _disc_from_dirpath(dirpath: str, album_root: str) -> Optional[int]:
    try:
        rel = os.path.relpath(dirpath, album_root)
    except Exception:
        return None
    rel = rel.replace('\\','/').strip('/')
    if not rel or rel == '.':
        return None
    parts = [p.strip() for p in rel.split('/') if p.strip()]
    for part in reversed(parts):
        m = DISC_FOLDER_PATTERN.search(part)
        if m:
            try:
                value = int(m.group(1))
                if value > 0:
                    return value
            except Exception:
                continue
        m2 = DISC_NUMBER_PATTERN.match(part)
        if m2:
            try:
                value = int(m2.group(1))
                if value > 0:
                    return value
            except Exception:
                continue
    return None


def _collect_tracks_for_album(album_path: str, root: str) -> List[Dict]:
    tracks: List[Dict] = []
    for dirpath, _subdirs, files in os.walk(album_path):
        disc_hint = _disc_from_dirpath(dirpath, album_path)
        for fname in sorted(files):
            if not is_audio(fname):
                continue
            tn, title_from_name = parse_track_filename(fname)
            rel = os.path.relpath(os.path.join(dirpath, fname), start=root).replace('\\','/')
            md = None
            try:
                md = read_metadata(rel)
            except Exception:
                md = None
            title = (md.get('title') if isinstance(md, dict) else None) or title_from_name
            track_tag = None
            if isinstance(md, dict):
                track_tag = _parse_track_tuple(md.get('track'))
            if track_tag is None and isinstance(tn, int) and tn > 0:
                track_tag = tn
            disc_tag = None
            if isinstance(md, dict):
                disc_tag = _parse_track_tuple(md.get('disc'))
            if disc_tag is None:
                disc_tag = disc_hint
            tracks.append({
                'title': title,
                'relpath': rel,
                'index': track_tag,
                'disc': disc_tag,
            })
    return tracks



def scan_library(root: Optional[str] = None) -> int:
    """Scan MUSIC_ROOT for Artist/Album/Track.ext and build an in-memory index.
    Returns number of indexed albums.
    """
    root = root or get_music_root()
    if not root or not os.path.isdir(root):
        raise RuntimeError("MUSIC_ROOT not set or not a directory")

    logger.info(f"Scanning music library at: {root}")
    index: Dict[Tuple[str, str], List[Dict]] = {}
    key_to_folder: Dict[Tuple[str, str], str] = {}
    folder_index: Dict[str, List[Dict]] = {}
    folder_meta: Dict[str, Dict[str, Any]] = {}
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
            try:
                tracks = _collect_tracks_for_album(al_path, root)
            except Exception as e:
                logger.warning(f"Error reading {al_path}: {e}")
                continue
            if tracks:
                def _key(t):
                    d = t.get("disc") if isinstance(t.get("disc"), int) else 1
                    i = t.get("index") if isinstance(t.get("index"), int) else 9999
                    return (d, i, str(t.get("title") or ''))
                tracks.sort(key=_key)
                index[key] = tracks
                folder_rel = os.path.join(artist, album).replace('\\', '/')
                if key not in key_to_folder:
                    key_to_folder[key] = folder_rel
                folder_index[folder_rel] = tracks
                folder_meta[folder_rel] = {
                    "artist": artist,
                    "album": album,
                    "normalized_artist": key[0],
                    "normalized_album": key[1],
                    "folder": folder_rel,
                    "track_count": len(tracks),
                    "source": 'nested',
                }


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
                    tn, title_from_name = parse_track_filename(fname)
                    # relative path from root
                    rel = os.path.relpath(os.path.join(dirpath, fname), start=root).replace("\\", "/")
                    md = None
                    try:
                        md = read_metadata(rel)
                    except Exception:
                        md = None
                    title = (md.get("title") if md else None) or title_from_name
                    tn_tag = md.get("track") if isinstance(md, dict) else None
                    disc_tag = md.get("disc") if isinstance(md, dict) else None
                    idx = tn_tag if isinstance(tn_tag, int) and tn_tag > 0 else (tn if isinstance(tn, int) and tn > 0 else None)
                    tracks.append({
                        "title": title,
                        "relpath": rel,
                        "index": idx,
                        "disc": int(disc_tag) if isinstance(disc_tag, int) and disc_tag > 0 else None,
                    })
        except Exception as e:
            logger.warning(f"Error reading {fpath}: {e}")
            continue
        if tracks:
            def _key2(t):
                d = t.get("disc") if isinstance(t.get("disc"), int) else 1
                i = t.get("index") if isinstance(t.get("index"), int) else 9999
                return (d, i, str(t.get("title") or ""))
            tracks.sort(key=_key2)
            index[key] = tracks
            folder_rel = os.path.relpath(fpath, start=root).replace("\\", "/")
            if key not in key_to_folder:
                key_to_folder[key] = folder_rel
            folder_index[folder_rel] = tracks
            folder_meta[folder_rel] = {
                "artist": artist,
                "album": album,
                "normalized_artist": key[0],
                "normalized_album": key[1],
                "folder": folder_rel,
                "track_count": len(tracks),
                "source": "flat",
            }

    global LOCAL_INDEX, LOCAL_KEY_TO_FOLDER, LOCAL_FOLDER_INDEX, LOCAL_FOLDER_META
    LOCAL_INDEX = index
    LOCAL_KEY_TO_FOLDER = key_to_folder
    LOCAL_FOLDER_INDEX = folder_index
    LOCAL_FOLDER_META = folder_meta
    logger.info(f"Scanned albums: {len(LOCAL_INDEX)} (folders={len(LOCAL_FOLDER_INDEX)})")
    return len(LOCAL_INDEX)


def _ensure_index() -> bool:
    if LOCAL_INDEX:
        return True
    try:
        scan_library()
        return bool(LOCAL_INDEX)
    except Exception as e:
        logger.warning(f"Local index not available: {e}")
        return False


def album_tracks(artist_display: str, album_title: str) -> Optional[Dict[str, Dict]]:
    """Return dict mapping normalized track title -> info for play.
    info: { "source": 'local', 'path': relpath }
    """
    if not _ensure_index():
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
    if not _ensure_index():
        return None
    key = (normalize(artist_display), normalize(album_title))
    tracks = LOCAL_INDEX.get(key)
    if not tracks:
        return None
    # Already ordered during scan; return a shallow copy to avoid external mutation
    return list(tracks)


def folder_for_album(artist_display: str, album_title: str) -> Optional[str]:
    if not _ensure_index():
        return None
    key = (normalize(artist_display), normalize(album_title))
    return LOCAL_KEY_TO_FOLDER.get(key)


def album_tracks_from_folder(folder_rel: str) -> Optional[Dict[str, Dict]]:
    if not _ensure_index():
        return None
    if not folder_rel:
        return None
    folder = folder_rel.strip("/\\").replace("\\", "/")
    tracks = LOCAL_FOLDER_INDEX.get(folder)
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


def album_track_list_from_folder(folder_rel: str) -> Optional[List[Dict]]:
    if not _ensure_index():
        return None
    if not folder_rel:
        return None
    folder = folder_rel.strip("/\\").replace("\\", "/")
    tracks = LOCAL_FOLDER_INDEX.get(folder)
    if not tracks:
        return None
    return list(tracks)


def search_albums(query: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    if not _ensure_index():
        return []
    items = list(LOCAL_FOLDER_META.values())
    q_raw = (query or "").strip()
    q = _fold_ascii(q_raw).lower() if q_raw else ""
    if q:
        parts = [p for p in q.split() if p]
        if parts:
            def _matches(meta: Dict[str, Any]) -> bool:
                hay_src = " ".join(filter(None, [meta.get("artist"), meta.get("album"), meta.get("folder")]))
                hay = _fold_ascii(hay_src).lower()
                return all(part in hay for part in parts)
            items = [m for m in items if _matches(m)]
    try:
        limit_val = int(limit) if limit is not None else 50
    except Exception:
        limit_val = 50
    limit_val = max(1, min(200, limit_val))
    items.sort(key=lambda m: (
        (m.get("artist") or "").lower(),
        (m.get("album") or "").lower(),
        m.get("folder") or "",
    ))
    return [dict(m) for m in items[:limit_val]]


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


def _first(val):
    try:
        if isinstance(val, (list, tuple)):
            return val[0] if val else None
    except Exception:
        pass
    return val


def _parse_track_tuple(v) -> Optional[int]:
    """Handle track/disc tuples or strings like '1/12'."""
    try:
        if isinstance(v, (list, tuple)) and v:
            # MP4 'trkn' style: [(track, total)] or (track, total)
            vv = v[0] if isinstance(v[0], (list, tuple)) else v
            return int(vv[0]) if vv and int(vv[0]) >= 0 else None
        s = str(v or '').strip()
        if not s:
            return None
        if '/' in s:
            s = s.split('/', 1)[0]
        return int(s) if s.isdigit() else None
    except Exception:
        return None


def read_metadata(relpath: str) -> Optional[dict]:
    """Read common tags and technical info from a relative path under MUSIC_ROOT.

    Returns a dict with keys: title, artist, album, albumartist, track, disc, date, genre,
    duration, bitrate, sample_rate, channels, container.
    """
    try:
        ap = _abs_path_from_rel(relpath)
        if not ap or not os.path.isfile(ap):
            return None
        from mutagen import File as MutagenFile
        import os as _os
        mf = MutagenFile(ap)
        if not mf:
            return None
        info = getattr(mf, 'info', None)
        out = {
            'title': None,
            "artist": None,
            "album": None,
            'albumartist': None,
            'track': None,
            'disc': None,
            'date': None,
            'genre': None,
            'duration': float(getattr(info, 'length', 0.0) or 0.0) if info else None,
            'bitrate': int(getattr(info, 'bitrate', 0) or 0) if info else None,
            'sample_rate': int(getattr(info, 'sample_rate', 0) or 0) if info else None,
            'channels': int(getattr(info, 'channels', 0) or 0) if info else None,
            'container': (getattr(mf, '__class__', type('x',(object,),{})).__name__),
            'path': relpath,
        }

        tags = getattr(mf, 'tags', None)
        if tags:
            # MP3 (ID3)
            try:
                from mutagen.id3 import ID3
                if isinstance(tags, ID3):
                    def _id3(name):
                        f = tags.get(name)
                        return _first(getattr(f, 'text', None)) if f else None
                    out['title'] = _id3('TIT2') or out['title']
                    out["artist"] = _id3('TPE1') or out["artist"]
                    out["album"] = _id3('TALB') or out["album"]
                    out['albumartist'] = _id3('TPE2') or out['albumartist']
                    out['genre'] = _id3('TCON') or out['genre']
                    out['date'] = _id3('TDRC') or _id3('TYER') or out['date']
                    out['track'] = _parse_track_tuple(_id3('TRCK')) or out['track']
                    out['disc'] = _parse_track_tuple(_id3('TPOS')) or out['disc']
            except Exception:
                pass

            # MP4/M4A
            try:
                if hasattr(tags, 'keys') and ('\xa9nam' in tags or '©nam' in tags):
                    def _mp4_get(k):
                        return tags.get(k) or tags.get(k.encode('utf-8').decode('utf-8'))
                    out['title'] = _first(tags.get('\xa9nam') or tags.get('©nam')) or out['title']
                    out["artist"] = _first(tags.get('\xa9ART') or tags.get('©ART')) or out["artist"]
                    out["album"] = _first(tags.get('\xa9alb') or tags.get('©alb')) or out["album"]
                    out['albumartist'] = _first(tags.get('aART')) or out['albumartist']
                    out['genre'] = _first(tags.get('\xa9gen') or tags.get('©gen')) or out['genre']
                    out['date'] = _first(tags.get('\xa9day') or tags.get('©day')) or out['date']
                    out['track'] = _parse_track_tuple(tags.get('trkn')) or out['track']
                    out['disc'] = _parse_track_tuple(tags.get('disk')) or out['disc']
            except Exception:
                pass

            # FLAC / Vorbis / Opus (Vorbis comments)
            try:
                def _vc_first(*keys):
                    for k in keys:
                        v = tags.get(k)
                        if v:
                            return _first(v)
                    return None
                out['title'] = _vc_first('title') or out['title']
                out["artist"] = _vc_first("artist") or out["artist"]
                out["album"] = _vc_first("album") or out["album"]
                out['albumartist'] = _vc_first('albumartist', 'album artist') or out['albumartist']
                out['genre'] = _vc_first('genre') or out['genre']
                out['date'] = _vc_first('date', 'year') or out['date']
                out['track'] = _parse_track_tuple(_vc_first('tracknumber', 'track')) or out['track']
                out['disc'] = _parse_track_tuple(_vc_first('discnumber', 'disc')) or out['disc']
            except Exception:
                pass

            # ASF/WMA
            try:
                # mutagen.asf.ASF objects expose dict-like with keys like 'Title', 'Author', etc.
                def _asf_first(k):
                    v = tags.get(k)
                    return _first(getattr(v[0], 'value', v[0])) if isinstance(v, list) and v else None
                if hasattr(tags, 'as_dict') or 'Title' in tags:
                    out['title'] = _asf_first('Title') or out['title']
                    out["artist"] = _asf_first('Author') or out["artist"]
                    out["album"] = _asf_first('WM/AlbumTitle') or out["album"]
                    out['albumartist'] = _asf_first('WM/AlbumArtist') or out['albumartist']
                    out['genre'] = _asf_first('WM/Genre') or out['genre']
                    out['date'] = _asf_first('WM/Year') or out['date']
                    out['track'] = _parse_track_tuple(_asf_first('WM/TrackNumber')) or out['track']
                    out['disc'] = _parse_track_tuple(_asf_first('WM/PartOfSet')) or out['disc']
            except Exception:
                pass

        # Cleanup types
        for k in ('title',"artist","album",'albumartist','genre','date'):
            try:
                if out[k] is not None:
                    out[k] = str(out[k])
            except Exception:
                pass
        return out
    except Exception as e:
        logger.debug(f"Metadata read failed for {relpath}: {e}")
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


