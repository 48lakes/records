from __future__ import annotations
import os, time, httpx, requests
from typing import List, Dict, Any, Optional
from types import SimpleNamespace

class DiscogsClient:
    def __init__(self):
        token = os.getenv("DISCOGS_TOKEN","").strip()
        ua = os.getenv("DISCOGS_USER_AGENT","").strip()
        self.username = os.getenv("DISCOGS_USERNAME","").strip()
        if not token: raise ValueError("Missing DISCOGS_TOKEN")
        if not ua: raise ValueError("Missing DISCOGS_USER_AGENT")
        if not self.username: raise ValueError("Missing DISCOGS_USERNAME")
        self.headers = {"User-Agent": ua, "Authorization": f"Discogs token={token}"}
        self.base = "https://api.discogs.com"

    async def fetch_collection(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page, per_page = 1, 100
        async with httpx.AsyncClient(headers=self.headers, timeout=30) as client:
            while True:
                url = f"{self.base}/users/{self.username}/collection/folders/0/releases?page={page}&per_page={per_page}"
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
                for it in data.get("releases", []):
                    b = it.get("basic_information", {})
                    artist = (b.get("artists") or [{}])[0].get("name")
                    # Strip Discogs numeric disambiguator e.g. "Crown (9)" -> "Crown"
                    try:
                        import re
                        artist_display = re.sub(r"\s*\(\d+\)\s*$", "", artist or "").strip()
                    except Exception:
                        artist_display = artist
                    fmt = ", ".join([f.get("name","") for f in b.get("formats", []) if isinstance(f, dict)])
                    out.append({
                        "discogs_id": b.get("id"),
                        "title": b.get("title"),
                        "artist_name": artist,
                        "artist_display_name": artist_display or artist,
                        # edition_year is the specific release year from Discogs; keep legacy 'year' too
                        "year": b.get("year"),
                        "edition_year": b.get("year"),
                        # original_year will be filled later via MusicBrainz when available
                        "original_year": None,
                        "label": ", ".join([l.get("name","") for l in b.get("labels", [])]),
                        "country": b.get("country"),
                        "format": fmt or None,
                        "genre": ", ".join(b.get("genres", []) or []) or None,
                        "style": ", ".join(b.get("styles", []) or []) or None,
                        "date_added": it.get("date_added"),
                        "mb_release_group_id": None,
                        "cover_art_url": b.get("cover_image") or b.get("thumb"),
                        "cover_thumb_url": None,
                        "artist_id": None
                    })
                if page >= data.get("pagination",{}).get("pages",1): break
                page += 1
                if page > 50: break
        return out

    # Synchronous search used by server endpoints
    def search(self, type: str = 'release', q: Optional[str] = None, artist: Optional[str] = None,
               release_title: Optional[str] = None, per_page: int = 10) -> List[Any]:
        """Search Discogs and return lightweight results with images.
        Returns a list of objects with attributes: title, images (list of dicts with uri/uri150/width/height/type).
        """
        params: Dict[str, Any] = {
            'type': type,
            'per_page': per_page,
            'page': 1,
        }
        if q:
            params['q'] = q
        if artist:
            params['artist'] = artist
        if release_title:
            params['release_title'] = release_title

        url = f"{self.base}/database/search"
        headers = self.headers
        results: List[Any] = []

        # Fetch search with simple backoff
        for attempt in range(3):
            r = requests.get(url, headers=headers, params=params, timeout=20)
            if r.status_code == 429:
                ra = r.headers.get('Retry-After')
                sleep_for = float(ra) if ra else (attempt + 1) * 0.75
                time.sleep(sleep_for)
                continue
            if 500 <= r.status_code < 600:
                time.sleep((attempt + 1) * 0.5)
                continue
            r.raise_for_status()
            break
        data = r.json() or {}
        for res in data.get('results', [])[:per_page]:
            # Only process releases/masters
            rtype = res.get('type')
            rid = res.get('id')
            title = res.get('title')
            resource_url = res.get('resource_url')
            if not resource_url or rtype not in ('release', 'master'):
                continue

            # Fetch full details to get images array
            try:
                # Gentle rate-limit between detail requests
                time.sleep(0.25)
                det = None
                for attempt in range(3):
                    det = requests.get(resource_url, headers=headers, timeout=20)
                    if det.status_code == 429:
                        ra = det.headers.get('Retry-After')
                        sleep_for = float(ra) if ra else (attempt + 1) * 0.75
                        time.sleep(sleep_for)
                        continue
                    if 500 <= det.status_code < 600:
                        time.sleep((attempt + 1) * 0.5)
                        continue
                    break
                if not det or not det.ok:
                    images = []
                else:
                    djson = det.json() or {}
                    images = djson.get('images') or []
            except Exception:
                images = []

            results.append(SimpleNamespace(title=title, images=images))

        return results

    def fetch_release(self, discogs_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single release by Discogs ID and map to record dict."""
        try:
            url = f"{self.base}/releases/{discogs_id}"
            r = requests.get(url, headers=self.headers, timeout=20)
            if not r.ok:
                return None
            d = r.json() or {}
            artist = (d.get('artists') or [{}])[0].get('name')
            try:
                import re
                artist_display = re.sub(r"\s*\(\d+\)\s*$", "", artist or "").strip()
            except Exception:
                artist_display = artist
            fmt = ", ".join([f.get("name","") for f in d.get("formats", []) if isinstance(f, dict)])
            labels = ", ".join([l.get("name","") for l in d.get("labels", []) if isinstance(l, dict)])
            genres = ", ".join(d.get("genres", []) or []) or None
            styles = ", ".join(d.get("styles", []) or []) or None
            images = d.get('images') or []
            cover = None
            if images:
                # Prefer primary/front
                prim = [im for im in images if im.get('type') == 'primary']
                use = prim[0] if prim else images[0]
                cover = use.get('uri') or use.get('resource_url')
            return {
                "discogs_id": d.get('id'),
                "title": d.get('title'),
                "artist_name": artist,
                "artist_display_name": artist_display,
                "year": d.get('year'),
                "label": labels,
                "country": d.get('country'),
                "format": fmt or None,
                "genre": genres,
                "style": styles,
                "mb_release_group_id": None,
                "cover_art_url": cover,
                "cover_thumb_url": None,
                "artist_id": None
            }
        except Exception:
            return None
