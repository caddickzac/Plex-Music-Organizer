#!/usr/bin/env python3
"""
Export Plex music library metadata to CSV.

Reads Plex credentials from environment:
  PLEX_BASEURL (preferred) or PLEX_URL
  PLEX_TOKEN (preferred) or PLEX_API_TOKEN

Writes CSV to:
  OUTPUT_CSV (if set) else "plex_music_exported_details.csv"
"""

import os
import sys
import csv
from collections import defaultdict
from plexapi.server import PlexServer

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
                file_path = track.media[0].parts[0].file  # full file path
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
    "Playlists", "User_Rating", "Play_Count", "Last_Played", "Labels", "Lyrics"
]

count = 0
with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(header)

    for a_idx, artist in enumerate(artists, start=1):
        try:
            artist_genres = ", ".join(g.tag for g in getattr(artist, "_
