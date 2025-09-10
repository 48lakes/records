from __future__ import annotations
from sqlalchemy.orm import declarative_base, relationship, Mapped, mapped_column
from sqlalchemy import Integer, String, Text

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
    year = mapped_column(Integer)
    label = mapped_column(String)
    country = mapped_column(String)
    format = mapped_column(String)
    genre = mapped_column(String)
    style = mapped_column(String)
    mb_release_group_id = mapped_column(String)
    cover_art_url = mapped_column(String)
    cover_thumb_url = mapped_column(String)
    artist_id = mapped_column(Integer)
    # catalog_number = mapped_column(String)  # This column doesn't exist in your DB
    artwork_url = mapped_column(String)  # This was added successfully
    
    def __repr__(self):
        return f"<Record(title='{self.title}', artist_name='{self.artist_name}')>"

    # Add a property to provide backward compatibility
    @property
    def artist(self):
        return self.artist_name
    
    @property 
    def catalog_number(self):
        return None  # Return None since this column doesn't exist
