#!/usr/bin/env python3
"""
Add artist genres from a CSV (append-only).

Reads from STDIN a JSON payload (sent by the Streamlit app):
  {
    "csv_path": "<path to csv>",
    "dry_run": true|false,
    "action": "relabel: artist genres"   # optional
  }

Credentials via environment (set by the Streamlit app):
  PLEX_BASEURL (or PLEX_URL)
  PLEX_TOKEN   (or PLEX_API_TOKEN)

CSV must supply:
  - an artist id column (any ONE of):
      artist_id | artist_rating_key | grandparent_rating_key | rating_key
  - a genres column (any ONE of):
      artist_genres | genres | new_genres | artist_genre_new
    (Values can be a single genre or comma/semicolon/pipe separated list.)

Behavior:
  - Aggregates all genre entries per artist across rows.
  - Adds only genres that the artist doesn't already have (case-insensitive).
  - Does NOT remove existing genres (append-only), matching your snippet.
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
        .str.replace(r"[^a-z0-9]+", "_", regex=True)  # e.g. "Artist ID" -> "artist_id"
        .str.strip("_")
    )
    return df

def first_present(df: pd.DataFrame, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None

def split_genres(cell: str):
    """Return a list of genres. Accept comma/semicolon/pipe as separators.
       If none present, treat as a single genre; trims spaces and de-dups (case-insensitive)."""
    if cell is None:
        return []
    s = str(cell).strip()
    if not s:
        return []
    parts = re.split(r"[;,|]", s)
    out, seen = [], set()
    for p in parts:
        g = p.strip()
        if not g:
            continue
        key = g.lower()
        if key not in seen:
            seen.add(key)
            out.append(g)
    return out

def coerce_int(val):
    try:
        return int(float(str(val).strip()))
    except Exception:
        return None

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

    id_col    = first_present(df, ["artist_id", "artist_rating_key", "grandparent_rating_key", "rating_key"])
    genres_col = first_present(df, ["artist_genres", "genres", "new_genres", "artist_genre_new"])

    if not id_col or not genres_col:
        print(
            "ERROR: Could not find required columns.\n"
            f"  Present columns: {list(df.columns)}\n"
            "  Need an artist id column from: artist_id | artist_rating_key | grandparent_rating_key | rating_key\n"
            "  And a genres column from: artist_genres | genres | new_genres | artist_genre_new",
            file=sys.stderr,
        )
        sys.exit(4)

    # Keep rows with non-empty genres
    df = df[df[genres_col].notna() & (df[genres_col].astype(str).str.strip() != "")]
    print(f"🌟 {len(df)} artist rows loaded with non-empty genre cells...", flush=True)

    # Aggregate genres per artist
    desired = {}  # artist_id -> set of genres (original case preserved via a map)
    for _, row in df.iterrows():
        aid = coerce_int(row.get(id_col))
        if aid is None:
            continue
        for g in split_genres(row.get(genres_col)):
            desired.setdefault(aid, {})
            # preserve original capitalization for output while deduping by lowercase
            desired[aid].setdefault(g.lower(), g)

    print(f"🎯 {len(desired)} unique artists to update...", flush=True)

    edited, skipped = 0, 0
    for aid, gmap in desired.items():
        want_list = list(gmap.values())
        try:
            artist = plex.fetchItem(f"/library/metadata/{aid}")
            have = [g.tag for g in getattr(artist, "genres", []) or []]

            # compute missing (case-insensitive)
            have_lc = {h.lower() for h in have}
            to_add = [g for g in want_list if g.lower() not in have_lc]

            if not to_add:
                print(f"Skip: Artist_ID={aid} '{getattr(artist,'title','')}' already has all genres {want_list}.", flush=True)
                continue

            if dry_run:
                print(f"[DRY-RUN] Artist_ID={aid} '{getattr(artist,'title','')}'\n"
                      f"  Before: {have}\n"
                      f"  Add   : {to_add}", flush=True)
            else:
                artist.addGenre(to_add)
                artist.reload()
                after = [g.tag for g in getattr(artist, "genres", []) or []]
                print(f"✅ Artist_ID={aid} '{getattr(artist,'title','')}' updated.\n"
                      f"  Before: {have}\n"
                      f"  After : {after}", flush=True)
                edited += 1
        except Exception as e:
            print(f"❌ Error updating Artist_ID {aid}: {e}", flush=True)
            skipped += 1

    print(f"Done. Edited={edited} Skipped={skipped}", flush=True)

if __name__ == "__main__":
    main()
