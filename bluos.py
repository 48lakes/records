from __future__ import annotations
import os
import logging
from typing import Optional, Dict, Any, Iterable
from urllib.parse import quote
import time
import requests
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


class BluOSClient:
    """
    Minimal BluOS HTTP API client, updated to ensure **rich metadata** by
    playing via `playURL` / `autoplayURL` returned by `/Browse`, instead of
    pushing raw stream URLs to `/Play?url=...` (which yields `state=stream`
    and sparse metadata).

    ENV configuration:
      - BLUOS_HOST (required, e.g., 192.168.1.100)
      - BLUOS_PORT (optional, default 11000)
      - BLUOS_CONNECT_TIMEOUT (optional, default 5 seconds)
      - BLUOS_READ_TIMEOUT (optional, default 10 seconds)
      - BLUOS_LONG_POLL_GRACE (optional, default 2 seconds)
      - BLUOS_RETRY_COUNT (optional, default 2)
      - BLUOS_RETRY_BACKOFF_SECS (optional, default 0.25)
    """

    # ------------------------------------------------------------
    # Construction / session / timeouts
    # ------------------------------------------------------------
    def __init__(self):
        host = (os.getenv("BLUOS_HOST") or "").strip()
        port = int(os.getenv("BLUOS_PORT") or 11000)
        if not host:
            raise RuntimeError("BLUOS_HOST is required to use BluOS integration")
        self.base = f"http://{host}:{port}"

        # Timeouts
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

        # Simple retry policy for transient network hiccups
        try:
            self._retries = int(os.getenv("BLUOS_RETRY_COUNT", "2") or 2)
        except Exception:
            self._retries = 2
        try:
            self._retry_backoff = float(os.getenv("BLUOS_RETRY_BACKOFF_SECS", "0.25") or 0.25)
        except Exception:
            self._retry_backoff = 0.25

        # Reuse TCP connections
        self._sess = requests.Session()

    def _timeout_tuple(self, read_timeout: Optional[float] = None) -> tuple[float, float]:
        connect = max(0.5, self._connect_timeout)
        read = read_timeout if read_timeout is not None else self._read_timeout
        return (connect, max(1.0, read))

    # ------------------------------------------------------------
    # Low-level HTTP with tiny retry wrapper
    # ------------------------------------------------------------
    def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[tuple[float, float]] = None,
    ) -> requests.Response:
        last_exc: Optional[Exception] = None
        attempts = max(0, self._retries) + 1
        for attempt in range(1, attempts + 1):
            try:
                r = self._sess.request(
                    method=method.upper(),
                    url=url,
                    params=params or None,
                    data=data or None,
                    timeout=timeout or self._timeout_tuple(),
                )
                r.raise_for_status()
                return r
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
                last_exc = e
                # 5xx → retry; 4xx → don't (except maybe 408)
                if isinstance(e, requests.HTTPError):
                    code = e.response.status_code if e.response is not None else None
                    if code is not None and code < 500 and code not in (408,):
                        break
                if attempt < attempts:
                    time.sleep(self._retry_backoff * attempt)
        assert last_exc is not None
        raise last_exc

    def _get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        read_timeout: Optional[float] = None,
    ) -> requests.Response:
        url = f"{self.base}{path}"
        return self._request_with_retries(
            "GET", url, params=params, timeout=self._timeout_tuple(read_timeout)
        )

    def _post(
        self,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        read_timeout: Optional[float] = None,
    ) -> requests.Response:
        # NOTE: BluOS control API is primarily GET; _post stays here for completeness.
        url = f"{self.base}{path}"
        return self._request_with_retries(
            "POST", url, data=data, timeout=self._timeout_tuple(read_timeout)
        )

    # ------------------------------------------------------------
    # Queries (Status / SyncStatus) with long-poll support
    # ------------------------------------------------------------
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

    # ------------------------------------------------------------
    # Transport (Queue-based controls)
    # ------------------------------------------------------------
    def play(self, seek: Optional[int] = None, track_id: Optional[int] = None) -> ET.Element:
        """
        Control playback of the **queue**.
        - Use `seek` and/or `track_id` to jump **within the current queue**.
        - For rich metadata, ensure the queue entries originate from `/Browse` actions.
        """
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

    # ------------------------------------------------------------
    # Stream playback (discouraged for metadata purposes)
    # ------------------------------------------------------------
    def play_url(self, url: str) -> ET.Element:
        """
        Ask the player to play an arbitrary **stream URL**.
        WARNING: This typically yields `state=stream` and **sparse metadata**,
        which is NOT ideal for Last.fm scrobbling or nice now-playing displays.

        Prefer `call_action_path()` with a `playURL` / `autoplayURL` obtained
        from `browse()` (or your persisted `bluos_maps`) so BluOS builds proper
        queue items with full metadata.

        Implementation detail:
        - Encode the inner stream URL exactly once.
        - Build the full `/Play` URL string to avoid `requests` adding another layer.
        """
        enc = quote(url, safe="")
        full = f"{self.base}/Play?url={enc}"
        r = self._request_with_retries("GET", full, timeout=self._timeout_tuple())
        return ET.fromstring(r.text)

    # ------------------------------------------------------------
    # Browse / action helpers (preferred path for rich metadata)
    # ------------------------------------------------------------
    def browse(self, key: str) -> ET.Element:
        """
        Call /Browse with a provided key (e.g., 'LocalMusic:' or 'LocalMusic:/path').
        Use results' `playURL` / `autoplayURL` to add/play with full metadata.
        """
        r = self._request_with_retries(
            "GET",
            f"{self.base}/Browse",
            params={"key": key},
            timeout=self._timeout_tuple(),
        )
        return ET.fromstring(r.text)

    def call_action_path(self, path: str) -> ET.Element:
        """
        Invoke a returned playURL/actionURL from a Browse item (e.g., '/Add?...' or '/Play?...').
        This is the **correct** way to start playback with metadata.
        """
        path = (path or "").strip()
        if not path:
            raise ValueError("path is required")
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            if not path.startswith('/'):
                path = '/' + path
            url = f"{self.base}{path}"
        r = self._request_with_retries("GET", url, timeout=self._timeout_tuple())
        try:
            return ET.fromstring(r.text)
        except Exception:
            # Some action endpoints return plain text; wrap it.
            root = ET.Element('result')
            root.text = r.text
            return root

    def play_from_browse_items(
        self,
        browse_root: ET.Element,
        match_predicate: Optional[callable] = None,
        prefer_autoplay: bool = True,
    ) -> Optional[ET.Element]:
        """
        Convenience: given a `/Browse` XML root, find the first item that
        matches `match_predicate(item_element) -> bool`, then invoke its
        autoplay/play action so playback shows rich metadata.

        - If `prefer_autoplay` is True, use `autoplayURL`/`autoplayPath` first.
        - Fallback to `playURL`/`actionURL`.

        Returns the action's XML response, or None if no match.
        """
        if match_predicate is None:
            match_predicate = lambda el: el.tag == "item"  # first playable item

        for el in browse_root.iter():
            if el.tag != "item":
                continue
            if not match_predicate(el):
                continue

            # Pull candidate action URLs in preference order
            attrs = el.attrib
            paths: Iterable[str] = []
            if prefer_autoplay:
                paths = (
                    p for p in [
                        attrs.get("autoplayURL") or attrs.get("autoplayPath"),
                        attrs.get("playURL") or attrs.get("actionURL"),
                    ] if p
                )
            else:
                paths = (
                    p for p in [
                        attrs.get("playURL") or attrs.get("actionURL"),
                        attrs.get("autoplayURL") or attrs.get("autoplayPath"),
                    ] if p
                )

            for p in paths:
                try:
                    return self.call_action_path(p)
                except Exception as e:
                    logger.debug(f"call_action_path failed for '{p}': {e}")
                    continue
        return None

    # ------------------------------------------------------------
    # Presets / Volume
    # ------------------------------------------------------------
    def volume(
        self,
        level: Optional[int] = None,
        mute: Optional[bool] = None,
        db: Optional[float] = None,
        abs_db: Optional[float] = None,
        tell_slaves: Optional[int] = None,
    ) -> ET.Element:
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

    def presets(self) -> ET.Element:
        r = self._get("/Presets")
        return ET.fromstring(r.text)

    def load_preset(self, preset_id: str | int) -> ET.Element:
        r = self._get("/Preset", {"id": str(preset_id)})
        return ET.fromstring(r.text)

    # ------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------
    @staticmethod
    def status_to_dict(root: ET.Element) -> Dict[str, Any]:
        """
        Extract a useful subset of `/Status` into a dict.
        Includes fields helpful for Last.fm scrobbling and diagnostics.
        """

        def text(tag: str) -> Optional[str]:
            el = root.find(tag)
            return el.text if el is not None else None

        d: Dict[str, Any] = {
            # Top-level attributes
            "etag": root.attrib.get("etag"),
            # State & identity
            "state": text("state"),
            "service": text("service"),
            "streamUrl": text("streamUrl"),
            "streamFormat": text("streamFormat"),
            "quality": text("quality"),
            # Canonical title/artist/album resolution
            "title": text("title1") or text("title") or text("name") or text("song"),
            "subtitle": text("title2") or text("album"),
            "artist": text("artist") or text("title2"),
            "album": text("album") or text("title3"),
            # Artwork
            "image": text("image"),
            "radioImage": text("radioImage"),
            # Positioning / queue
            "secs": None,
            "totlen": None,
            "volume": None,
            "shuffle": text("shuffle"),
            "repeat": text("repeat"),
        }
        try:
            s = text("secs")
            d["secs"] = int(s) if s is not None else None
        except Exception:
            pass
        try:
            tl = text("totlen")
            d["totlen"] = int(tl) if tl is not None else None
        except Exception:
            pass
        try:
            v = text("volume")
            d["volume"] = int(v) if v is not None else None
        except Exception:
            pass

        # Helpful boolean: are we in raw-stream mode (i.e., likely poor metadata)?
        try:
            d["is_stream"] = (d.get("state") == "stream") or bool(d.get("streamUrl"))
        except Exception:
            d["is_stream"] = False

        return d


from __future__ import annotations
import os
import time
import logging
from typing import Optional, Dict, Any, Iterable, Callable, Tuple
from urllib.parse import quote
import xml.etree.ElementTree as ET
import requests

logger = logging.getLogger(__name__)


class BluOSClient:
    """
    BluOS HTTP API client tuned for **metadata-correct** playback and parsing.

    Key points for metadata / Last.fm:
    - The BluOS API (v1.7) specifies that **title1/title2/title3** (or twoline_* variants)
      are the authoritative now-playing lines. Do NOT rely on album/artist/name for logic.
    - If `/Status` contains **<streamUrl>** or state == "stream", playback is not sourced
      from the queue; next/prev, shuffle, repeat may be irrelevant and metadata can be sparse.
    - For reliable metadata (and thus good scrobbles), prefer playing via **playURL/autoplayURL**
      returned by `/Browse` (LocalMusic or services) rather than `/Play?url=...`.

    ENV configuration:
      - BLUOS_HOST (required, e.g., 192.168.1.100)
      - BLUOS_PORT (optional, default 11000)
      - BLUOS_CONNECT_TIMEOUT (optional, default 5)
      - BLUOS_READ_TIMEOUT (optional, default 10)
      - BLUOS_LONG_POLL_GRACE (optional, default 2)
      - BLUOS_RETRY_COUNT (optional, default 2)
      - BLUOS_RETRY_BACKOFF_SECS (optional, default 0.25)
    """

    # -------------------------------------------------------------------------
    # Construction / session / timeouts
    # -------------------------------------------------------------------------
    def __init__(self):
        host = (os.getenv("BLUOS_HOST") or "").strip()
        port = int(os.getenv("BLUOS_PORT") or 11000)
        if not host:
            raise RuntimeError("BLUOS_HOST is required to use BluOS integration")
        self.base = f"http://{host}:{port}"

        # Timeouts
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

        # Simple retry policy for transient network hiccups
        try:
            self._retries = int(os.getenv("BLUOS_RETRY_COUNT", "2") or 2)
        except Exception:
            self._retries = 2
        try:
            self._retry_backoff = float(os.getenv("BLUOS_RETRY_BACKOFF_SECS", "0.25") or 0.25)
        except Exception:
            self._retry_backoff = 0.25

        # Reuse TCP connections
        self._sess = requests.Session()

    def _timeout_tuple(self, read_timeout: Optional[float] = None) -> tuple[float, float]:
        """Build (connect_timeout, read_timeout) for requests."""
        connect = max(0.5, self._connect_timeout)
        read = read_timeout if read_timeout is not None else self._read_timeout
        return (connect, max(1.0, read))

    # -------------------------------------------------------------------------
    # Low-level HTTP with tiny retry wrapper
    # -------------------------------------------------------------------------
    def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        timeout: Optional[Tuple[float, float]] = None,
    ) -> requests.Response:
        """Issue an HTTP request with light retries on timeouts/5xx."""
        last_exc: Optional[Exception] = None
        attempts = max(0, self._retries) + 1
        for attempt in range(1, attempts + 1):
            try:
                r = self._sess.request(
                    method=method.upper(),
                    url=url,
                    params=params or None,
                    data=data or None,
                    timeout=timeout or self._timeout_tuple(),
                )
                r.raise_for_status()
                return r
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
                last_exc = e
                # For HTTP errors, only retry on 5xx and 408.
                if isinstance(e, requests.HTTPError):
                    code = e.response.status_code if e.response is not None else None
                    if code is not None and code < 500 and code not in (408,):
                        break
                if attempt < attempts:
                    time.sleep(self._retry_backoff * attempt)
        assert last_exc is not None
        raise last_exc

    def _get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        read_timeout: Optional[float] = None,
    ) -> requests.Response:
        url = f"{self.base}{path}"
        return self._request_with_retries(
            "GET", url, params=params, timeout=self._timeout_tuple(read_timeout)
        )

    def _post(
        self,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        read_timeout: Optional[float] = None,
    ) -> requests.Response:
        # NOTE: BluOS control API is primarily GET; _post is kept for completeness.
        url = f"{self.base}{path}"
        return self._request_with_retries(
            "POST", url, data=data, timeout=self._timeout_tuple(read_timeout)
        )

    # -------------------------------------------------------------------------
    # Queries (Status / SyncStatus) with long-poll support
    # -------------------------------------------------------------------------
    def status(self, timeout: Optional[int] = None, etag: Optional[str] = None) -> ET.Element:
        """
        Get /Status. Supports long polling with (timeout, etag).
        Use this for now-playing metadata and progress.
        """
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
        read_timeout = poll_timeout + self._long_poll_grace if poll_timeout is not None else None
        r = self._get("/Status", params, read_timeout=read_timeout)
        return ET.fromstring(r.text)

    def sync_status(self, timeout: Optional[int] = None, etag: Optional[str] = None) -> ET.Element:
        """Get /SyncStatus. Useful for volume/group info; pairs with Status via syncStat."""
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
        read_timeout = poll_timeout + self._long_poll_grace if poll_timeout is not None else None
        r = self._get("/SyncStatus", params, read_timeout=read_timeout)
        return ET.fromstring(r.text)

    # -------------------------------------------------------------------------
    # Transport (Queue-based controls)
    # -------------------------------------------------------------------------
    def play(self, seek: Optional[int] = None, track_id: Optional[int] = None) -> ET.Element:
        """
        Control playback of the **queue**.
        - Use `seek` and/or `track_id` to jump within the current queue.
        - For rich metadata, ensure queue entries came from /Browse actions (playURL/autoplayURL).
        """
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
    # Stream playback (discouraged for metadata purposes)
    # -------------------------------------------------------------------------
    def play_url(self, url: str) -> ET.Element:
        """
        Ask the player to play an arbitrary **stream URL**.
        WARNING: This typically yields `state=stream` and sparse metadata.
        Prefer `call_action_path()` using a `playURL` / `autoplayURL` from `/Browse`.
        """
        enc = quote(url, safe="")
        full = f"{self.base}/Play?url={enc}"
        r = self._request_with_retries("GET", full, timeout=self._timeout_tuple())
        return ET.fromstring(r.text)

    # -------------------------------------------------------------------------
    # Browse / actions (preferred path for rich metadata)
    # -------------------------------------------------------------------------
    def browse(self, key: str) -> ET.Element:
        """
        Call /Browse with a provided key (e.g., 'LocalMusic:' or 'LocalMusic:/path').
        Use results' `playURL` / `autoplayURL` to add/play with full metadata.
        """
        r = self._request_with_retries(
            "GET",
            f"{self.base}/Browse",
            params={"key": key},
            timeout=self._timeout_tuple(),
        )
        return ET.fromstring(r.text)

    def call_action_path(self, path: str) -> ET.Element:
        """
        Invoke a returned playURL/actionURL from a Browse item (e.g., '/Add?...', '/Play?...').
        This is the **correct** way to start playback with metadata.
        """
        path = (path or "").strip()
        if not path:
            raise ValueError("path is required")
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            if not path.startswith('/'):
                path = '/' + path
            url = f"{self.base}{path}"
        r = self._request_with_retries("GET", url, timeout=self._timeout_tuple())
        try:
            return ET.fromstring(r.text)
        except Exception:
            root = ET.Element('result')  # Some actions return plain text
            root.text = r.text
            return root

    def play_from_browse_items(
        self,
        browse_root: ET.Element,
        match_predicate: Optional[Callable[[ET.Element], bool]] = None,
        prefer_autoplay: bool = True,
    ) -> Optional[ET.Element]:
        """
        Convenience: within a `/Browse` XML root, find the first item that matches
        `match_predicate(item) -> bool`, then invoke its autoplay/play action.
        Returns the action's XML response, or None if no match.
        """
        if match_predicate is None:
            match_predicate = lambda el: el.tag == "item"  # first playable item

        for el in browse_root.iter():
            if el.tag != "item":
                continue
            if not match_predicate(el):
                continue

            attrs = el.attrib
            # Choose action URLs in a sane preference order
            candidates: Iterable[str] = (
                p for p in [
                    (attrs.get("autoplayURL") or attrs.get("autoplayPath")) if prefer_autoplay else None,
                    attrs.get("playURL") or attrs.get("actionURL"),
                    (attrs.get("autoplayURL") or attrs.get("autoplayPath")) if not prefer_autoplay else None,
                ] if p is not None
            )
            for p in candidates:
                try:
                    return self.call_action_path(p)
                except Exception as e:
                    logger.debug(f"call_action_path failed for '{p}': {e}")
                    continue
        return None

    # -------------------------------------------------------------------------
    # Presets / Volume
    # -------------------------------------------------------------------------
    def volume(
        self,
        level: Optional[int] = None,
        mute: Optional[bool] = None,
        db: Optional[float] = None,
        abs_db: Optional[float] = None,
        tell_slaves: Optional[int] = None,
    ) -> ET.Element:
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

    def presets(self) -> ET.Element:
        r = self._get("/Presets")
        return ET.fromstring(r.text)

    def load_preset(self, preset_id: str | int) -> ET.Element:
        r = self._get("/Preset", {"id": str(preset_id)})
        return ET.fromstring(r.text)

    # -------------------------------------------------------------------------
    # Utilities: robust parsing for Status + Last.fm mapping
    # -------------------------------------------------------------------------
    @staticmethod
    def _txt(root: ET.Element, tag: str) -> Optional[str]:
        """Helper: fetch child text if present."""
        el = root.find(tag)
        return el.text if el is not None else None

    @staticmethod
    def status_to_dict(root: ET.Element) -> Dict[str, Any]:
        """
        Extract a **robust** status dict.
        - Uses title1/title2/title3 (and twoline_* if present) as the canonical lines,
          per BluOS API guidance.
        - Computes `is_stream` from state/streamUrl.
        - Produces Last.fm-friendly `track/artist/album` with safe fallbacks.
        """
        t = BluOSClient._txt  # alias

        # Raw canonical now-playing lines (per spec)
        line1 = t(root, "title1") or t(root, "twoline_title1") or t(root, "name") or t(root, "title") or t(root, "song")
        line2 = t(root, "title2") or t(root, "twoline_title2")
        line3 = t(root, "title3")

        # Other raw fields (kept for reference; not authoritative for text)
        raw_artist = t(root, "artist")
        raw_album = t(root, "album")
        raw_service = t(root, "service")
        raw_state = t(root, "state") or ""
        stream_url = t(root, "streamUrl")
        image = t(root, "image") or t(root, "radioImage") or t(root, "stationImage")

        # Numeric-ish
        def _to_int(x: Optional[str]) -> Optional[int]:
            try:
                return int(x) if x is not None else None
            except Exception:
                return None

        secs = _to_int(t(root, "secs"))
        totlen = _to_int(t(root, "totlen"))
        volume = _to_int(t(root, "volume"))
        shuffle = t(root, "shuffle")
        repeat = t(root, "repeat")
        etag = root.attrib.get("etag")
        sync_stat = t(root, "syncStat")

        # Stream detection (queue vs. raw stream)
        is_stream = (raw_state.lower() == "stream") or bool(stream_url)

        # ---------- Last.fm-friendly mapping ----------
        # Primary heuristic:
        #   - For queue-based playback (LocalMusic/services via queue), mapping is often:
        #       line1 = track, line2 = artist, line3 = album
        #   - For streams (internet radio or `/Play?url=`), sometimes line1 encodes "Artist - Track".
        #
        # We implement:
        #   1) Default: track=line1, artist=line2, album=line3
        #   2) If artist missing and we're in stream mode and line1 like "Artist - Track",
        #      then split and assign (artist, track) accordingly.
        #   3) If still missing artist, fall back to raw_artist.
        #   4) If album missing, fall back to raw_album.
        track = (line1 or "") if line1 else None
        artist = line2 or None
        album = line3 or None

        if (not artist) and is_stream and line1 and (" - " in line1):
            # Radio-style "Artist - Track" (most common); prefer that orientation first.
            a, b = [s.strip() for s in line1.split(" - ", 1)]
            # Heuristic: if line2 equals station/service name (often not artist),
            # we'll prefer the split result as artist/track.
            if a and b:
                artist = a
                track = b

        # Fallbacks from raw tags when sane
        if not artist and raw_artist:
            artist = raw_artist
        if not album and raw_album:
            album = raw_album

        # Build dict
        d: Dict[str, Any] = {
            # Canonical now-playing lines
            "title1": line1,
            "title2": line2,
            "title3": line3,

            # Normalized Last.fm-ish fields
            "track": track,
            "artist": artist,
            "album": album,

            # Misc status
            "service": raw_service,
            "state": raw_state,
            "image": image,
            "streamUrl": stream_url,
            "is_stream": is_stream,
            "secs": secs,
            "totlen": totlen,
            "volume": volume,
            "shuffle": shuffle,
            "repeat": repeat,
            "etag": etag,
            "syncStat": sync_stat,
        }
        return d

    def now_playing(self, timeout: Optional[int] = None, etag: Optional[str] = None) -> Dict[str, Any]:
        """
        Convenience: fetch /Status and return the robust dict from `status_to_dict`.
        """
        root = self.status(timeout=timeout, etag=etag)
        return self.status_to_dict(root)

