#!/usr/bin/env python3
"""
Relabel album genres from a CSV.

Reads from STDIN a JSON payload:
  {
    "csv_path": "<path to csv>",
    "dry_run": true|false,
    "action": "relabel: albums genre"   # optional
  }

Credentials via environment (set by the Streamlit app):
  PLEX_BASEURL (or PLEX_URL)
  PLEX_TOKEN   (or PLEX_API_TOKEN)

CSV must supply:
  - an identifier column (album or track):
      album_rating_key | album_id | album_ratingkey | track_id
  - a genre column:
      album_genres | genres | new_genres | album_genre_new
    (Values may be a single genre or comma/semicolon/pipe-separated list.)
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
        .str.replace(r"[^a-z0-9]+", "_", regex=True)
        .str.strip("_")
    )
    return df

def first_present(df: pd.DataFrame, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None

def parse_genre_cell(cell: str):
    """Return a list of genres. Accept comma/semicolon/pipe as separators.
       If the value has none of those, treat it as a single genre string."""
    if cell is None:
        return []
    s = str(cell).strip()
    if not s:
        return []
    # split on , ; or | — but keep exact labels otherwise
    import re
    parts = re.split(r"[;,|]", s)
    genres = [p.strip() for p in parts if p.strip()]
    # de-dup preserving order
    seen = set()
    out = []
    for g in genres:
        key = g.lower()
        if key not in seen:
            seen.add(key)
            out.append(g)
    return out

# ---------- main ----------
def main():
    # Read credentials
    base = env("PLEX_BASEURL", "PLEX_URL")
    token = env("PLEX_TOKEN", "PLEX_API_TOKEN")
    if not base or not token:
        print("ERROR: Missing PLEX_BASEURL/PLEX_TOKEN (or PLEX_URL/PLEX_API_TOKEN).", file=sys.stderr)
        sys.exit(2)

    # Read payload (csv_path, dry_run)
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

    id_col = first_present(df, ["album_rating_key", "album_id", "album_ratingkey", "track_id"])
    genre_col = first_present(df, ["album_genres", "genres", "new_genres", "album_genre_new"])

    if not id_col or not genre_col:
        print(f"ERROR: Could not find required columns.\n"
              f"  Present columns: {list(df.columns)}\n"
              f"  Need ID column: album_rating_key | album_id | album_ratingkey | track_id\n"
              f"  Need genre column: album_genres | genres | new_genres | album_genre_new",
              file=sys.stderr)
        sys.exit(4)

    # Filter rows with non-empty genre strings
    df = df[df[genre_col].notna() & (df[genre_col].astype(str).str.strip() != "")]
    print(f"🎯 {len(df)} rows with non-empty genres to process...", flush=True)

    # If we only have track_id, resolve to album ids first
    resolved = 0
    if id_col == "track_id":
        new_rows = []
        for _, row in df.iterrows():
            try:
                tid = int(row[id_col])
                track = plex.fetchItem(f"/library/metadata/{tid}")
                aid = int(getattr(track, "parentRatingKey"))
                new_rows.append({"album_rating_key": aid, genre_col: row[genre_col]})
                resolved += 1
            except Exception as e:
                print(f"Skip: could not resolve album for Track_ID={row.get(id_col)}: {e}", flush=True)
        df = pd.DataFrame(new_rows)
        id_col = "album_rating_key"
        print(f"Resolved {resolved} album ids from track ids.", flush=True)

    # Build desired genres per album (last wins if multiple rows per album)
    desired = {}
    for _, row in df.iterrows():
        try:
            aid = int(row[id_col])
            desired[aid] = parse_genre_cell(row[genre_col])
        except Exception:
            continue

    print(f"Prepared desired genres for {len(desired)} albums.", flush=True)

    edited, skipped = 0, 0
    for aid, want_genres in desired.items():
        try:
            album = plex.fetchItem(f"/library/metadata/{aid}")
            have = [g.tag for g in getattr(album, "genres", []) or []]

            # Compare case-insensitively
            set_have = {g.lower() for g in have}
            set_want = {g.lower() for g in want_genres}
            if set_have == set_want:
                print(f"Skip: Album_ID={aid} genres unchanged: {have}", flush=True)
                continue

            if dry_run:
                print(f"[DRY-RUN] Album_ID={aid} '{getattr(album, 'title','')}'\n"
                      f"  Before: {have}\n"
                      f"  After : {want_genres}", flush=True)
            else:
                # Clear existing
                for g in have:
                    album.removeGenre(g)
                album.reload()

                # Add new (exactly as provided)
                if want_genres:
                    album.addGenre(want_genres)
                album.reload()

                # Verify
                after = [g.tag for g in getattr(album, "genres", []) or []]
                print(f"✅ Album_ID={aid} updated.\n  Before: {have}\n  After : {after}", flush=True)
                edited += 1
        except Exception as e:
            print(f"❌ Error updating Album_ID={aid}: {e}", flush=True)
            skipped += 1

    print(f"Done. Edited={edited} Skipped={skipped} ResolvedFromTracks={resolved}", flush=True)

if __name__ == "__main__":
    main()
