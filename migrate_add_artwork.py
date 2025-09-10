#!/usr/bin/env python3

import sys
import os
sys.path.append('/app')

from sqlalchemy import text
from app.db import engine

def add_artwork_url_column():
    """Add artwork_url column to records table"""
    try:
        with engine.connect() as conn:
            # Check if column already exists
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='records' AND column_name='artwork_url'
            """))
            
            if result.fetchone():
                print("artwork_url column already exists")
                return
            
            # Add the column
            conn.execute(text("ALTER TABLE records ADD COLUMN artwork_url VARCHAR"))
            conn.commit()
            print("Successfully added artwork_url column")
            
    except Exception as e:
        print(f"Error adding column: {e}")
        raise

if __name__ == "__main__":
    add_artwork_url_column()