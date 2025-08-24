#!/usr/bin/env python3
"""
Relabel disc numbers for tracks from a CSV.

Reads from STDIN a JSON payload:
  {
    "csv_path": "<path to csv>",
    "dry_run": true|false,
    "action": "relabel: disc numbers"   # optional
  }

Credentials via environment (set by the Streamlit app):
  PLEX_BASEURL (or PLEX_URL)
  PLEX_TOKEN   (or PLEX_API_TOKEN)

CSV must supply:
  - a track id column (any one of):
      track_id | track_rating_key | rating_key
  - a disc number column (any one of):
      disc | disc_number | disc_no | discnum | disc_index | parent_index | disc_# | disc_number_new | new_disc | disc_ (handles “Disc #”)
"""

import os, sys, json, re
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
        .str.replace(r"[^a-z0-9]+", "_", regex=True)  # "Disc #" -> "disc_"
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
    payload = read_payload_stdin()
    csv_path = payload.get("csv_path")
    dry_run = bool(payload.get("dry_run", False))
    if not csv_path or not os.path.isfile(csv_path):
        print(f"ERROR: csv_path missing or not found: {csv_path}", file=sys.stderr)
        sys.exit(3)

    print(f"Connecting to Plex @ {base} ...", flush=True)
    plex = PlexServer(base, token)

    # Load CSV
    df = pd.read_csv(csv_path)
    df = normalize_cols(df)

    # Column detection
    id_col   = first_present(df, ["track_id", "track_rating_key", "rating_key"])
    disc_col = first_present(df, [
        "disc", "disc_number", "disc_no", "discnum", "disc_index",
        "parent_index", "disc_", "disc_number_new", "new_disc"
    ])

    if not id_col or not disc_col:
        print(f"ERROR: Could not find required columns.\n"
              f"  Present columns: {list(df.columns)}\n"
              f"  Need a track id column from: track_id | track_rating_key | rating_key\n"
              f"  And a disc number column from: disc | disc_number | disc_no | discnum | disc_index | parent_index | disc_ | disc_number_new | new_disc",
              file=sys.stderr)
        sys.exit(4)

    print(f"Using columns: id={id_col}, disc={disc_col}.", flush=True)

    edited, skipped = 0, 0

    for _, row in df.iterrows():
        tid = coerce_int(row.get(id_col))
        new_disc = coerce_int(row.get(disc_col))
        if tid is None or new_disc is None:
            skipped += 1
            continue

        try:
            track = plex.fetchItem(f"/library/metadata/{tid}")
            title = getattr(track, "title", "")
            old_disc = getattr(track, "parentIndex", None)

            if old_disc == new_disc:
                print(f"Skip: Track_ID={tid} '{title}' disc already {new_disc}.", flush=True)
                continue

            if dry_run:
                print(f"[DRY-RUN] Track_ID={tid} '{title}': {old_disc} -> {new_disc}", flush=True)
            else:
                # Generic metadata edit pattern (works across PlexAPI versions)
                track.edit(**{"parentIndex.value": new_disc, "parentIndex.locked": 1})
                track.reload()
                print(f"✅ Track_ID={tid} '{title}': {old_disc} -> {new_disc}", flush=True)
                edited += 1
        except Exception as e:
            print(f"❌ Error updating Track_ID {tid}: {e}", flush=True)
            skipped += 1

    print(f"Done. Edited={edited} Skipped={skipped}", flush=True)

if __name__ == "__main__":
    main()
