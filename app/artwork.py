from __future__ import annotations
import os
import asyncio
import random
from pathlib import Path
from typing import Dict, Any, Optional
import io
import httpx
from PIL import Image

STATIC_DIR = Path(__file__).parent / "static"
ART_DIR = STATIC_DIR / "artwork"
THUMB_DIR = STATIC_DIR / "thumbs"

def ensure_dirs():
    ART_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

def _ua() -> str:
    return os.getenv("DISCOGS_USER_AGENT", "records-app/1.0 (+local)")

async def _fetch_bytes(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30, max_attempts: int = 4) -> Optional[bytes]:
    if not url:
        return None
    h = {"User-Agent": _ua()}
    if headers:
        h.update(headers)
    attempt = 0
    async with httpx.AsyncClient(headers=h, timeout=timeout, follow_redirects=True) as client:
        while attempt < max_attempts:
            attempt += 1
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    return r.content
                if r.status_code in (429, 500, 502, 503, 504):
                    ra = r.headers.get("Retry-After")
                    try:
                        wait = float(ra) if ra else 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                    except ValueError:
                        wait = 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                    await asyncio.sleep(wait)
                    continue
                return None
            except httpx.HTTPError:
                wait = 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                await asyncio.sleep(wait)
                continue
    return None

async def _mb_search_release_group(artist: str, title: str) -> Optional[dict]:
    if not artist or not title:
        return None
    q_artist = artist.replace('"', '""')
    q_title = title.replace('"', '""')
    url = f"https://musicbrainz.org/ws/2/release-group/?query=artist:\"{q_artist}\" AND release:\"{q_title}\"&fmt=json&limit=1"
    attempt = 0
    while attempt < 4:
        attempt += 1
        try:
            async with httpx.AsyncClient(headers={"User-Agent": _ua()}, timeout=30) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    data = r.json()
                    rgs = data.get("release-groups", [])
                    if not rgs:
                        return None
                    rg = rgs[0]
                    rgid = rg.get("id")
                    frd = (rg.get("first-release-date") or "").strip()
                    year = None
                    if frd:
                        # Expect formats like YYYY or YYYY-MM-DD
                        try:
                            year = int(frd[:4])
                        except Exception:
                            year = None
                    return {"id": rgid, "first_release_year": year}
                if r.status_code in (429, 500, 502, 503, 504):
                    ra = r.headers.get("Retry-After")
                    try:
                        wait = float(ra) if ra else 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                    except ValueError:
                        wait = 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                    await asyncio.sleep(wait)
                    continue
                return None
        except httpx.HTTPError:
            await asyncio.sleep(0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.2))
            continue
    return None

async def _mb_cover_art_rg(mb_rgid: str) -> Optional[bytes]:
    # Try 1200px first for high-res; fall back to 500 if unavailable
    for size in (1200, 500):
        url = f"https://coverartarchive.org/release-group/{mb_rgid}/front-{size}"
        data = await _fetch_bytes(url)
        if data:
            return data
    # Last resort: metadata endpoint -> image link
    meta_url = f"https://coverartarchive.org/release-group/{mb_rgid}"
    attempt = 0
    while attempt < 4:
        attempt += 1
        try:
            async with httpx.AsyncClient(headers={"User-Agent": _ua()}, timeout=30) as client:
                r = await client.get(meta_url)
                if r.status_code == 200:
                    j = r.json()
                    images = j.get("images", [])
                    for im in images:
                        if im.get("front") and im.get("image"):
                            b = await _fetch_bytes(im.get("image"))
                            if b:
                                return b
                    return None
                if r.status_code in (429, 500, 502, 503, 504):
                    ra = r.headers.get("Retry-After")
                    try:
                        wait = float(ra) if ra else 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                    except ValueError:
                        wait = 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                    await asyncio.sleep(wait)
                    continue
                return None
        except httpx.HTTPError:
            await asyncio.sleep(0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.2))
            continue
    return None

def _save_scaled(img_bytes: bytes, full_path: Path, full_target_px: int, thumb_path: Path, thumb_px: int = 150) -> None:
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    # Full image scaled to square full_target_px (fit, then pad/crop center)
    im_full = im.copy()
    im_full.thumbnail((full_target_px, full_target_px), Image.LANCZOS)
    im_full.save(full_path, format="JPEG", quality=90)
    # Thumb
    im_thumb = im.copy()
    im_thumb.thumbnail((thumb_px, thumb_px), Image.LANCZOS)
    im_thumb.save(thumb_path, format="JPEG", quality=85)

async def enrich_with_artwork(
    rec: Dict[str, Any],
    existing: Optional[Dict[str, Any]] = None,
    *,
    force_artwork: bool = False
) -> Dict[str, Any]:
    """Best-effort enrichment for Discogs records.

    When ``existing`` is provided and ``force_artwork`` is ``False`` the existing local
    artwork paths are preserved and we avoid re-fetching images from external
    services. This keeps previously cached artwork intact unless the caller opts in
    to an update.
    """
    ensure_dirs()
    discogs_id = rec.get("discogs_id")
    artist = rec.get("artist_name") or ""
    title = rec.get("title") or ""
    mb_rgid: Optional[str] = None
    mb_first_year: Optional[int] = None
    img_bytes: Optional[bytes] = None

    existing_cover = None
    existing_thumb = None
    if existing:
        existing_cover = existing.get("cover_art_url") or existing.get("artwork_url")
        existing_thumb = existing.get("cover_thumb_url")
        # Preserve any previously known MB metadata before lookup runs.
        if not rec.get("mb_release_group_id") and existing.get("mb_release_group_id"):
            rec["mb_release_group_id"] = existing.get("mb_release_group_id")
        if not rec.get("original_year") and existing.get("original_year"):
            rec["original_year"] = existing.get("original_year")

    need_mb_lookup = not rec.get("mb_release_group_id") or not rec.get("original_year")
    if need_mb_lookup:
        try:
            mb_info = await _mb_search_release_group(artist, title)
            if mb_info:
                mb_rgid = mb_info.get("id")
                mb_first_year = mb_info.get("first_release_year")
                if mb_rgid:
                    img_bytes = await _mb_cover_art_rg(mb_rgid)
        except Exception:
            mb_rgid = None
    else:
        mb_rgid = rec.get("mb_release_group_id")

    should_fetch_art = force_artwork or not existing_cover or not (existing_cover or "").startswith("/static/")

    if not should_fetch_art and existing_cover:
        # Preserve existing cached artwork without reaching out to external services.
        rec["cover_art_url"] = existing_cover
        if existing_thumb:
            rec["cover_thumb_url"] = existing_thumb
        rec.setdefault("cover_thumb_url", existing_thumb)
        if existing:
            rec.setdefault("artwork_url", existing.get("artwork_url"))
        # Ensure MB info is stored when we skipped fresh artwork.
        if mb_rgid and not rec.get("mb_release_group_id"):
            rec["mb_release_group_id"] = mb_rgid
        if mb_first_year and not rec.get("original_year"):
            rec["original_year"] = mb_first_year
        rec["_artwork_refreshed"] = False
        return rec

    # If we reach here we need artwork. First reuse MB lookup bytes if we already fetched them.
    if not img_bytes:
        img_bytes = await _fetch_bytes(rec.get("cover_art_url") or rec.get("artwork_full") or "")

    # If no image available, return unchanged (but keep mb id if found)
    if not img_bytes:
        if mb_rgid:
            rec["mb_release_group_id"] = mb_rgid
        if mb_first_year and not rec.get("original_year"):
            rec["original_year"] = mb_first_year
        if existing_thumb:
            rec.setdefault("cover_thumb_url", existing_thumb)
        rec["_artwork_refreshed"] = False
        return rec

    # Save under deterministic filenames using discogs_id if present, else hash-like from title
    base = str(discogs_id) if discogs_id else f"{artist}_{title}".replace("/", "_").replace("\\", "_")
    full_path = ART_DIR / f"{base}.jpg"
    thumb_path = THUMB_DIR / f"{base}_150.jpg"
    _save_scaled(img_bytes, full_path, full_target_px=600, thumb_path=thumb_path, thumb_px=150)

    rec["mb_release_group_id"] = mb_rgid or rec.get("mb_release_group_id")
    if mb_first_year and not rec.get("original_year"):
        rec["original_year"] = mb_first_year
    rec["cover_art_url"] = f"/static/artwork/{full_path.name}"
    rec["cover_thumb_url"] = f"/static/thumbs/{thumb_path.name}"
    rec["_artwork_refreshed"] = True
    return rec
