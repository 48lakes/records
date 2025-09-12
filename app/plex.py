from __future__ import annotations
import os
import logging
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode, quote
import requests
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


class PlexClient:
    """Lightweight Plex HTTP API client focused on Music library lookups.

    Configuration via environment variables:
      - PLEX_URL (e.g., http://127.0.0.1:32400)
      - PLEX_TOKEN (required)
      - PLEX_SECTION_ID (optional; music section id)
    """

    def __init__(self):
        base = (os.getenv("PLEX_URL") or "").strip().rstrip("/")
        token = (os.getenv("PLEX_TOKEN") or "").strip()
        if not base or not token:
            raise RuntimeError("PLEX_URL and PLEX_TOKEN are required for Plex integration")
        self.base_url = base
        self.token = token
        self._machine_id: Optional[str] = None
        self._music_section_id: Optional[str] = (os.getenv("PLEX_SECTION_ID") or "").strip() or None

    # --- HTTP helpers ---
    def _headers(self) -> Dict[str, str]:
        return {
            "X-Plex-Token": self.token,
            "X-Plex-Product": "records-app",
            "X-Plex-Version": "1.0",
            "X-Plex-Client-Identifier": "records-app",
            "Accept": "application/xml",
        }

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> ET.Element:
        url = f"{self.base_url}{path}"
        try:
            r = requests.get(url, headers=self._headers(), params=params or {}, timeout=10)
            r.raise_for_status()
            return ET.fromstring(r.text)
        except Exception as e:
            logger.error(f"Plex GET failed {url}: {e}")
            raise

    # --- Discovery ---
    def machine_id(self) -> str:
        if self._machine_id:
            return self._machine_id
        root = self._get("/identity")
        mid = root.attrib.get("machineIdentifier") or ""
        if not mid:
            # Some servers put it in MediaContainer
            mid = root.find(".").attrib.get("machineIdentifier", "")
        if not mid:
            raise RuntimeError("Unable to determine Plex machineIdentifier")
        self._machine_id = mid
        return mid

    def music_section_id(self) -> str:
        if self._music_section_id:
            return self._music_section_id
        root = self._get("/library/sections")
        # Find a Directory with type="artist" (Music library)
        for elem in root.findall("Directory"):
            if elem.attrib.get("type") in ("artist", "music"):
                self._music_section_id = elem.attrib.get("key")
                break
        if not self._music_section_id:
            # Fallback: first Directory of type artist
            for elem in root.iter():
                if elem.tag == "Directory" and elem.attrib.get("type") == "artist":
                    self._music_section_id = elem.attrib.get("key")
                    break
        if not self._music_section_id:
            raise RuntimeError("Unable to find Plex Music library section (type=artist)")
        return self._music_section_id

    # --- Lookups ---
    def search_album(self, artist_display: str, album_title: str) -> Optional[Dict[str, Any]]:
        """Search for an album (type=9) by artist and album title.
        Returns minimal metadata: ratingKey, title, parentTitle, year.
        """
        section_id = self.music_section_id()
        params = {
            "type": 9,  # album
            "artist": artist_display,
            "album": album_title,
        }
        root = self._get(f"/library/sections/{section_id}/search", params=params)
        # Albums are typically in Directory elements
        best = None
        for elem in root.findall("Directory"):
            if elem.attrib.get("type") not in ("album",):
                continue
            item = {
                "ratingKey": elem.attrib.get("ratingKey"),
                "key": elem.attrib.get("key"),
                "title": elem.attrib.get("title"),
                "parentTitle": elem.attrib.get("parentTitle"),
                "year": elem.attrib.get("year"),
            }
            # Prefer exact-ish matches
            if (item["title"] or "").lower() == (album_title or "").lower():
                best = item
                break
            if not best:
                best = item
        return best

    def album_tracks(self, album_rating_key: str) -> List[Dict[str, Any]]:
        """Return tracks under an album ratingKey."""
        root = self._get(f"/library/metadata/{album_rating_key}/children")
        tracks: List[Dict[str, Any]] = []
        # Metadata elements of type="track"
        for elem in root.findall("Metadata"):
            if elem.attrib.get("type") != "track":
                continue
            tracks.append({
                "ratingKey": elem.attrib.get("ratingKey"),
                "title": elem.attrib.get("title"),
                "index": elem.attrib.get("index"),  # track number
                "parentIndex": elem.attrib.get("parentIndex"),  # disc number
                "duration": elem.attrib.get("duration"),
                "key": elem.attrib.get("key"),
            })
        return tracks

    # --- URL builders ---
    def web_album_url(self, rating_key: str) -> str:
        # Local web UI on the server
        machine = self.machine_id()
        key = f"/library/metadata/{rating_key}"
        return f"{self.base_url}/web/index.html#!/server/{machine}/details?key={quote(key, safe='') }"

    def web_track_url(self, rating_key: str) -> str:
        machine = self.machine_id()
        key = f"/library/metadata/{rating_key}"
        return f"{self.base_url}/web/index.html#!/server/{machine}/details?key={quote(key, safe='') }"

    # For completeness, allow direct metadata URL
    def metadata_url(self, rating_key: str) -> str:
        return f"{self.base_url}/library/metadata/{rating_key}?{urlencode({'X-Plex-Token': self.token})}"


def normalize_title(s: str) -> str:
    try:
        import re
        s = (s or "").lower().strip()
        s = re.sub(r"\s*[\-–—:]\s*", " ", s)  # unify separators
        s = re.sub(r"\s*\(.*?\)\s*", " ", s)  # remove parenthetical
        s = re.sub(r"[^a-z0-9\s]", "", s)
        s = re.sub(r"\s+", " ", s)
        return s.strip()
    except Exception:
        return (s or "").lower().strip()

    # --- Track part helpers ---
    def track_part(self, rating_key: str) -> Optional[Dict[str, Any]]:
        """Get first media Part for a track ratingKey.
        Returns dict with keys: key, container, file, size, codec.
        """
        root = self._get(f"/library/metadata/{rating_key}")
        # Look for first Part element
        part = None
        # Typical structure: MediaContainer -> Metadata (type=track) -> Media -> Part
        for md in root.findall("Metadata"):
            for media in md.findall("Media"):
                p = media.find("Part")
                if p is not None:
                    part = p
                    break
            if part is not None:
                break
        if part is None:
            # Fallback: search any Part anywhere
            part = root.find(".//Part")
        if part is None:
            return None
        return {
            "key": part.attrib.get("key"),
            "container": part.attrib.get("container"),
            "file": part.attrib.get("file"),
            "size": part.attrib.get("size"),
            "codec": part.attrib.get("codec"),
        }

    def part_url(self, part_key: str) -> str:
        return f"{self.base_url}{part_key}"
