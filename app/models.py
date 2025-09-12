from __future__ import annotations
from sqlalchemy.orm import declarative_base, relationship, Mapped, mapped_column
from sqlalchemy import Integer, String, Text, Column, DateTime, ForeignKey
from sqlalchemy.sql import func

Base = declarative_base()

class Artist(Base):
    __tablename__ = "artists"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    records = relationship("Record", back_populates="artist")

class Record(Base):
    __tablename__ = "records"
    
    # Based on the SQL error, these are the actual columns in your database:
    id = mapped_column(Integer, primary_key=True, index=True)
    discogs_id = mapped_column(Integer, unique=True, index=True)
    title = mapped_column(String, index=True)
    artist_name = mapped_column(String, index=True)  # Note: artist_name, not artist
    artist_display_name = mapped_column(String, nullable=True, index=True)
    year = mapped_column(Integer)
    # New optional fields for more precise sorting
    original_year = mapped_column(Integer, nullable=True)
    edition_year = mapped_column(Integer, nullable=True)
    label = mapped_column(String)
    country = mapped_column(String)
    format = mapped_column(String)
    genre = mapped_column(String)
    style = mapped_column(String)
    date_added = mapped_column(String, nullable=True)
    mb_release_group_id = mapped_column(String)
    cover_art_url = mapped_column(String)
    cover_thumb_url = mapped_column(String)
    artist_id = mapped_column(Integer)
    # catalog_number = mapped_column(String)  # This column doesn't exist in your DB
    artwork_url = mapped_column(String)  # This was added successfully
    # Tracking columns
    user_modified_at = mapped_column(DateTime, nullable=True)
    last_synced_at = mapped_column(DateTime, nullable=True)
    
    # Add this relationship
    tracks = relationship("Track", back_populates="record", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Record(title='{self.title}', artist_name='{self.artist_name}')>"

    # Add a property to provide backward compatibility
    @property
    def artist(self):
        return self.artist_name
    
    @property 
    def catalog_number(self):
        return None  # Return None since this column doesn't exist

# Add this Track model
class Track(Base):
    __tablename__ = "tracks"
    
    id = Column(Integer, primary_key=True, index=True)
    record_id = Column(Integer, ForeignKey("records.id", ondelete="CASCADE"), nullable=False)
    position = Column(String(10))
    title = Column(String(500))
    duration = Column(String(20))
    track_order = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationship back to Record
    record = relationship("Record", back_populates="tracks")
