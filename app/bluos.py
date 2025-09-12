from __future__ import annotations
import os
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlencode, quote
import requests
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


class BluOSClient:
    """Minimal BluOS HTTP API client.

    Config via environment variables:
      - BLUOS_HOST (required, e.g., 192.168.1.100)
      - BLUOS_PORT (optional, default 11000)
    """

    def __init__(self):
        host = (os.getenv("BLUOS_HOST") or "").strip()
        port = int(os.getenv("BLUOS_PORT") or 11000)
        if not host:
            raise RuntimeError("BLUOS_HOST is required to use BluOS integration")
        self.base = f"http://{host}:{port}"

    # --- HTTP helpers ---
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = f"{self.base}{path}"
        r = requests.get(url, params=params or {}, timeout=10)
        r.raise_for_status()
        return r

    def _post(self, path: str, data: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = f"{self.base}{path}"
        r = requests.post(url, data=data or {}, timeout=10)
        r.raise_for_status()
        return r

    # --- Queries ---
    def status(self, timeout: Optional[int] = None, etag: Optional[str] = None) -> ET.Element:
        params: Dict[str, Any] = {}
        if timeout:
            params["timeout"] = timeout
        if etag:
            params["etag"] = etag
        r = self._get("/Status", params)
        return ET.fromstring(r.text)

    def sync_status(self, timeout: Optional[int] = None, etag: Optional[str] = None) -> ET.Element:
        params: Dict[str, Any] = {}
        if timeout:
            params["timeout"] = timeout
        if etag:
            params["etag"] = etag
        r = self._get("/SyncStatus", params)
        return ET.fromstring(r.text)

    # --- Transport ---
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

    def play_url(self, url: str) -> ET.Element:
        """Ask the player to play a stream URL.
        - Encode the inner stream URL exactly once.
        - Build the full /Play URL string to avoid requests adding another layer.
        """
        enc = quote(url, safe="")
        full = f"{self.base}/Play?url={enc}"
        r = requests.get(full, timeout=10)
        r.raise_for_status()
        return ET.fromstring(r.text)

    # --- Browse / actions ---
    def browse(self, key: str) -> ET.Element:
        """Call /Browse with a provided key (e.g., 'LocalMusic:' or 'LocalMusic:/path')."""
        # Use requests params to encode key once
        r = requests.get(f"{self.base}/Browse", params={"key": key}, timeout=10)
        r.raise_for_status()
        return ET.fromstring(r.text)

    def call_action_path(self, path: str) -> ET.Element:
        """Invoke a returned playURL/actionURL path from a Browse item (e.g., '/Add?...')."""
        if not path.startswith('/'):
            path = '/' + path
        r = requests.get(f"{self.base}{path}", timeout=10)
        r.raise_for_status()
        # Many action calls return <status> or another root; parse leniently
        try:
            return ET.fromstring(r.text)
        except Exception:
            # Return a generic element if response is empty/HTML
            root = ET.Element('result')
            root.text = r.text
            return root

    # --- Volume ---
    def volume(self,
               level: Optional[int] = None,
               mute: Optional[bool] = None,
               db: Optional[float] = None,
               abs_db: Optional[float] = None,
               tell_slaves: Optional[int] = None) -> ET.Element:
        params: Dict[str, Any] = {}
        if level is not None:
            params["level"] = max(0, min(100, int(level)))
        if mute is not None:
            params["mute"] = 1 if mute else 0
        if db is not None:
            params["db"] = db
        if abs_db is not None:
            params["abs_db"] = abs_db
        if tell_slaves is not None:
            params["tell_slaves"] = 1 if tell_slaves else 0
        r = self._get("/Volume", params)
        return ET.fromstring(r.text)

    # --- Presets ---
    def presets(self) -> ET.Element:
        r = self._get("/Presets")
        return ET.fromstring(r.text)

    def load_preset(self, preset_id: str | int) -> ET.Element:
        r = self._get("/Preset", {"id": str(preset_id)})
        return ET.fromstring(r.text)

    # --- Utilities ---
    @staticmethod
    def status_to_dict(root: ET.Element) -> Dict[str, Any]:
        """Extract a useful subset of /Status into a dict."""
        def text(tag: str) -> Optional[str]:
            el = root.find(tag)
            return el.text if el is not None else None
        d: Dict[str, Any] = {
            "etag": root.attrib.get("etag"),
            "state": text("state"),
            "title": text("title1") or text("title") or text("song"),
            "subtitle": text("title2") or text("album"),
            "artist": text("artist"),
            "album": text("album"),
            "service": text("service"),
            "image": text("image"),
            "radioImage": text("radioImage"),
            "secs": None,
            "totlen": None,
            "volume": None,
            "shuffle": text("shuffle"),
            "repeat": text("repeat"),
        }
        # secs
        try:
            s = text("secs")
            d["secs"] = int(s) if s is not None else None
        except Exception:
            pass
        # total length (seconds)
        try:
            tl = text("totlen")
            d["totlen"] = int(tl) if tl is not None else None
        except Exception:
            pass
        # volume can appear in multiple forms; favor element <volume>
        try:
            v = text("volume")
            d["volume"] = int(v) if v is not None else None
        except Exception:
            pass
        return d
