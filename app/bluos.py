from __future__ import annotations
import os
import logging
from typing import Optional, Dict, Any, Tuple
from urllib.parse import quote, urlparse, parse_qs, unquote
import requests
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


class BluOSClient:
    """BluOS HTTP API client with metadata-safe playback.

    Key behavior change:
    - `play_url(url)` detects LocalMusic paths or your app's /local/stream URLs
      and resolves them to a queue action (autoplayURL/playURL via /Browse)
      instead of pushing a raw stream to /Play?url=... (which yields poor metadata).

    Env:
      - BLUOS_HOST (required), BLUOS_PORT=11000
      - BLUOS_CONNECT_TIMEOUT=5, BLUOS_READ_TIMEOUT=10, BLUOS_LONG_POLL_GRACE=2
      - BLUOS_LIBRARY_ROOT (optional; recommended for /local/stream resolution)
    """

    def __init__(self):
        host = (os.getenv("BLUOS_HOST") or "").strip()
        port = int(os.getenv("BLUOS_PORT") or 11000)
        if not host:
            raise RuntimeError("BLUOS_HOST is required to use BluOS integration")
        self.base = f"http://{host}:{port}"
        try:
            self._connect_timeout = float(os.getenv("BLUOS_CONNECT_TIMEOUT", "5") or 5)
        except Exception:
            self._connect_timeout = 5.0
        try:
            self._read_timeout = float(os.getenv("BLUOS_READ_TIMEOUT", "10") or 10)
        except Exception:
            self._read_timeout = 10.0
        try:
            self._long_poll_grace = float(os.getenv("BLUOS_LONG_POLL_GRACE", "2") or 2)
        except Exception:
            self._long_poll_grace = 2.0

    # -------------------------------------------------------------------------
    # Timeouts / HTTP helpers
    # -------------------------------------------------------------------------
    def _timeout_tuple(self, read_timeout: Optional[float] = None) -> tuple[float, float]:
        connect = max(0.5, self._connect_timeout)
        read = read_timeout if read_timeout is not None else self._read_timeout
        return (connect, max(1.0, read))

    def _get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        read_timeout: Optional[float] = None,
    ) -> requests.Response:
        url = f"{self.base}{path}"
        r = requests.get(url, params=params or {}, timeout=self._timeout_tuple(read_timeout))
        r.raise_for_status()
        return r

    def _post(
        self,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        read_timeout: Optional[float] = None,
    ) -> requests.Response:
        url = f"{self.base}{path}"
        r = requests.post(url, data=data or {}, timeout=self._timeout_tuple(read_timeout))
        r.raise_for_status()
        return r

    # -------------------------------------------------------------------------
    # Queries (supports long-poll)
    # -------------------------------------------------------------------------
    def status(self, timeout: Optional[int] = None, etag: Optional[str] = None) -> ET.Element:
        params: Dict[str, Any] = {}
        poll_timeout: Optional[int] = None
        if timeout is not None:
            try:
                poll_timeout = max(1, int(timeout))
            except Exception:
                poll_timeout = 1
            params["timeout"] = poll_timeout
        if etag:
            params["etag"] = etag
        read_timeout = None
        if poll_timeout is not None:
            read_timeout = poll_timeout + self._long_poll_grace
        r = self._get("/Status", params, read_timeout=read_timeout)
        return ET.fromstring(r.text)

    def sync_status(self, timeout: Optional[int] = None, etag: Optional[str] = None) -> ET.Element:
        params: Dict[str, Any] = {}
        poll_timeout: Optional[int] = None
        if timeout is not None:
            try:
                poll_timeout = max(1, int(timeout))
            except Exception:
                poll_timeout = 1
            params["timeout"] = poll_timeout
        if etag:
            params["etag"] = etag
        read_timeout = None
        if poll_timeout is not None:
            read_timeout = poll_timeout + self._long_poll_grace
        r = self._get("/SyncStatus", params, read_timeout=read_timeout)
        return ET.fromstring(r.text)

    # -------------------------------------------------------------------------
    # Transport
    # -------------------------------------------------------------------------
    def play(self, seek: Optional[int] = None, track_id: Optional[int] = None) -> ET.Element:
        params: Dict[str, Any] = {}
        if seek is not None:
            params["seek"] = seek
        if track_id is not None:
            params["id"] = track_id
        r = self._get("/Play", params)
        return ET.fromstring(r.text)

    def pause(self, toggle: bool = False) -> ET.Element:
        params = {"toggle": 1} if toggle else {}
        r = self._get("/Pause", params)
        return ET.fromstring(r.text)

    def stop(self) -> ET.Element:
        r = self._get("/Stop")
        return ET.fromstring(r.text)

    def skip(self) -> ET.Element:
        r = self._get("/Skip")
        return ET.fromstring(r.text)

    def back(self) -> ET.Element:
        r = self._get("/Back")
        return ET.fromstring(r.text)

    def clear(self) -> ET.Element:
        """Clear the current play queue."""
        r = self._get("/Clear")
        return ET.fromstring(r.text)

    # -------------------------------------------------------------------------
    # Metadata-safe play: resolve to action URLs when possible
    # -------------------------------------------------------------------------
    def play_url(self, url: str) -> ET.Element:
        """Play a URL, but prefer queue actions for LocalMusic/local streams.

        Behavior:
          - If `url` starts with "LocalMusic:", browse the parent folder and invoke
            the item's `autoplayURL`/`playURL` so the queue holds the track
            (→ rich metadata).
          - If `url` is your server's `/local/stream?p=...`, map that relative path
            to a LocalMusic folder (using BLUOS_LIBRARY_ROOT) and do the same.
          - Otherwise fall back to raw `/Play?url=` (shows sparse metadata).

        Returns the XML of the action or Play response.
        """
        if not url:
            raise ValueError("url is required for play_url")

        # Case A: LocalMusic scheme already provided
        if url.startswith("LocalMusic:"):
            try:
                remote_path = url[len("LocalMusic:"):].lstrip("/\\")
                folder, fname = self._split_folder_file(remote_path)
                if folder and fname:
                    resolved = self._play_localmusic_by_browse(folder, fname)
                    if resolved is not None:
                        return resolved
            except Exception as e:
                logger.debug(f"LocalMusic resolution failed, falling back to /Play: {e}")

        # Case B: our own /local/stream?p=...
        try:
            parsed = urlparse(url)
            if parsed.path.rstrip("/").endswith("/local/stream"):
                qs = parse_qs(parsed.query or "")
                rel = qs.get("p", [None])[0]
                if rel:
                    rel = unquote(rel)
                    lm_root = (os.getenv("BLUOS_LIBRARY_ROOT") or "").strip().rstrip("/\\")
                    if lm_root:
                        remote_path = f"{lm_root}/{rel}".replace("\\", "/")
                        folder, fname = self._split_folder_file(remote_path)
                        if folder and fname:
                            resolved = self._play_localmusic_by_browse(folder, fname)
                            if resolved is not None:
                                return resolved
        except Exception as e:
            logger.debug(f"/local/stream resolution failed, falling back to /Play: {e}")

        # Fallback: raw Play (likely `state=stream`, limited metadata)
        enc = quote(url, safe="")
        full = f"{self.base}/Play?url={enc}"
        r = requests.get(full, timeout=self._timeout_tuple())
        r.raise_for_status()
        return ET.fromstring(r.text)

    # --- Browse / actions ---
    def browse(self, key: str) -> ET.Element:
        """Call /Browse with a provided key (e.g., 'LocalMusic:' or 'LocalMusic:/path')."""
        r = requests.get(
            f"{self.base}/Browse",
            params={"key": key},
            timeout=self._timeout_tuple(),
        )
        r.raise_for_status()
        return ET.fromstring(r.text)

    def call_action_path(self, path: str) -> ET.Element:
        """Invoke a returned playURL/actionURL path from a Browse item (e.g., '/Add?...')."""
        path = (path or "").strip()
        if not path:
            raise ValueError("path is required")
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            if not path.startswith('/'):
                path = '/' + path
            url = f"{self.base}{path}"
        r = requests.get(url, timeout=self._timeout_tuple())
        r.raise_for_status()
        try:
            return ET.fromstring(r.text)
        except Exception:
            # Some actions return plain text; wrap it.
            root = ET.Element('result')
            root.text = r.text
            return root

    # -------------------------------------------------------------------------
    # Helpers for LocalMusic resolution
    # -------------------------------------------------------------------------
    @staticmethod
    def _split_folder_file(remote_path: str) -> Tuple[Optional[str], Optional[str]]:
        """Return (folder, filename) from a LocalMusic-style path."""
        rp = (remote_path or "").replace("\\", "/").strip().lstrip("/")
        if not rp:
            return None, None
        if "/" in rp:
            folder = rp.rsplit("/", 1)[0]
            name = rp.rsplit("/", 1)[1]
        else:
            folder, name = "", rp
        return (folder, name)

    @staticmethod
    def _norm(s: str) -> str:
        """Lightweight normalizer for comparing track titles/filenames."""
        if not s:
            return ""
        s2 = s.lower().strip()
        # remove extension
        if "." in s2:
            s2 = s2.rsplit(".", 1)[0]
        # collapse separators
        for ch in ("_", "-", ".", "(", ")", "[", "]"):
            s2 = s2.replace(ch, " ")
        while "  " in s2:
            s2 = s2.replace("  ", " ")
        return s2.strip()

    def _play_localmusic_by_browse(self, folder: str, filename: str) -> Optional[ET.Element]:
        """Browse LocalMusic:folder and try to locate filename → invoke its action."""
        key = f"LocalMusic:{folder}"
        broot = self.browse(key)

        target_file = (filename or "").lower()
        target_norm = self._norm(filename)

        best_el = None
        best_ratio = 0.0

        for el in broot.iter():
            if el.tag != 'item':
                continue
            t = el.attrib.get('type')
            if t not in ('audio', 'song', 'track', None):
                continue

            # 1) Try URL/Path attribute match (often contains full path)
            file_attr = (el.attrib.get('url') or el.attrib.get('path') or "").lower()
            if file_attr and target_file and target_file in file_attr:
                play_url = el.attrib.get('autoplayURL') or el.attrib.get('autoplayPath') \
                           or el.attrib.get('playURL') or el.attrib.get('actionURL')
                if play_url:
                    return self.call_action_path(play_url)

            # 2) Fuzzy match against displayed title
            text_title = (el.attrib.get('text') or "").strip()
            if text_title:
                cand = self._norm(text_title)
                # quick ratio: common length / max len
                if cand and target_norm:
                    common = len(os.path.commonprefix([cand, target_norm]))
                    maxlen = max(len(cand), len(target_norm))
                    ratio = common / max(1, maxlen)
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_el = el

        # If a fuzzy candidate seems good enough (>= 0.8), use it
        if best_el and best_ratio >= float(os.getenv("BLUOS_FUZZY_THRESHOLD", "0.8")):
            play_url = best_el.attrib.get('autoplayURL') or best_el.attrib.get('autoplayPath') \
                       or best_el.attrib.get('playURL') or best_el.attrib.get('actionURL')
            if play_url:
                return self.call_action_path(play_url)

        return None

    # -------------------------------------------------------------------------
    # Presets / Volume
    # -------------------------------------------------------------------------
    def presets(self) -> ET.Element:
        r = self._get("/Presets")
        return ET.fromstring(r.text)

    def load_preset(self, preset_id: str | int) -> ET.Element:
        r = self._get("/Preset", {"id": str(preset_id)})
        return ET.fromstring(r.text)

    # -------------------------------------------------------------------------
    # Utilities: robust /Status parsing for Last.fm / UI
    # -------------------------------------------------------------------------
    @staticmethod
    def status_to_dict(root: ET.Element) -> Dict[str, Any]:
        """Extract a robust subset of /Status into a dict, with Last.fm-friendly fields."""

        def text(tag: str) -> Optional[str]:
            el = root.find(tag)
            return el.text if el is not None else None

        # Prefer title1/title2/title3 per BluOS UI semantics
        line1 = text("title1") or text("twoline_title1") or text("title") or text("name") or text("song")
        line2 = text("title2") or text("twoline_title2")
        line3 = text("title3")

        raw_artist = text("artist")
        raw_album = text("album")
        raw_state = (text("state") or "").lower()
        stream_url = text("streamUrl")
        image = text("image") or text("radioImage")

        # Map common numbers
        def to_int(v: Optional[str]) -> Optional[int]:
            try:
                return int(v) if v is not None else None
            except Exception:
                return None

        d: Dict[str, Any] = {
            "etag": root.attrib.get("etag"),
            "state": raw_state or None,
            "service": text("service"),
            "image": image,
            "radioImage": text("radioImage"),
            "streamFormat": text("streamFormat"),
            "quality": text("quality"),
            "secs": to_int(text("secs")),
            "totlen": to_int(text("totlen")),
            "volume": to_int(text("volume")),
            "shuffle": text("shuffle"),
            "repeat": text("repeat"),
            "streamUrl": stream_url,
        }

        # is_stream: either state=stream or streamUrl present
        d["is_stream"] = raw_state == "stream" or bool(stream_url)

        # Last.fm-friendly mapping with heuristics
        track = line1 or None
        artist = line2 or None
        album = line3 or None

        if (not artist) and d["is_stream"] and line1 and (" - " in line1):
            # Radio-style: "Artist - Track"
            a, b = [s.strip() for s in line1.split(" - ", 1)]
            if a and b:
                artist, track = a, b

        if not artist and raw_artist:
            artist = raw_artist
        if not album and raw_album:
            album = raw_album

        d.update({
            "title1": line1,
            "title2": line2,
            "title3": line3,
            "track": track,
            "artist": artist,
            "album": album,
        })
        return d
