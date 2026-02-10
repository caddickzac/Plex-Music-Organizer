#!/usr/bin/env python3
"""
Export Plex music library metadata to CSV.
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

# --- Export Limit ---
limit_env = os.environ.get("EXPORT_LIMIT")
EXPORT_LIMIT = int(limit_env) if limit_env and limit_env.isdigit() else 0
if EXPORT_LIMIT > 0:
    print(f"ℹ️ Test Mode: Scanning limit set to {EXPORT_LIMIT} tracks.", flush=True)

# --- Feature Flags ---
DO_PLAYLISTS = os.environ.get("EXPORT_PLAYLISTS", "1") == "1"

if not PLEX_BASEURL or not PLEX_TOKEN:
    sys.stderr.write("ERROR: Missing PLEX_BASEURL/PLEX_TOKEN (or PLEX_URL/PLEX_API_TOKEN).\n")
    sys.exit(2)

_date_prefix = datetime.now().strftime("%Y_%m_%d")

# OUTPUT handling
ENV_OUTPUT_CSV = os.environ.get("OUTPUT_CSV")
compat_output_csv = None

if ENV_OUTPUT_CSV:
    out_dir = os.path.dirname(os.path.abspath(ENV_OUTPUT_CSV)) or os.getcwd()
    base = os.path.basename(ENV_OUTPUT_CSV)
    norm = base.lower().replace(" ", "_")
    if ("track_level_info" in norm) and (not base.startswith(_date_prefix)):
        OUTPUT_CSV = os.path.join(out_dir, f"{_date_prefix} Track_Level_Info.csv")
        compat_output_csv = os.path.abspath(ENV_OUTPUT_CSV)
    else:
        OUTPUT_CSV = os.path.abspath(ENV_OUTPUT_CSV)
        compat_output_csv = None
else:
    out_dir = os.getcwd()
    OUTPUT_CSV = os.path.join(out_dir, f"{_date_prefix} Track_Level_Info.csv")
    compat_output_csv = None

print(f"Connecting to Plex @ {PLEX_BASEURL} ...", flush=True)
try:
    plex = PlexServer(PLEX_BASEURL, PLEX_TOKEN)
except Exception as e:
    sys.stderr.write(f"ERROR: Could not connect to Plex: {e}\n")
    sys.exit(1)

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
    if not val: return ""
    try:
        if hasattr(val, "date"): return val.date().isoformat()
    except Exception: pass
    s = str(val).strip()
    if not s: return ""
    if "T" in s: s = s.split("T", 1)[0]
    else: s = s.split(" ", 1)[0]
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
    if not s: return []
    return [x.strip() for x in str(s).split(",") if x.strip()]

def _sorted_unique_join(items):
    cleaned = sorted({str(x).strip() for x in items if str(x).strip()}, key=lambda x: x.lower())
    return ", ".join(cleaned) if cleaned else ""

def _try_float(x):
    try:
        if x is None: return None
        if isinstance(x, (int, float)): return float(x)
        s = str(x).strip()
        if not s: return None
        return float(s)
    except Exception:
        return None

def _avg(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return (sum(vals) / len(vals)) if vals else ""

def _deep_search_attr(track, candidates):
    """
    Searches for a value across Track -> XML -> Media -> Stream using a list of candidate keys.
    """
    # 1. Search Track Object & Track XML
    for key in candidates:
        val = getattr(track, key, None)
        if val is not None and str(val).strip(): return val
        if hasattr(track, "_data") and hasattr(track._data, "attrib"):
            val = track._data.attrib.get(key)
            if val is not None and str(val).strip(): return val

    # 2. Search Media Objects
    if hasattr(track, "media") and track.media:
        for media in track.media:
            for key in candidates:
                val = getattr(media, key, None)
                if val is not None and str(val).strip(): return val
                if hasattr(media, "_data") and hasattr(media._data, "attrib"):
                    val = media._data.attrib.get(key)
                    if val is not None and str(val).strip(): return val
            
            # 3. Search Parts & Audio Streams
            if hasattr(media, "parts") and media.parts:
                for part in media.parts:
                    if hasattr(part, "streams") and part.streams:
                        for stream in part.streams:
                            if stream.streamType == 2: # Audio stream
                                for key in candidates:
                                    val = getattr(stream, key, None)
                                    if val is not None and str(val).strip(): return val
                                    if hasattr(stream, "_data") and hasattr(stream._data, "attrib"):
                                        val = stream._data.attrib.get(key)
                                        if val is not None and str(val).strip(): return val
    return ""

# ---------------------------------
# Step 1: Map playlists → file path
# ---------------------------------
track_to_playlists = defaultdict(set)

if DO_PLAYLISTS:
    print("Scanning playlists ...", flush=True)
    music_playlists = [pl for pl in plex.playlists() if getattr(pl, "playlistType", "") == "audio"]
    print(f"Found {len(music_playlists)} audio playlists.", flush=True)

    for idx, playlist in enumerate(music_playlists, start=1):
        try:
            for track in playlist.items():
                try:
                    media = track.media[0] if getattr(track, "media", None) else None
                    part = media.parts[0] if (media and getattr(media, "parts", None)) else None
                    file_path = getattr(part, "file", "") if part else ""
                    if file_path:
                        track_to_playlists[file_path].add(playlist.title)
                except Exception:
                    pass
        finally:
            if idx % 5 == 0 or idx == len(music_playlists):
                print(f"  mapped {idx}/{len(music_playlists)} playlists ...", flush=True)
    print("✅ Playlist mapping complete.", flush=True)
else:
    print("⏭️  Skipping Playlist mapping (disabled by user).", flush=True)

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
    "Track_ID", "Artist_ID", "Album_ID", "Media_ID",
    "Track_Genres", "Album_Genres", "Artist_Genres",
]

# Standard Columns (Permanent)
header += [
    "Date", "Date_Cleaned", "Bitrate", "Duration",
    "Track #", "Disc #", "File Type", "File Size (MB)",
    "Date Created", "Date Modified",
    "Record_Label", "Gain", "Loudness", "Similar_Artists", 
    "Artist_Collections", "Album_Collections", "Track_Collections", "Mood",
    "Playlists", "User_Rating", "Play_Count", "Track_Popularity", "Last_Played", "Labels", "Lyrics"
]

# Accumulators
album_acc = {}
def _album_bucket(album_artist: str, album_name: str):
    key = (album_artist or "", album_name or "")
    if key not in album_acc:
        album_acc[key] = {
            "album_ids": set(), "years": set(), "track_count": 0,
            "artist_collections": set(), "album_collections": set(), "track_collections": set(),
            "playlists": set(), "file_types": set(), "bitrate_vals": [],
            "file_size_bytes_sum": 0, "date_created_dates": set(),
        }
    return album_acc[key]

artist_acc = {}
def _artist_bucket(artist_name: str):
    k = artist_name or ""
    if k not in artist_acc:
        artist_acc[k] = {
            "albums": set(), "years": set(), "track_count": 0,
            "artist_collections": set(), "bitrate_vals": [], "file_size_bytes_sum": 0,
        }
    return artist_acc[k]

total_written = 0
total_processed = 0 

print(f"Writing track export to: {OUTPUT_CSV}", flush=True)
with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow(header)

    for a_idx, artist in enumerate(artists, start=1):
        if EXPORT_LIMIT > 0 and total_processed >= EXPORT_LIMIT: break

        try:
            artist_name = getattr(artist, "title", "") or ""
            artist_genres = _safe_join(getattr(artist, "genres", None))
            artist_collections = _safe_join(getattr(artist, "collections", None))

            ab = _artist_bucket(artist_name)
            for item in _split_csvish(artist_collections):
                ab["artist_collections"].add(item)

            albums = artist.albums()
            for album in albums:
                if EXPORT_LIMIT > 0 and total_processed >= EXPORT_LIMIT: break

                try:
                    album_name_obj = getattr(album, "title", "") or ""
                    album_genres = _safe_join(getattr(album, "genres", None))
                    album_collections = _safe_join(getattr(album, "collections", None))
                    album_id = getattr(album, "ratingKey", "") or ""
                    
                    # FETCH RECORD LABEL (from Album.studio)
                    record_label = getattr(album, "studio", "") or ""

                    ab["albums"].add(album_name_obj)

                    tracks = album.tracks()
                    for track in tracks:
                        # --- HARD LIMIT CHECK ---
                        if EXPORT_LIMIT > 0 and total_processed >= EXPORT_LIMIT:
                            break
                        
                        total_processed += 1 

                        try:
                            # 1. Fetch Basic Metadata
                            media = track.media[0] if getattr(track, "media", None) else None
                            part = media.parts[0] if (media and getattr(media, "parts", None)) else None
                            file_path = getattr(part, "file", "") if part else ""
                            title = getattr(track, "title", "") or ""
                            
                            album_artist = getattr(track, "grandparentTitle", "") or ""
                            track_artist = getattr(track, "originalTitle", "") or ""
                            album_name  = getattr(track, "parentTitle", "") or album_name_obj or ""
                            track_id = getattr(track, "ratingKey", "") or ""
                            media_id = getattr(media, "id", "") if media else ""
                            artist_id = getattr(track, "grandparentRatingKey", "") or ""
                            album_id_track = getattr(track, "parentRatingKey", "") or album_id or ""
                            track_genres = _track_genres_from_xml(track)
                            
                            # FETCH GAIN / LOUDNESS
                            # Only reload if we really need deep stream data (optional but safer for gain/loudness)
                            # track.reload() 
                            try:
                                track.reload()
                            except Exception:
                                pass 
                                
                            gain = _deep_search_attr(track, ["gain", "replayGain", "albumGain"])
                            loudness = _deep_search_attr(track, ["loudness"])

                            # FETCH SIMILAR ARTISTS (Standard Metadata only)
                            similar_str = ""
                            try:
                                sims = getattr(artist, "similar", [])
                                sim_artists = [s.tag if hasattr(s, "tag") else s.title for s in sims]
                                similar_str = ", ".join(sim_artists)
                            except Exception:
                                pass

                            # 3. Standard Data
                            album_date = getattr(album, "originallyAvailableAt", None)
                            date_cleaned = getattr(album_date, "year", "") if album_date else ""
                            date_str = _safe_date_str(album_date)
                            duration_ms = getattr(track, "duration", 0) or 0
                            duration_seconds = int(duration_ms / 1000) if duration_ms else 0
                            minutes = duration_seconds // 60
                            seconds = duration_seconds % 60
                            duration_cleaned = f"{minutes}:{str(seconds).zfill(2)}" if duration_seconds else ""
                            file_size_bytes = getattr(part, "size", 0) or 0
                            file_size_mb = round(file_size_bytes / (1024 * 1024), 1) if file_size_bytes else 0
                            file_type = getattr(part, "container", "") if part else ""
                            bitrate = getattr(media, "bitrate", "") if media else ""
                            date_created = getattr(track, "addedAt", "")
                            date_modified = getattr(track, "updatedAt", "")
                            moods             = _safe_join(getattr(track, "moods", None))
                            labels            = _safe_join(getattr(track, "labels", None))
                            track_collections = _safe_join(getattr(track, "collections", None))
                            playlists = ", ".join(sorted(track_to_playlists[file_path])) if file_path in track_to_playlists else ""
                            user_rating  = getattr(track, "userRating", "") or ""
                            play_count   = getattr(track, "viewCount", 0) or 0
                            rating_count = getattr(track, "ratingCount", 0) or 0
                            last_played  = getattr(track, "lastViewedAt", "") or ""
                            track_num = getattr(track, "index", "") or ""
                            disc_num  = getattr(track, "parentIndex", "") or ""

                            row = [
                                file_path, title, track_artist, album_artist, album_name,
                                track_id, artist_id, album_id_track, media_id, 
                                track_genres, album_genres, artist_genres
                            ]
                            
                            row += [
                                date_str, date_cleaned, bitrate, duration_cleaned,
                                track_num, disc_num, file_type, file_size_mb,
                                date_created, date_modified,
                                record_label, gain, loudness, similar_str,
                                artist_collections, album_collections, track_collections, moods,
                                playlists, user_rating, play_count, int(rating_count), last_played, labels, getattr(track, "lyrics", "") or ""
                            ]

                            writer.writerow(row)
                            total_written += 1

                            # Update accumulators
                            b = _album_bucket(album_artist, album_name)
                            b["track_count"] += 1
                            if album_id_track: b["album_ids"].add(str(album_id_track))
                            if date_cleaned: b["years"].add(str(date_cleaned))
                            for item in _split_csvish(artist_collections): b["artist_collections"].add(item)
                            for item in _split_csvish(album_collections): b["album_collections"].add(item)
                            for item in _split_csvish(track_collections): b["track_collections"].add(item)
                            for item in _split_csvish(playlists): b["playlists"].add(item)
                            if file_type: b["file_types"].add(str(file_type).strip())
                            br = _try_float(bitrate)
                            if br is not None: b["bitrate_vals"].append(br)
                            if file_size_bytes: b["file_size_bytes_sum"] += int(file_size_bytes)
                            dc = _date_only(date_created)
                            if dc: b["date_created_dates"].add(dc)

                            ab = _artist_bucket(artist_name)
                            ab["track_count"] += 1
                            if date_cleaned:
                                try: ab["years"].add(int(date_cleaned))
                                except: pass
                            br2 = _try_float(bitrate)
                            if br2 is not None: ab["bitrate_vals"].append(br2)
                            if file_size_bytes: ab["file_size_bytes_sum"] += int(file_size_bytes)

                        except Exception as e:
                            print(f"⚠️ Skipped a track due to error: {e}", flush=True)
                            continue

                except Exception as e:
                    print(f"⚠️ Skipped an album due to error: {e}", flush=True)
                    continue

            if a_idx % 10 == 0 or a_idx == len(artists):
                print(f"  processed {a_idx}/{len(artists)} artists ... (tracks written: {total_written}, checked: {total_processed})", flush=True)

        except Exception as e:
            print(f"⚠️ Skipped an artist due to error: {e}", flush=True)
            continue

print(f"✅ Export complete: {total_written} tracks written to '{OUTPUT_CSV}'.", flush=True)

if compat_output_csv and os.path.abspath(OUTPUT_CSV) != os.path.abspath(compat_output_csv):
    try:
        shutil.copyfile(OUTPUT_CSV, compat_output_csv)
    except Exception as e:
        print(f"⚠️ Could not write compatibility copy: {e}", flush=True)

# ---------------------------------
# Step 3: Write Album & Artist Summaries
# ---------------------------------
out_dir = os.path.dirname(os.path.abspath(OUTPUT_CSV)) or os.getcwd()

# Album Summary
ALBUM_INFO_CSV = os.path.join(out_dir, f"{_date_prefix} Artist_Album_Info.csv")
album_header = ["Album_Artist", "Album", "Album_ID", "Year", "Track_Count", "Artist_Collections", "Album_Collections", "Track_Collections", "Playlists", "File_Type", "Bitrate_Avg", "Album_File_MB_Size", "Date_Created"]
keys_sorted = sorted(album_acc.keys(), key=lambda k: (k[0].lower(), k[1].lower()))

with open(ALBUM_INFO_CSV, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(album_header)
    for (album_artist, album_name) in keys_sorted:
        b = album_acc[(album_artist, album_name)]
        try:
            years_sorted = sorted({int(y) for y in b["years"] if str(y).strip()})
            year_str = ", ".join(str(y) for y in years_sorted) if years_sorted else ""
        except:
            year_str = _sorted_unique_join(b["years"])
        bitrate_avg = _avg(b["bitrate_vals"])
        album_mb = round(b["file_size_bytes_sum"] / (1024 * 1024), 1) if b["file_size_bytes_sum"] else 0
        w.writerow([
            album_artist, album_name, _sorted_unique_join(b["album_ids"]), year_str, b["track_count"],
            _sorted_unique_join(b["artist_collections"]), _sorted_unique_join(b["album_collections"]),
            _sorted_unique_join(b["track_collections"]), _sorted_unique_join(b["playlists"]),
            _sorted_unique_join(b["file_types"]), bitrate_avg, album_mb, _sorted_unique_join(b["date_created_dates"]),
        ])
print(f"✅ Album summary complete: wrote '{ALBUM_INFO_CSV}'.", flush=True)

# Artist Summary
ARTIST_INFO_CSV = os.path.join(out_dir, f"{_date_prefix} Artist_Level_Info.csv")
artist_header = ["Artist", "Albums", "Years", "Album_Count", "Track_Count", "Artist_Collections", "Bitrate_Avg", "Bitrate_Min", "Bitrate_Max", "File_Size_Total_MB"]
artist_keys_sorted = sorted(artist_acc.keys(), key=lambda x: x.lower())

with open(ARTIST_INFO_CSV, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(artist_header)
    for artist_name in artist_keys_sorted:
        a = artist_acc[artist_name]
        albums = sorted({str(x).strip() for x in a["albums"] if str(x).strip()}, key=lambda x: x.lower())
        years_int = sorted({y for y in a["years"] if isinstance(y, int)})
        years_str = (f"{years_int[0]}-{years_int[-1]}" if years_int[0] != years_int[-1] else str(years_int[0])) if years_int else ""
        bitrates = [v for v in a["bitrate_vals"] if isinstance(v, (int, float))]
        bitrate_avg = _avg(bitrates)
        bitrate_min = min(bitrates) if bitrates else ""
        bitrate_max = max(bitrates) if bitrates else ""

        size_mb = round(a["file_size_bytes_sum"] / (1024 * 1024), 1) if a["file_size_bytes_sum"] else 0
        w.writerow([
            artist_name, ", ".join(albums), years_str, len(albums), int(a["track_count"]),
            _sorted_unique_join(a["artist_collections"]), _avg(bitrates),
            min(bitrates) if bitrates else "", max(bitrates) if bitrates else "", size_mb,
        ])
print(f"✅ Artist summary complete: wrote '{ARTIST_INFO_CSV}'.", flush=True)