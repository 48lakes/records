from __future__ import annotations
import os, httpx
from typing import List, Dict, Any

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
                    fmt = ", ".join([f.get("name","") for f in b.get("formats", []) if isinstance(f, dict)])
                    out.append({
                        "discogs_id": b.get("id"),
                        "title": b.get("title"),
                        "artist_name": artist,
                        "year": b.get("year"),
                        "label": ", ".join([l.get("name","") for l in b.get("labels", [])]),
                        "country": b.get("country"),
                        "format": fmt or None,
                        "genre": ", ".join(b.get("genres", []) or []) or None,
                        "style": ", ".join(b.get("styles", []) or []) or None,
                        "mb_release_group_id": None,
                        "cover_art_url": b.get("cover_image") or b.get("thumb"),
                        "cover_thumb_url": None,
                        "artist_id": None
                    })
                if page >= data.get("pagination",{}).get("pages",1): break
                page += 1
                if page > 50: break
        return out
