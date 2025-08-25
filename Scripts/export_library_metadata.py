#!/usr/bin/env python3
"""
Export Plex music library metadata to CSV.

Reads Plex credentials from environment:
  PLEX_BASEURL (preferred) or PLEX_URL
  PLEX_TOKEN   (preferred) or PLEX_API_TOKEN

Writes CSV to:
  OUTPUT_CSV (if set) else "plex_music_exported_details.csv"
"""

import os
import sys
import csv
from collections import defaultdict
from plexapi.server import PlexServer

# --- Console encoding safety (Windows) ---
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# --- Config from environment ---
PLEX_BASEURL = os.environ.get("PLEX_BASEURL") or os.environ.get("PLEX_URL")
PLEX_TOKEN   = os.environ.get("PLEX_TOKEN")   or os.environ.get("PLEX_API_TOKEN")
OUTPUT_CSV   = os.environ.get("OUTPUT_CSV", "plex_music_exported_details.csv")

if not PLEX_BASEURL or not PLEX_TOKEN:
    sys.stderr.write("ERROR: Missing PLEX_BASEURL/PLEX_TOKEN (or PLEX_URL/PLEX_API_TOKEN).\n")
    sys.exit(2)

print(f"Connecting to Plex @ {PLEX_BASEURL} ...", flush=True)
plex = PlexServer(PLEX_BASEURL, PLEX_TOKEN)

# ---------------------------------
# Step 1: Map playlists → file path
# ---------------------------------
print("Scanning playlists ...", flush=True)
music_playlists = [pl for pl in plex.playlists() if getattr(pl, "playlistType", "") == "audio"]
print(f"Found {len(music_playlists)} audio playlists.", flush=True)

track_to_playlists = defaultdict(set)
for idx, playlist in enumerate(music_playlists, start=1):
    try:
        for track in playlist.items():
            try:
                media = track.media[0] if getattr(track, "media", None) else None
                part = media.parts[0] if (media and getattr(media, "parts", None)) else None
                file_path = getattr(part, "file", "") if part else ""
                if file_path:
                    track_to_playlists[file_path].add(playlist.title)
            except Exception as e:
                print(f"Playlist '{playlist.title}': skipped a track: {e}", flush=True)
    finally:
        if idx % 5 == 0 or idx == len(music_playlists):
            print(f"  mapped {idx}/{len(music_playlists)} playlists ...", flush=True)

print("✅ Playlist mapping complete.", flush=True)

# ---------------------------------
# Step 2: Extract full track metadata
# ---------------------------------
music_library = next((s for s in plex.library.sections() if getattr(s, "TYPE", "") == "artist"), None)
if music_library is None:
    sys.stderr.write("🎵 No music library (artist-type section) found.\n")
    sys.exit(3)

artists = music_library.search()
print(f"Scanning {len(artists)} artists ...", flush=True)

header = [
    "Filename", "Title", "Track_Artist", "Album_Artist", "Album",
    "Track_ID", "Media_ID", "Artist_ID",
    "Track_Genres", "Album_Genres", "Artist_Genres",
    "Date", "Date_Cleaned", "Bitrate", "Duration",
    "Track #", "Disc #", "File Type", "File Size (MB)",
    "Date Created", "Date Modified",
    "Artist_Collections", "Album_Collections", "Mood",
    "Playlists", "User_Rating", "Play_Count", "Track_Popularity", "Last_Played", "Labels", "Lyrics"
]

def _safe_join(tags):
    try:
        return ", ".join(t.tag for t in (tags or []))
    except Exception:
        return ""

def _safe_date_str(d):
    try:
        return str(d) if d else ""
    except Exception:
        return ""

def _track_genres_from_xml(track):
    try:
        nodes = track._data.findall("Genre")
        return ", ".join(n.attrib.get("tag", "") for n in nodes) if nodes else ""
    except Exception:
        try:
            return ", ".join(g.tag for g in (getattr(track, "genres", None) or []))
        except Exception:
            return ""

total_written = 0

with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(header)

    for a_idx, artist in enumerate(artists, start=1):
        try:
            artist_genres = _safe_join(getattr(artist, "genres", None))
            artist_collections = _safe_join(getattr(artist, "collections", None))

            albums = artist.albums()
            for album in albums:
                try:
                    album_genres = _safe_join(getattr(album, "genres", None))
                    album_collections = _safe_join(getattr(album, "collections", None))

                    tracks = album.tracks()
                    for track in tracks:
                        try:
                            media = track.media[0] if getattr(track, "media", None) else None
                            part = media.parts[0] if (media and getattr(media, "parts", None)) else None

                            file_path = getattr(part, "file", "") if part else ""
                            title = getattr(track, "title", "") or ""

                            # Names
                            album_artist = getattr(track, "grandparentTitle", "") or ""
                            track_artist = getattr(track, "originalTitle", "") or ""
                            album_name  = getattr(track, "parentTitle", "") or ""

                            # IDs
                            track_id = getattr(track, "ratingKey", "") or ""
                            media_id = getattr(media, "id", "") if media else ""
                            artist_id = getattr(track, "grandparentRatingKey", "") or ""

                            # Genres
                            track_genres = _track_genres_from_xml(track)

                            # Dates
                            album_date = getattr(album, "originallyAvailableAt", None)
                            date_cleaned = getattr(album_date, "year", "") if album_date else ""
                            date_str = _safe_date_str(album_date)

                            # Duration (ms → M:SS)
                            duration_ms = getattr(track, "duration", 0) or 0
                            duration_seconds = int(duration_ms / 1000) if duration_ms else 0
                            minutes = duration_seconds // 60
                            seconds = duration_seconds % 60
                            duration_cleaned = f"{minutes}:{str(seconds).zfill(2)}" if duration_seconds else ""

                            # File details
                            file_size_bytes = getattr(part, "size", 0) or 0
                            file_size_mb = round(file_size_bytes / (1024 * 1024), 1) if file_size_bytes else 0
                            file_type = getattr(part, "container", "") if part else ""
                            bitrate = getattr(media, "bitrate", "") if media else ""

                            # Timestamps
                            date_created = getattr(track, "addedAt", "")
                            date_modified = getattr(track, "updatedAt", "")

                            # Moods / labels
                            moods  = _safe_join(getattr(track, "moods", None))
                            labels = _safe_join(getattr(track, "labels", None))

                            # Playlists (by file path)
                            playlists = ", ".join(sorted(track_to_playlists[file_path])) if file_path in track_to_playlists else ""

                            # User stats
                            user_rating    = getattr(track, "userRating", "") or ""
                            play_count     = getattr(track, "viewCount", 0) or 0
                            rating_count   = getattr(track, "ratingCount", 0) or 0  # NEW → Track_Popularity
                            last_played    = getattr(track, "lastViewedAt", "") or ""

                            # Disc/track numbers
                            track_num = getattr(track, "index", "") or ""
                            disc_num  = getattr(track, "parentIndex", "") or ""

                            row = [
                                file_path, title, track_artist, album_artist, album_name,
                                track_id, media_id, artist_id,
                                track_genres, album_genres, artist_genres,
                                date_str, date_cleaned, bitrate, duration_cleaned,
                                track_num, disc_num, file_type, file_size_mb,
                                date_created, date_modified,
                                artist_collections, album_collections, moods,
                                playlists, user_rating, play_count, int(rating_count), last_played, labels, getattr(track, "lyrics", "") or ""
                            ]
                            writer.writerow(row)
                            total_written += 1

                        except Exception as e:
                            print(f"⚠️ Skipped a track due to error: {e}", flush=True)
                            continue

                except Exception as e:
                    print(f"⚠️ Skipped an album due to error: {e}", flush=True)
                    continue

            if a_idx % 10 == 0 or a_idx == len(artists):
                print(f"  processed {a_idx}/{len(artists)} artists ... (tracks so far: {total_written})", flush=True)

        except Exception as e:
            print(f"⚠️ Skipped an artist due to error: {e}", flush=True)
            continue

print(f"✅ Export complete: {total_written} tracks written to '{OUTPUT_CSV}'.", flush=True)
