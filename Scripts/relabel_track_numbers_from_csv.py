#!/usr/bin/env python3
"""
Relabel track numbers from a CSV using Track_ID and Track #.

STDIN JSON payload (from the Streamlit app):
  {
    "csv_path": "<path to csv>",
    "action": "relabel: track numbers"
  }

Env:
  PLEX_BASEURL (or PLEX_URL)
  PLEX_TOKEN   (or PLEX_API_TOKEN)

CSV headers expected (from your export):
  Track_ID, Track #   (others are ignored)

Notes:
- Column names are normalized (e.g., "Track #" -> "track").
- Prints "Done. Edited=N Skipped=M" so the app can show a friendly success message.
"""

import os, sys, json
import pandas as pd
from plexapi.server import PlexServer

# Optional: ensure UTF-8 output if run outside the app
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

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
    except Exception:
        return {}

def normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = (
        df.columns
        .str.strip()
        .str.lower()
        .str.replace(r"[^a-z0-9]+", "_", regex=True)  # "Track #" -> "track_"
        .str.strip("_")                               # -> "track"
    )
    return df

def coerce_int(val, default=None):
    try:
        return int(float(str(val).strip()))
    except Exception:
        return default

def main():
    base = env("PLEX_BASEURL", "PLEX_URL")
    token = env("PLEX_TOKEN", "PLEX_API_TOKEN")
    if not base or not token:
        print("ERROR: Missing PLEX_BASEURL/PLEX_TOKEN (or PLEX_URL/PLEX_API_TOKEN).", file=sys.stderr)
        sys.exit(2)

    payload  = read_payload_stdin()
    csv_path = payload.get("csv_path")
    if not csv_path or not os.path.isfile(csv_path):
        print(f"ERROR: csv_path missing or not found: {csv_path}", file=sys.stderr)
        sys.exit(3)

    plex = PlexServer(base, token)

    # Load and normalize
    df = pd.read_csv(csv_path)
    df = normalize_cols(df)

    # Expect Track_ID -> "track_id" and Track # -> "track"
    if "track_id" not in df.columns or "track" not in df.columns:
        # Accept a few fallbacks just in case
        id_col = "track_id" if "track_id" in df.columns else next((c for c in ["track_rating_key","rating_key"] if c in df.columns), None)
        num_col = "track"    if "track"    in df.columns else next((c for c in ["track_number","track_index","index","track_no","tracknum","track_"] if c in df.columns), None)
        if not id_col or not num_col:
            print(
                f"ERROR: Could not find required columns.\n"
                f"  Present columns: {list(df.columns)}\n"
                f"  Need 'track_id' and 'track' (from 'Track_ID' and 'Track #').",
                file=sys.stderr,
            )
            sys.exit(4)
    else:
        id_col, num_col = "track_id", "track"

    # Keep only rows with a usable new track number
    df[num_col] = df[num_col].apply(coerce_int)
    df = df[df[num_col].notna()]
    print(f"{len(df)} rows with new track-number values to process.")

    edited, skipped = 0, 0

    for _, row in df.iterrows():
        tid = coerce_int(row.get(id_col))
        new_no = coerce_int(row.get(num_col))
        if tid is None or new_no is None:
            skipped += 1
            continue

        try:
            track = plex.fetchItem(f"/library/metadata/{tid}")
            old_no = getattr(track, "index", None)

            if old_no == new_no:
                # No change needed
                continue

            # Universal edit pattern for track number ('index')
            track.edit(**{"index.value": new_no, "index.locked": 1})
            track.reload()
            edited += 1

        except Exception as e:
            print(f"Error updating Track_ID {tid}: {e}", flush=True)
            skipped += 1

    print(f"Done. Edited={edited} Skipped={skipped}")

if __name__ == "__main__":
    main()
