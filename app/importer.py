from __future__ import annotations
import asyncio
import logging
from sqlalchemy.orm import Session
from .discogs_client import DiscogsClient
from .crud import upsert_record
from pathlib import Path
from .artwork import ensure_dirs, enrich_with_artwork

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

async def sync_discogs_collection(db: Session, progress_callback=None) -> int:
    client = DiscogsClient()
    n = 0
    ensure_dirs()
    
    try:
        items = await client.fetch_collection()
        total_items = len(items)
        logger.info(f"Found {total_items} items in Discogs collection")
        
        for rec in items:
            if CANCEL_SYNC:
                logger.info("Sync cancelled by user")
                break
            try:
                # Rate limiting - 1 request per second
                await asyncio.sleep(1)
                
                # Enrich with MusicBrainz/Cover Art Archive and thumbnails
                rec = await enrich_with_artwork(rec)
                upsert_record(db, rec)
                n += 1
                
                if progress_callback:
                    progress = (n / total_items) * 100
                    progress_callback(progress, f"Processing item {n} of {total_items}")
                    
                logger.debug(f"Processed record {rec.get('id')} - {rec.get('title')}")
                
            except Exception as e:
                logger.error(f"Error processing record {rec.get('id')}: {str(e)}")
                # Continue with next record on error
                continue
                
        db.commit()
        logger.info(f"Sync completed. Processed {n} records")
        return n
        
    except Exception as e:
        logger.error(f"Sync failed: {str(e)}")
        db.rollback()
        raise
