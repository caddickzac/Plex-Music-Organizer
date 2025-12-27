#!/usr/bin/env python3
"""
Export Plex music library metadata to CSV.

Reads Plex credentials from environment:
  PLEX_BASEURL (preferred) or PLEX_URL
  PLEX_TOKEN   (preferred) or PLEX_API_TOKEN

Per-track CSV:
  - Always writes a dated file: "YYYY_MM_DD plex_music_exported_details.csv"
  - If OUTPUT_CSV is provided and differs, also writes/copies a compatibility copy there.

Also writes:
  - "YYYY_MM_DD Artist_Album_Info.csv"   (album-level summary)
  - "YYYY_MM_DD Artist_Level_Info.csv"   (artist-level summary)
All summaries are written to the same directory as the track export.
"""

import os
import sys
import csv
import shutil
from collections import defaultdict
from datetime import datetime
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

if not PLEX_BASEURL or not PLEX_TOKEN:
    sys.stderr.write("ERROR: Missing PLEX_BASEURL/PLEX_TOKEN (or PLEX_URL/PLEX_API_TOKEN).\n")
    sys.exit(2)

_date_prefix = datetime.now().strftime("%Y_%m_%d")

# OUTPUT handling:
# 1) Always write dated: YYYY_MM_DD plex_music_exported_details.csv
# 2) If env OUTPUT_CSV is set and is different, also copy the dated file there (compat)
ENV_OUTPUT_CSV = os.environ.get("OUTPUT_CSV")  # may be None
compat_output_csv = None

if ENV_OUTPUT_CSV:
    out_dir = os.path.dirname(os.path.abspath(ENV_OUTPUT_CSV)) or os.getcwd()
    base = os.path.basename(ENV_OUTPUT_CSV)

    if ("plex_music_exported_details" in base.lower()) and (not base.startswith(_date_prefix)):
        OUTPUT_CSV = os.path.join(out_dir, f"{_date_prefix} plex_music_exported_details.csv")
        compat_output_csv = os.path.abspath(ENV_OUTPUT_CSV)
    else:
        # user explicitly wants a particular filename; honor it
        OUTPUT_CSV = os.path.abspath(ENV_OUTPUT_CSV)
        compat_output_csv = None
else:
    out_dir = os.getcwd()
    OUTPUT_CSV = os.path.join(out_dir, f"{_date_prefix} plex_music_exported_details.csv")
    compat_output_csv = None

print(f"Connecting to Plex @ {PLEX_BASEURL} ...", flush=True)
plex = PlexServer(PLEX_BASEURL, PLEX_TOKEN)

# ---------------------------------
# Helpers
# ---------------------------------
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

def _date_only(val) -> str:
    """
    Convert Plex datetime-ish or string to YYYY-MM-DD.
    Returns "" if missing.
    """
    if not val:
        return ""
    # datetime objects in plexapi often support .date()
    try:
        if hasattr(val, "date"):
            return val.date().isoformat()
    except Exception:
        pass

    s = str(val).strip()
    if not s:
        return ""
    # common forms: "2019-06-24 15:17:13" or "2019-06-24T15:17:13Z"
    if "T" in s:
        s = s.split("T", 1)[0]
    else:
        s = s.split(" ", 1)[0]
    return s[:10]

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
    if not s:
        return []
    return [x.strip() for x in str(s).split(",") if x.strip()]

def _sorted_unique_join(items):
    cleaned = sorted({str(x).strip() for x in items if str(x).strip()}, key=lambda x: x.lower())
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

def _avg(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return (sum(vals) / len(vals)) if vals else ""

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

# 2) Add Album_ID to track-level export
header = [
    "Filename", "Title", "Track_Artist", "Album_Artist", "Album",
    "Track_ID", "Media_ID", "Artist_ID", "Album_ID",
    "Track_Genres", "Album_Genres", "Artist_Genres",
    "Date", "Date_Cleaned", "Bitrate", "Duration",
    "Track #", "Disc #", "File Type", "File Size (MB)",
    "Date Created", "Date Modified",
    "Artist_Collections", "Album_Collections", "Track_Collections", "Mood",
    "Playlists", "User_Rating", "Play_Count", "Track_Popularity", "Last_Played", "Labels", "Lyrics"
]

# ---------------------------------
# Album summary accumulator
# ---------------------------------
# Keyed by (Album_Artist, Album)
album_acc = {}  # (album_artist, album_name) -> dict of sets/sums/lists

def _album_bucket(album_artist: str, album_name: str):
    key = (album_artist or "", album_name or "")
    if key not in album_acc:
        album_acc[key] = {
            "album_ids": set(),
            "years": set(),
            "track_count": 0,
            "artist_collections": set(),
            "album_collections": set(),
            "track_collections": set(),
            "playlists": set(),
            "file_types": set(),
            "bitrate_vals": [],
            "file_size_bytes_sum": 0,
            "date_created_dates": set(),  # 3b) YYYY-MM-DD only, unique
        }
    return album_acc[key]

# ---------------------------------
# Artist-level accumulator (NEW)
# ---------------------------------
artist_acc = {}  # artist_name -> dict

def _artist_bucket(artist_name: str):
    k = artist_name or ""
    if k not in artist_acc:
        artist_acc[k] = {
            "albums": set(),
            "years": set(),
            "track_count": 0,
            "artist_collections": set(),
            "bitrate_vals": [],
            "file_size_bytes_sum": 0,
        }
    return artist_acc[k]

total_written = 0

print(f"Writing track export to: {OUTPUT_CSV}", flush=True)
with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(header)

    for a_idx, artist in enumerate(artists, start=1):
        try:
            artist_name = getattr(artist, "title", "") or ""
            artist_genres = _safe_join(getattr(artist, "genres", None))
            artist_collections = _safe_join(getattr(artist, "collections", None))

            # Update artist bucket with collections (stable across tracks)
            ab = _artist_bucket(artist_name)
            for item in _split_csvish(artist_collections):
                ab["artist_collections"].add(item)

            albums = artist.albums()
            for album in albums:
                try:
                    album_name_obj = getattr(album, "title", "") or ""
                    album_genres = _safe_join(getattr(album, "genres", None))
                    album_collections = _safe_join(getattr(album, "collections", None))

                    # 2) Album_ID (prefer album.ratingKey)
                    album_id = getattr(album, "ratingKey", "") or ""

                    # Add album to artist-level album list
                    ab["albums"].add(album_name_obj)

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
                            album_name  = getattr(track, "parentTitle", "") or album_name_obj or ""

                            # IDs
                            track_id = getattr(track, "ratingKey", "") or ""
                            media_id = getattr(media, "id", "") if media else ""
                            artist_id = getattr(track, "grandparentRatingKey", "") or ""
                            # 2) Album_ID: track.parentRatingKey is often album key; fallback to album.ratingKey
                            album_id_track = getattr(track, "parentRatingKey", "") or album_id or ""

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
                            moods             = _safe_join(getattr(track, "moods", None))
                            labels            = _safe_join(getattr(track, "labels", None))
                            track_collections = _safe_join(getattr(track, "collections", None))

                            # Playlists (by file path)
                            playlists = ", ".join(sorted(track_to_playlists[file_path])) if file_path in track_to_playlists else ""

                            # User stats
                            user_rating  = getattr(track, "userRating", "") or ""
                            play_count   = getattr(track, "viewCount", 0) or 0
                            rating_count = getattr(track, "ratingCount", 0) or 0  # Track_Popularity
                            last_played  = getattr(track, "lastViewedAt", "") or ""

                            # Disc/track numbers
                            track_num = getattr(track, "index", "") or ""
                            disc_num  = getattr(track, "parentIndex", "") or ""

                            row = [
                                file_path, title, track_artist, album_artist, album_name,
                                track_id, media_id, artist_id, album_id_track,
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

                            if album_id_track:
                                b["album_ids"].add(str(album_id_track))

                            if date_cleaned:
                                b["years"].add(str(date_cleaned))

                            for item in _split_csvish(artist_collections):
                                b["artist_collections"].add(item)
                            for item in _split_csvish(album_collections):
                                b["album_collections"].add(item)
                            for item in _split_csvish(track_collections):
                                b["track_collections"].add(item)
                            for item in _split_csvish(playlists):
                                b["playlists"].add(item)

                            if file_type:
                                b["file_types"].add(str(file_type).strip())

                            br = _try_float(bitrate)
                            if br is not None:
                                b["bitrate_vals"].append(br)

                            if file_size_bytes:
                                b["file_size_bytes_sum"] += int(file_size_bytes)

                            # 3b) Date_Created as YYYY-MM-DD unique
                            dc = _date_only(date_created)
                            if dc:
                                b["date_created_dates"].add(dc)

                            # -----------------------------
                            # Update artist-level accumulator (NEW)
                            # -----------------------------
                            # Use the Plex artist.title as the grouping key (more stable than track.grandparentTitle)
                            ab = _artist_bucket(artist_name)
                            ab["track_count"] += 1

                            # years: use album_date.year if available
                            if date_cleaned:
                                try:
                                    ab["years"].add(int(date_cleaned))
                                except Exception:
                                    pass

                            br2 = _try_float(bitrate)
                            if br2 is not None:
                                ab["bitrate_vals"].append(br2)

                            if file_size_bytes:
                                ab["file_size_bytes_sum"] += int(file_size_bytes)

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

# If Streamlit passed OUTPUT_CSV=plex_music_exported_details.csv, keep app compatibility:
if compat_output_csv and os.path.abspath(OUTPUT_CSV) != os.path.abspath(compat_output_csv):
    try:
        shutil.copyfile(OUTPUT_CSV, compat_output_csv)
        print(f"ℹ️ Compatibility copy written to '{compat_output_csv}'.", flush=True)
    except Exception as e:
        print(f"⚠️ Could not write compatibility copy to '{compat_output_csv}': {e}", flush=True)

# ---------------------------------
# Step 3: Write Artist_Album_Info CSV (UPDATED)
# ---------------------------------
out_dir = os.path.dirname(os.path.abspath(OUTPUT_CSV)) or os.getcwd()
ALBUM_INFO_CSV = os.path.join(out_dir, f"{_date_prefix} Artist_Album_Info.csv")

album_header = [
    "Album_Artist",
    "Album",
    "Album_ID",          # 3a) add
    "Year",
    "Track_Count",
    "Artist_Collections",
    "Album_Collections",
    "Track_Collections",
    "Playlists",
    "File_Type",
    "Bitrate_Avg",
    "Album_File_MB_Size",
    "Date_Created",      # 3b) now YYYY-MM-DD unique
]

keys_sorted = sorted(album_acc.keys(), key=lambda k: (k[0].lower(), k[1].lower()))

with open(ALBUM_INFO_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(album_header)

    for (album_artist, album_name) in keys_sorted:
        b = album_acc[(album_artist, album_name)]

        # Years: sort numeric when possible
        try:
            years_sorted = sorted({int(y) for y in b["years"] if str(y).strip()})
            year_str = ", ".join(str(y) for y in years_sorted) if years_sorted else ""
        except Exception:
            year_str = _sorted_unique_join(b["years"])

        bitrate_avg = _avg(b["bitrate_vals"])
        album_mb = round(b["file_size_bytes_sum"] / (1024 * 1024), 1) if b["file_size_bytes_sum"] else 0

        w.writerow([
            album_artist,
            album_name,
            _sorted_unique_join(b["album_ids"]),
            year_str,
            b["track_count"],
            _sorted_unique_join(b["artist_collections"]),
            _sorted_unique_join(b["album_collections"]),
            _sorted_unique_join(b["track_collections"]),
            _sorted_unique_join(b["playlists"]),
            _sorted_unique_join(b["file_types"]),
            bitrate_avg,
            album_mb,
            _sorted_unique_join(b["date_created_dates"]),
        ])

print(f"✅ Album summary complete: wrote '{ALBUM_INFO_CSV}'.", flush=True)

# ---------------------------------
# Step 4: Write Artist_Level_Info CSV (NEW)
# ---------------------------------
ARTIST_INFO_CSV = os.path.join(out_dir, f"{_date_prefix} Artist_Level_Info.csv")

artist_header = [
    "Artist",
    "Albums",
    "Years",
    "Album_Count",
    "Track_Count",
    "Artist_Collections",
    "Bitrate_Avg",
    "Bitrate_Min",
    "Bitrate_Max",
    "File_Size_Total_MB",
]

artist_keys_sorted = sorted(artist_acc.keys(), key=lambda x: x.lower())

with open(ARTIST_INFO_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(artist_header)

    for artist_name in artist_keys_sorted:
        a = artist_acc[artist_name]
        albums = sorted({str(x).strip() for x in a["albums"] if str(x).strip()}, key=lambda x: x.lower())
        album_count = len(albums)
        track_count = int(a["track_count"])

        years_int = sorted({y for y in a["years"] if isinstance(y, int)})
        if years_int:
            y_min, y_max = years_int[0], years_int[-1]
            years_str = str(y_min) if y_min == y_max else f"{y_min}-{y_max}"
        else:
            years_str = ""

        bitrates = [v for v in a["bitrate_vals"] if isinstance(v, (int, float))]
        bitrate_avg = _avg(bitrates)
        bitrate_min = min(bitrates) if bitrates else ""
        bitrate_max = max(bitrates) if bitrates else ""

        size_mb = round(a["file_size_bytes_sum"] / (1024 * 1024), 1) if a["file_size_bytes_sum"] else 0

        w.writerow([
            artist_name,
            ", ".join(albums),
            years_str,
            album_count,
            track_count,
            _sorted_unique_join(a["artist_collections"]),
            bitrate_avg,
            bitrate_min,
            bitrate_max,
            size_mb,
        ])

print(f"✅ Artist summary complete: wrote '{ARTIST_INFO_CSV}'.", flush=True)
