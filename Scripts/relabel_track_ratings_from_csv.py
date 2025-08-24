#!/usr/bin/env python3
"""
Relabel user ratings for tracks from a CSV.

Reads from STDIN a JSON payload (sent by the Streamlit app):
  {
    "csv_path": "<path to csv>",
    "dry_run": true|false,
    "action": "relabel: track ratings"   # optional
  }

Credentials via environment (set by the Streamlit app):
  PLEX_BASEURL (or PLEX_URL)
  PLEX_TOKEN   (or PLEX_API_TOKEN)

CSV must supply:
  - a track id column (any ONE of):
      track_id | track_rating_key | rating_key
  - a rating column (any ONE of):
      user_rating | rating | new_rating
    (Values can be on a 0–10, 0–5, or 0–100 scale; we normalize to 0–10.)
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
        .str.replace(r"[^a-z0-9]+", "_", regex=True)  # e.g. "User Rating" -> "user_rating"
        .str.strip("_")
    )
    return df

def first_present(df: pd.DataFrame, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None

def parse_rating(val):
    """Return a float rating normalized to 0–10, or None if invalid."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None

    # Handle forms like "4/5" or "80/100"
    frac = re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*$", s)
    if frac:
        num = float(frac.group(1))
        den = float(frac.group(2))
        if den > 0:
            return max(0.0, min(10.0, (num / den) * 10.0))

    # Plain numeric
    try:
        v = float(s)
    except Exception:
        return None

    # Normalize:
    #   0–5  -> multiply by 2
    #   0–10 -> keep
    #   0–100-> divide by 10
    if 0.0 <= v <= 5.0:
        v = v * 2.0
    elif 5.0 < v <= 10.0:
        v = v
    elif 10.0 < v <= 100.0:
        v = v / 10.0
    # else: out of usual range; clamp
    v = max(0.0, min(10.0, v))
    # Round to one decimal (Plex supports floats)
    return round(v, 1)

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

    id_col     = first_present(df, ["track_id", "track_rating_key", "rating_key"])
    rating_col = first_present(df, ["user_rating", "rating", "new_rating"])

    if not id_col or not rating_col:
        print(
            "ERROR: Could not find required columns.\n"
            f"  Present columns: {list(df.columns)}\n"
            "  Need a track id column from: track_id | track_rating_key | rating_key\n"
            "  And a rating column from: user_rating | rating | new_rating",
            file=sys.stderr,
        )
        sys.exit(4)

    # Filter rows with usable ratings
    df[rating_col] = df[rating_col].apply(parse_rating)
    df = df[df[rating_col].notna()]
    print(f"🎯 {len(df)} rows with valid user ratings to process.", flush=True)

    edited, skipped = 0, 0

    for _, row in df.iterrows():
        tid = coerce_int(row.get(id_col))
        new_rating = row.get(rating_col)
        if tid is None or new_rating is None:
            skipped += 1
            continue

        try:
            track = plex.fetchItem(f"/library/metadata/{tid}")
            title = getattr(track, "title", "")
            old_rating = getattr(track, "userRating", None)

            # Normalize old to one decimal for comparison (when present)
            old_norm = round(float(old_rating), 1) if isinstance(old_rating, (int, float)) else old_rating
            if old_norm == new_rating:
                print(f"Skip: Track_ID={tid} '{title}' already rated {new_rating}.", flush=True)
                continue

            if dry_run:
                print(f"[DRY-RUN] Track_ID={tid} '{title}': rating {old_norm} -> {new_rating}", flush=True)
            else:
                # Try convenient helper if available, else use generic edit pattern
                if hasattr(track, "rate") and callable(getattr(track, "rate")):
                    track.rate(new_rating)
                else:
                    track.edit(**{"userRating.value": new_rating, "userRating.locked": 1})
                track.reload()
                print(f"✅ Track_ID={tid} '{title}': rating {old_norm} -> {new_rating}", flush=True)
                edited += 1

        except Exception as e:
            print(f"❌ Error updating Track_ID {tid}: {e}", flush=True)
            skipped += 1

    print(f"Done. Edited={edited} Skipped={skipped}", flush=True)

if __name__ == "__main__":
    main()
