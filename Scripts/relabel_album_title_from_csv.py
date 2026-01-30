#!/usr/bin/env python3
"""
Relabel album titles from a CSV.

Reads STDIN JSON payload (from the Streamlit app):
  {
    "csv_path": "<path to csv>",
    "action": "relabel: album title"   # label only; not required
  }

Env (set by the app):
  PLEX_BASEURL (or PLEX_URL)
  PLEX_TOKEN   (or PLEX_API_TOKEN)

CSV can identify the item by either:
  - Album id:  album_rating_key | album_id | album_ratingkey
  - Track id:  track_id  (we'll resolve its parent album)

New title column can be:
  new_album_title | new_title | album_new | album_title_new | new_album | album
"""

import os, sys, json
import pandas as pd
from plexapi.server import PlexServer

# --- console encoding safety (Windows) ---
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

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
    except Exception:
        return {}

def normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = (
        df.columns
        .str.strip()
        .str.lower()
        .str.replace(r"[^a-z0-9]+", "_", regex=True)  # "Track ID" -> "track_id", "Album" -> "album"
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

    df = pd.read_csv(csv_path)
    df = normalize_cols(df)

    # Detect id and new-title columns
    id_col = first_present(df, ["album_rating_key", "album_id", "album_ratingkey", "track_id"])
    title_col = first_present(df, ["new_album_title", "new_title", "album_new", "album_title_new", "new_album", "album"])

    if not id_col or not title_col:
        print(
            "ERROR: Could not find required columns.\n"
            f"  Present columns: {list(df.columns)}\n"
            "  Need an ID column from: album_rating_key | album_id | album_ratingkey | track_id\n"
            "  And a title column from: new_album_title | new_title | album_new | album_title_new | new_album | album",
            file=sys.stderr,
        )
        sys.exit(4)

    # Keep only rows with a non-empty new title
    df = df[df[title_col].astype(str).str.strip() != ""]
    print(f"{len(df)} rows with new album titles to process.")

    edited, skipped = 0, 0

    for _, row in df.iterrows():
        id_val = coerce_int(row.get(id_col))
        new_title = str(row.get(title_col, "")).strip()
        if id_val is None or not new_title:
            skipped += 1
            continue

        try:
            # Resolve album
            if id_col in ("album_rating_key", "album_id", "album_ratingkey"):
                album = plex.fetchItem(f"/library/metadata/{id_val}")
            else:
                # track_id -> parent album
                track = plex.fetchItem(f"/library/metadata/{id_val}")
                parent_key = getattr(track, "parentRatingKey", None)
                if parent_key is None:
                    raise RuntimeError("No parent album found for the track.")
                album = plex.fetchItem(f"/library/metadata/{parent_key}")

            old_title = getattr(album, "title", "")

            if old_title == new_title:
                # No change required
                continue

            # Prefer the generic edit pattern (works across PlexAPI versions)
            try:
                album.edit(**{"title.value": new_title, "title.locked": 1})
            except Exception:
                # Fallback helper if present
                if hasattr(album, "editTitle"):
                    album.editTitle(new_title)
                else:
                    raise
            album.reload()
            edited += 1

        except Exception as e:
            print(f"Error updating item id {id_val}: {e}", flush=True)
            skipped += 1

    print(f"Done. Edited={edited} Skipped={skipped}")

if __name__ == "__main__":
    main()
