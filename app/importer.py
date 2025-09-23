from __future__ import annotations
import asyncio
import logging
import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .discogs_client import DiscogsClient
from .crud import upsert_record, get_record_by_discogs_id
from .artwork import ensure_dirs, enrich_with_artwork
from .sync_utils import discogs_payload_signature

logger = logging.getLogger(__name__)

"""Importer sync with cancellation support"""

# Global cancel flag (set externally by API to stop ongoing full sync)
CANCEL_SYNC = False

def request_cancel():
    global CANCEL_SYNC
    CANCEL_SYNC = True

def clear_cancel():
    global CANCEL_SYNC
    CANCEL_SYNC = False

def _sanitize_field(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "/").strip()


def _progress_message(kind: str, discogs_id: Any, rec: dict, position: int, total: int) -> str:
    title = _sanitize_field(rec.get("title"))
    artist = _sanitize_field(rec.get("artist_display_name") or rec.get("artist_name"))
    return f"{kind}|{discogs_id}|{title}|{artist}|{position}|{total}"


async def sync_discogs_collection(db: Session, progress_callback=None) -> int:
    client = DiscogsClient()
    ensure_dirs()

    try:
        items = await client.fetch_collection()
        total_items = len(items)
        logger.info(f"Found {total_items} items in Discogs collection")

        processed = 0
        updated = 0

        for position, rec in enumerate(items, start=1):
            if CANCEL_SYNC:
                logger.info("Sync cancelled by user")
                break

            discogs_id = rec.get("discogs_id")
            if not discogs_id:
                logger.warning("Encountered Discogs entry without an id; skipping")
                if progress_callback:
                    progress_callback((position / max(total_items, 1)) * 100, f"Skipping invalid entry {position}")
                continue

            try:
                snapshot, payload_hash = discogs_payload_signature(rec)
                existing = get_record_by_discogs_id(db, discogs_id)

                if existing and existing.get("discogs_payload_hash") == payload_hash:
                    logger.debug(f"Skipping unchanged record {discogs_id}")
                    processed += 1
                    if progress_callback:
                        progress_callback(
                            (position / max(total_items, 1)) * 100,
                            _progress_message("UNCHANGED", discogs_id, rec, position, total_items or 0)
                        )
                    continue

                # Rate limiting - 1 request per second for Discogs/MB lookups
                await asyncio.sleep(1)

                rec = await enrich_with_artwork(rec, existing=existing, force_artwork=False)
                artwork_refreshed = rec.pop("_artwork_refreshed", None)
                rec["discogs_payload"] = json.dumps(snapshot, sort_keys=True)
                rec["discogs_payload_hash"] = payload_hash

                if artwork_refreshed:
                    rec["artwork_synced_at"] = datetime.utcnow()
                else:
                    # Passing NULL keeps the previous artwork timestamp via the UPSERT clause
                    rec["artwork_synced_at"] = None

                upsert_record(db, rec)
                updated += 1
                processed += 1

                if progress_callback:
                    progress_callback(
                        (position / max(total_items, 1)) * 100,
                        _progress_message("UPDATED", discogs_id, rec, position, total_items or 0)
                    )

                logger.debug(f"Processed record {discogs_id} - {rec.get('title')}")

            except Exception as e:
                logger.error(f"Error processing record {discogs_id}: {str(e)}")
                try:
                    db.rollback()
                except Exception:
                    pass
                continue

        db.commit()
        logger.info(f"Sync completed. Updated {updated} records (processed {processed} entries)")
        return updated

    except Exception as e:
        logger.error(f"Sync failed: {str(e)}")
        db.rollback()
        raise
