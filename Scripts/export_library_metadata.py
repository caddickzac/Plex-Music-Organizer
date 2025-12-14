#!/usr/bin/env python3
"""
Export Plex music library metadata to CSV.

Reads Plex credentials from environment:
  PLEX_BASEURL (preferred) or PLEX_URL
  PLEX_TOKEN   (preferred) or PLEX_API_TOKEN

Writes per-track CSV to:
  OUTPUT_CSV (if set) else "YYYY_MM_DD plex_music_exported_details.csv"

Also writes album-level summary CSV (R-inspired):
  "YYYY_MM_DD Artist_Album_Info.csv"
  (in the same directory as OUTPUT_CSV)
"""

import os
import sys
import csv
from collections import defaultdict
from plexapi.server import PlexServer
from datetime import datetime

# --- Console encoding safety (Windows) ---
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# --- Config from environment ---
PLEX_BASEURL = os.environ.get("PLEX_BASEURL") or os.environ.get("PLEX_URL")
PLEX_TOKEN   = os.environ.get("PLEX_TOKEN")   or os.environ.get("PLEX_API_TOKEN")

_date_prefix = datetime.now().strftime("%Y_%m_%d")
_default_name = f"{_date_prefix} plex_music_exported_details.csv"
OUTPUT_CSV   = os.environ.get("OUTPUT_CSV") or _default_name

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
    "Artist_Collections", "Album_Collections", "Track_Collections", "Mood",
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

def _split_csvish(s: str):
    """
    Split strings that look like "a, b, c" into ["a","b","c"].
    Keeps only non-empty trimmed items.
    """
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]

def _sorted_unique_join(items):
    """
    Join unique items (strings), sorted.
    Returns "" if nothing (after stripping empties).
    """
    cleaned = sorted({str(x).strip() for x in items if str(x).strip()})
    return ", ".join(cleaned) if cleaned else ""

def _try_float(x):
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if not s:
            return None
        return float(s)
    except Exception:
        return None

# ---------------------------------
# Album summary accumulator (R-inspired)
# ---------------------------------
# Keyed by (Album_Artist, Album)
album_acc = {}  # (album_artist, album_name) -> dict of sets/sums/lists

def _album_bucket(album_artist: str, album_name: str):
    key = (album_artist or "", album_name or "")
    if key not in album_acc:
        album_acc[key] = {
            "years": set(),
            "track_count": 0,
            "artist_collections": set(),
            "album_collections": set(),
            "track_collections": set(),
            "playlists": set(),
            "file_types": set(),
            "bitrate_vals": [],
            "file_size_bytes_sum": 0,
            "date_created": set(),
        }
    return album_acc[key]

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

                            # Moods / labels / collections
                            moods               = _safe_join(getattr(track, "moods", None))
                            labels              = _safe_join(getattr(track, "labels", None))
                            track_collections   = _safe_join(getattr(track, "collections", None))

                            # Playlists (by file path)
                            playlists = ", ".join(sorted(track_to_playlists[file_path])) if file_path in track_to_playlists else ""

                            # User stats
                            user_rating    = getattr(track, "userRating", "") or ""
                            play_count     = getattr(track, "viewCount", 0) or 0
                            rating_count   = getattr(track, "ratingCount", 0) or 0  # Track_Popularity
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
                                artist_collections, album_collections, track_collections, moods,
                                playlists, user_rating, play_count, int(rating_count), last_played, labels, getattr(track, "lyrics", "") or ""
                            ]
                            writer.writerow(row)
                            total_written += 1

                            # -----------------------------
                            # Update album-level accumulator
                            # -----------------------------
                            b = _album_bucket(album_artist, album_name)
                            b["track_count"] += 1

                            if date_cleaned:
                                b["years"].add(str(date_cleaned))

                            # Collections are already comma-joined strings in the track export;
                            # we split and re-unique at album level to mimic tidyverse unique()+paste().
                            for item in _split_csvish(artist_collections):
                                b["artist_collections"].add(item)
                            for item in _split_csvish(album_collections):
                                b["album_collections"].add(item)
                            for item in _split_csvish(track_collections):
                                b["track_collections"].add(item)

                            # Playlists string might contain multiple titles; split to unique them.
                            for item in _split_csvish(playlists):
                                b["playlists"].add(item)

                            if file_type:
                                b["file_types"].add(str(file_type).strip())

                            br = _try_float(bitrate)
                            if br is not None:
                                b["bitrate_vals"].append(br)

                            if file_size_bytes:
                                b["file_size_bytes_sum"] += int(file_size_bytes)

                            if date_created:
                                b["date_created"].add(str(date_created))

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

# ---------------------------------
# Step 3: Write R-inspired Artist_Album_Info CSV
# ---------------------------------
out_dir = os.path.dirname(os.path.abspath(OUTPUT_CSV)) or os.getcwd()
ALBUM_INFO_CSV = os.path.join(out_dir, f"{_date_prefix} Artist_Album_Info.csv")

album_header = [
    "Album_Artist",
    "Album",
    "Year",
    "Track_Count",
    "Artist_Collections",
    "Album_Collections",
    "Track_Collections",
    "Playlists",
    "File_Type",
    "Bitrate_Avg",
    "Album_File_MB_Size",
    "Date_Created",
]

def _avg(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return (sum(vals) / len(vals)) if vals else ""

# Sort rows for readability (Album_Artist then Album)
keys_sorted = sorted(album_acc.keys(), key=lambda k: (k[0].lower(), k[1].lower()))

with open(ALBUM_INFO_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(album_header)

    for (album_artist, album_name) in keys_sorted:
        b = album_acc[(album_artist, album_name)]

        # Mimic: Year = paste(sort(unique(Date_Cleaned)), collapse=", ")
        # Sorting years numerically when possible:
        try:
            years_sorted = sorted({int(y) for y in b["years"] if str(y).strip()})
            year_str = ", ".join(str(y) for y in years_sorted) if years_sorted else ""
        except Exception:
            year_str = _sorted_unique_join(b["years"])

        bitrate_avg = _avg(b["bitrate_vals"])
        # File size: sum MB like your R (sum(File.Size..MB.)); we sum bytes then convert
        album_mb = round(b["file_size_bytes_sum"] / (1024 * 1024), 1) if b["file_size_bytes_sum"] else 0

        w.writerow([
            album_artist,
            album_name,
            year_str,
            b["track_count"],
            _sorted_unique_join(b["artist_collections"]),
            _sorted_unique_join(b["album_collections"]),
            _sorted_unique_join(b["track_collections"]),
            _sorted_unique_join(b["playlists"]),
            _sorted_unique_join(b["file_types"]),
            bitrate_avg,
            album_mb,
            _sorted_unique_join(b["date_created"]),
        ])

print(f"✅ Album summary complete: wrote '{ALBUM_INFO_CSV}'.", flush=True)
