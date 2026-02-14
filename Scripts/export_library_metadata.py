#!/usr/bin/env python3
"""
Export Plex music library metadata to CSV - Strict Original Logic with Hierarchical Moves & Fixes.
"""

import os
import sys
import csv
import shutil
import statistics
import re # Added for robust gain/loudness parsing
from collections import defaultdict
from datetime import datetime
from plexapi.server import PlexServer

# --- Console encoding safety ---
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
    if os.path.exists("/app/Exports"):
        out_dir = "/app/Exports"
    else:
        out_dir = os.getcwd()
    
    OUTPUT_CSV = os.path.join(out_dir, f"{_date_prefix} Track_Level_Info.csv")
    compat_output_csv = None

if os.path.exists(out_dir):
    try: os.chmod(out_dir, 0o777)
    except: pass

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
    try: return ", ".join(t.tag for t in (tags or []))
    except Exception: return ""

def _safe_date_str(d):
    try: return str(d) if d else ""
    except Exception: return ""

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
        except Exception: return ""

def _split_csvish(s: str):
    if not s: return []
    return [x.strip() for x in str(s).split(",") if x.strip()]

def _sorted_unique_join(items):
    cleaned = sorted({str(x).strip() for x in items if str(x).strip()}, key=lambda x: x.lower())
    return ", ".join(cleaned) if cleaned else ""

def _try_float(x):
    # FIX: Robust parsing for "7.2 dB" or "14 LUFS"
    try:
        if x is None: return None
        if isinstance(x, (int, float)): return float(x)
        s = str(x).strip()
        if not s: return None
        # Extract number from string (e.g. "-7.2 dB" -> -7.2)
        match = re.search(r"[-+]?\d*\.\d+|\d+", s)
        return float(match.group()) if match else None
    except Exception: return None

def _avg(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return (sum(vals) / len(vals)) if vals else ""

def _deep_search_attr(track, candidates):
    for key in candidates:
        val = getattr(track, key, None)
        if val is not None and str(val).strip(): return val
        if hasattr(track, "_data") and hasattr(track._data, "attrib"):
            val = track._data.attrib.get(key)
            if val is not None and str(val).strip(): return val
    if hasattr(track, "media") and track.media:
        for media in track.media:
            for key in candidates:
                val = getattr(media, key, None)
                if val is not None and str(val).strip(): return val
                if hasattr(media, "_data") and hasattr(media._data, "attrib"):
                    val = media._data.attrib.get(key)
                    if val is not None and str(val).strip(): return val
            if hasattr(media, "parts") and media.parts:
                for part in media.parts:
                    if hasattr(part, "streams") and part.streams:
                        for stream in part.streams:
                            if stream.streamType == 2: 
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
    for idx, playlist in enumerate(music_playlists, start=1):
        try:
            for track in playlist.items():
                try:
                    media = track.media[0] if getattr(track, "media", None) else None
                    part = media.parts[0] if (media and getattr(media, "parts", None)) else None
                    file_path = getattr(part, "file", "") if part else ""
                    if file_path:
                        track_to_playlists[file_path].add(playlist.title)
                except Exception: pass
        finally:
            if idx % 5 == 0 or idx == len(music_playlists):
                print(f"  mapped {idx}/{len(music_playlists)} playlists ...", flush=True)
else:
    print("⏭️  Skipping Playlist mapping (disabled by user).", flush=True)

# ---------------------------------
# Step 2: Extract full track metadata
# ---------------------------------
music_library = next((s for s in plex.library.sections() if getattr(s, "TYPE", "") == "artist"), None)
artists = music_library.search()

# 1. EDIT: Remove Gain, Loudness, Record_Label from Track Header
header = [
    "Filename", "Title", "Track_Artist", "Album_Artist", "Album",
    "Track_ID", "Artist_ID", "Album_ID", "Media_ID",
    "Track_Genres", "Album_Genres", "Artist_Genres",
]
header += [
    "Date", "Date_Cleaned", "Bitrate", "Duration",
    "Track #", "Disc #", "File Type", "File Size (MB)",
    "Date Created", "Date Modified",
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
            "gain_vals": [], "loudness_vals": [], "record_labels": set()
        }
    return album_acc[key]

artist_acc = {}
def _artist_bucket(artist_name: str):
    k = artist_name or ""
    if k not in artist_acc:
        artist_acc[k] = {
            "albums": set(), "years": set(), "track_count": 0,
            "artist_collections": set(), "bitrate_vals": [], "file_size_bytes_sum": 0,
            "similar_artists": "", "popularity_vals": [], "total_plays": 0 
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
            sims = getattr(artist, "similar", [])
            ab["similar_artists"] = ", ".join([s.tag if hasattr(s, "tag") else s.title for s in sims])
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
                    
                    label_temp = getattr(album, "studio", "") or ""

                    ab["albums"].add(album_name_obj)

                    tracks = album.tracks()
                    for track in tracks:
                        if EXPORT_LIMIT > 0 and total_processed >= EXPORT_LIMIT: break
                        
                        total_processed += 1 

                        try:
                            # Basic Metadata
                            media = track.media[0] if getattr(track, "media", None) else None
                            part = media.parts[0] if (media and getattr(media, "parts", None)) else None
                            file_path = getattr(part, "file", "") if part else ""
                            title = getattr(track, "title", "") or ""
                            
                            # FIX: Use track-level grouping key (Assessment Pt 5)
                            album_artist = getattr(track, "grandparentTitle", "") or ""
                            album_name_track = getattr(track, "parentTitle", "") or album_name_obj or ""
                            
                            # Update Album Bucket
                            b = _album_bucket(album_artist, album_name_track)
                            if label_temp: b["record_labels"].add(label_temp)

                            # 1. EDIT: Capture Gain/Loudness (Robust)
                            # FIX: Reload track to ensure deep streams are available (Assessment Pt 4)
                            try:
                                track.reload()
                            except Exception:
                                pass
                            
                            gain = _deep_search_attr(track, ["gain", "replayGain", "albumGain"])
                            loudness = _deep_search_attr(track, ["loudness"])
                            
                            g_val = _try_float(gain)
                            l_val = _try_float(loudness)
                            if g_val is not None: b["gain_vals"].append(g_val)
                            if l_val is not None: b["loudness_vals"].append(l_val)

                            # 3. EDIT: Capture Artist Stats
                            p_count = getattr(track, "viewCount", 0) or 0
                            rating_count = getattr(track, "ratingCount", 0) or 0
                            ab["total_plays"] += p_count
                            ab["popularity_vals"].append(int(rating_count))
                            
                            # Collections
                            track_collections = _safe_join(getattr(track, "collections", None))
                            for item in _split_csvish(artist_collections): b["artist_collections"].add(item)
                            for item in _split_csvish(album_collections): b["album_collections"].add(item)
                            for item in _split_csvish(track_collections): b["track_collections"].add(item)

                            # FIX: Restore Duration Logic (m:ss)
                            duration_ms = getattr(track, "duration", 0) or 0
                            duration_seconds = int(duration_ms / 1000) if duration_ms else 0
                            minutes = duration_seconds // 60
                            seconds = duration_seconds % 60
                            duration_cleaned = f"{minutes}:{str(seconds).zfill(2)}" if duration_seconds else ""

                            # FIX: Restore Playlist Lookup Logic
                            plist_str = ", ".join(sorted(track_to_playlists[file_path])) if file_path in track_to_playlists else ""
                            
                            # FIX: Use correct Album ID source (Assessment Pt 2)
                            album_id_track = getattr(track, "parentRatingKey", "") or album_id or ""

                            row = [
                                file_path, title, getattr(track, "originalTitle", ""), album_artist, album_name_track,
                                getattr(track, "ratingKey", ""), artist.ratingKey, album_id_track, getattr(media, "id", ""), 
                                _track_genres_from_xml(track), album_genres, artist_genres
                            ]
                            
                            row += [
                                _safe_date_str(getattr(album, "originallyAvailableAt", None)), 
                                getattr(getattr(album, "originallyAvailableAt", None), "year", ""), 
                                getattr(media, "bitrate", ""), duration_cleaned,
                                getattr(track, "index", ""), getattr(track, "parentIndex", ""), 
                                getattr(part, "container", ""), round(getattr(part, "size", 0) / (1024 * 1024), 1),
                                getattr(track, "addedAt", ""), getattr(track, "updatedAt", ""),
                                artist_collections, album_collections, track_collections, 
                                _safe_join(getattr(track, "moods", None)),
                                plist_str,
                                getattr(track, "userRating", ""), p_count, int(rating_count), 
                                getattr(track, "lastViewedAt", ""), _safe_join(getattr(track, "labels", None)), 
                                getattr(track, "lyrics", "") or ""
                            ]

                            writer.writerow(row)
                            total_written += 1

                            # Update Standard Accumulators
                            b["track_count"] += 1
                            # FIX: Accumulate album_id_track, not album_id object (Assessment Pt 2)
                            if album_id_track: b["album_ids"].add(str(album_id_track))
                            
                            if getattr(getattr(album, "originallyAvailableAt", None), "year", ""): 
                                b["years"].add(str(album.originallyAvailableAt.year))
                            for item in _split_csvish(plist_str): b["playlists"].add(item)
                            if getattr(part, "container", ""): b["file_types"].add(str(part.container).strip())
                            
                            # FIX: Guard against None in bitrate (Assessment Pt 3)
                            br_val = _try_float(getattr(media, "bitrate", ""))
                            if br_val is not None: b["bitrate_vals"].append(br_val)
                            
                            b["file_size_bytes_sum"] += getattr(part, "size", 0)
                            if getattr(track, "addedAt", ""): b["date_created_dates"].add(_date_only(track.addedAt))

                            ab["track_count"] += 1
                            if getattr(getattr(album, "originallyAvailableAt", None), "year", ""):
                                try: ab["years"].add(int(album.originallyAvailableAt.year))
                                except: pass
                            br2 = _try_float(getattr(media, "bitrate", ""))
                            if br2 is not None: ab["bitrate_vals"].append(br2)
                            ab["file_size_bytes_sum"] += getattr(part, "size", 0)

                        except Exception as e:
                            # FIX: Restore error logging (Assessment Pt 5)
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
try: os.chmod(OUTPUT_CSV, 0o777) 
except: pass

if compat_output_csv and os.path.abspath(OUTPUT_CSV) != os.path.abspath(compat_output_csv):
    try: shutil.copyfile(OUTPUT_CSV, compat_output_csv)
    except Exception as e: print(f"⚠️ Could not write compatibility copy: {e}", flush=True)

# ---------------------------------
# Step 3: Write Album & Artist Summaries
# ---------------------------------
out_dir = os.path.dirname(os.path.abspath(OUTPUT_CSV)) or os.getcwd()

# Album Summary
ALBUM_INFO_CSV = os.path.join(out_dir, f"{_date_prefix} Artist_Album_Info.csv")
album_header = ["Album_Artist", "Album", "Album_ID", "Year", "Track_Count", "Artist_Collections", "Album_Collections", "Track_Collections", "Playlists", "File_Type", "Bitrate_Avg", "Album_File_MB_Size", "Date_Created", "Record_Label", "Avg_Gain", "Avg_Loudness"]
keys_sorted = sorted(album_acc.keys(), key=lambda k: (k[0].lower(), k[1].lower()))

with open(ALBUM_INFO_CSV, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(album_header)
    for (album_artist, album_name) in keys_sorted:
        b = album_acc[(album_artist, album_name)]
        bitrate_avg = _avg(b["bitrate_vals"])
        album_mb = round(b["file_size_bytes_sum"] / (1024 * 1024), 1) if b["file_size_bytes_sum"] else 0
        
        # FIX: Restore numeric year sorting (Assessment Pt 1)
        try:
            years_sorted = sorted({int(y) for y in b["years"] if str(y).strip()})
            year_str = ", ".join(str(y) for y in years_sorted) if years_sorted else ""
        except:
            year_str = _sorted_unique_join(b["years"])

        w.writerow([
            album_artist, album_name, _sorted_unique_join(b["album_ids"]), year_str, b["track_count"],
            _sorted_unique_join(b["artist_collections"]), _sorted_unique_join(b["album_collections"]),
            _sorted_unique_join(b["track_collections"]), _sorted_unique_join(b["playlists"]),
            _sorted_unique_join(b["file_types"]), bitrate_avg, album_mb, _sorted_unique_join(b["date_created_dates"]),
            _sorted_unique_join(b["record_labels"]), _avg(b["gain_vals"]), _avg(b["loudness_vals"])
        ])
try: os.chmod(ALBUM_INFO_CSV, 0o777)
except: pass

# Artist Summary
ARTIST_INFO_CSV = os.path.join(out_dir, f"{_date_prefix} Artist_Level_Info.csv")
artist_header = ["Artist", "Similar_Artists", "Total_Plays", "Median_Track_Popularity", "Albums", "Years", "Album_Count", "Track_Count", "Artist_Collections", "Bitrate_Avg", "Bitrate_Min", "Bitrate_Max", "File_Size_Total_MB"]
artist_keys_sorted = sorted(artist_acc.keys(), key=lambda x: x.lower())

with open(ARTIST_INFO_CSV, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(artist_header)
    for artist_name in artist_keys_sorted:
        a = artist_acc[artist_name]
        med_pop = statistics.median(a["popularity_vals"]) if a["popularity_vals"] else 0
        bitrates = [v for v in a["bitrate_vals"] if isinstance(v, (int, float))]
        years_int = sorted({y for y in a["years"] if isinstance(y, int)})
        size_mb = round(a["file_size_bytes_sum"] / (1024 * 1024), 1) if a["file_size_bytes_sum"] else 0
        w.writerow([
            artist_name, a["similar_artists"], a["total_plays"], med_pop, ", ".join(sorted(a["albums"])),
            (f"{years_int[0]}-{years_int[-1]}" if years_int[0] != years_int[-1] else str(years_int[0])) if years_int else "",
            len(a["albums"]), int(a["track_count"]), _sorted_unique_join(a["artist_collections"]),
            _avg(bitrates), min(bitrates) if bitrates else "", max(bitrates) if bitrates else "", size_mb,
        ])
try: os.chmod(ARTIST_INFO_CSV, 0o777)
except: pass