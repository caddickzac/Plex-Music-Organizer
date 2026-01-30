#!/usr/bin/env python3
"""
Relabel track artist (per-track) from a CSV.

Reads from STDIN a JSON payload (sent by the Streamlit app):
  {
    "csv_path": "<path to csv>",
    "dry_run": true|false,
    "action": "relabel: track artist"   # optional
  }

Credentials via environment (set by the Streamlit app):
  PLEX_BASEURL (or PLEX_URL)
  PLEX_TOKEN   (or PLEX_API_TOKEN)

CSV must supply:
  - a track id column (any ONE of):
      track_id | track_rating_key | rating_key
  - a new artist column (any ONE of):
      new_track_artist | track_artist | new_artist | artist | track_artist_new
"""

import os, sys, json
import pandas as pd
from plexapi.server import PlexServer

# ---------- helpers ----------
def env(key, *alts, default=None):
    for k in (key, *alts):
        v = os.environ.get(k)
        if v:
            return v
    return default

def read_payload_stdin():
    try:
        txt = sys.stdin.read()
        return json.loads(txt or "{}")
    except Exception as e:
        print(f"Failed to parse STDIN JSON payload: {e}", file=sys.stderr)
        return {}

def normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = (
        df.columns
        .str.strip()
        .str.lower()
        .str.replace(r"[^a-z0-9]+", "_", regex=True)  # e.g. "Track ID" -> "track_id"
        .str.strip("_")
    )
    return df

def first_present(df: pd.DataFrame, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None

def coerce_int(val, default=None):
    try:
        return int(float(str(val).strip()))
    except Exception:
        return default

# ---------- main ----------
def main():
    # Credentials
    base = env("PLEX_BASEURL", "PLEX_URL")
    token = env("PLEX_TOKEN", "PLEX_API_TOKEN")
    if not base or not token:
        print("ERROR: Missing PLEX_BASEURL/PLEX_TOKEN (or PLEX_URL/PLEX_API_TOKEN).", file=sys.stderr)
        sys.exit(2)

    # Payload
    payload  = read_payload_stdin()
    csv_path = payload.get("csv_path")
    dry_run  = bool(payload.get("dry_run", False))
    if not csv_path or not os.path.isfile(csv_path):
        print(f"ERROR: csv_path missing or not found: {csv_path}", file=sys.stderr)
        sys.exit(3)

    print(f"Connecting to Plex @ {base} ...", flush=True)
    plex = PlexServer(base, token)

    # Load CSV and detect columns
    df = pd.read_csv(csv_path)
    df = normalize_cols(df)

    id_col = first_present(df, ["track_id", "track_rating_key", "rating_key"])
    artist_col = first_present(df, ["new_track_artist", "track_artist", "new_artist", "artist", "track_artist_new"])

    if not id_col or not artist_col:
        print(
            "ERROR: Could not find required columns.\n"
            f"  Present columns: {list(df.columns)}\n"
            "  Need a track id column from: track_id | track_rating_key | rating_key\n"
            "  And a new artist column from: new_track_artist | track_artist | new_artist | artist | track_artist_new",
            file=sys.stderr,
        )
        sys.exit(4)

    # Filter to rows with non-empty new artist
    df = df[df[artist_col].notna() & (df[artist_col].astype(str).str.strip() != "")]
    print(f"🎯 {len(df)} rows with new track-artist values to process.", flush=True)

    edited, skipped = 0, 0

    for _, row in df.iterrows():
        tid = coerce_int(row.get(id_col))
        new_artist = str(row.get(artist_col, "")).strip()

        if tid is None or not new_artist:
            skipped += 1
            continue

        try:
            track = plex.fetchItem(f"/library/metadata/{tid}")
            title = getattr(track, "title", "")
            old_artist = getattr(track, "originalTitle", "") or ""

            if old_artist == new_artist:
                print(f"Skip: Track_ID={tid} '{title}' already has artist '{new_artist}'.", flush=True)
                continue

            if dry_run:
                print(f"[DRY-RUN] Track_ID={tid} '{title}': artist '{old_artist}' -> '{new_artist}'", flush=True)
            else:
                # Edit the 'originalTitle' field (track-level artist credit)
                track.edit(**{"originalTitle.value": new_artist, "originalTitle.locked": 1})
                track.reload()
                print(f"✅ Track_ID={tid} '{title}': artist '{old_artist}' -> '{new_artist}'", flush=True)
                edited += 1

        except Exception as e:
            print(f"❌ Error updating Track_ID {tid}: {e}", flush=True)
            skipped += 1

    print(f"Done. Edited={edited} Skipped={skipped}", flush=True)

if __name__ == "__main__":
    main()
