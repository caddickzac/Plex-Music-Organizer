#!/usr/bin/env python3
import os, sys, json, re
import pandas as pd
from plexapi.server import PlexServer

# ---------- helpers ----------
def env(key, *alts, default=None):
    for k in (key, *alts):
        v = os.environ.get(k)
        if v: return v
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
        if c in df.columns: return c
    return None

def parse_genre_cell(cell: str):
    if cell is None: return []
    s = str(cell).strip()
    if not s: return []
    parts = re.split(r"[;,|]", s)
    genres = [p.strip() for p in parts if p.strip()]
    seen, out = set(), []
    for g in genres:
        if g.lower() not in seen:
            seen.add(g.lower())
            out.append(g)
    return out

# ---------- main ----------
def main():
    base = env("PLEX_BASEURL", "PLEX_URL")
    token = env("PLEX_TOKEN", "PLEX_API_TOKEN")
    if not base or not token:
        print("ERROR: Missing PLEX_BASEURL/PLEX_TOKEN.", file=sys.stderr)
        sys.exit(2)

    payload = read_payload_stdin()
    csv_path = payload.get("csv_path")
    dry_run = bool(payload.get("dry_run", False))

    if not csv_path or not os.path.isfile(csv_path):
        print(f"ERROR: csv_path not found: {csv_path}", file=sys.stderr)
        sys.exit(3)

    print(f"Connecting to Plex @ {base} ...", flush=True)
    plex = PlexServer(base, token)

    df = normalize_cols(pd.read_csv(csv_path))
    id_col = first_present(df, ["track_id", "rating_key", "track_rating_key"])
    genre_col = first_present(df, ["track_genres", "genres", "new_genres", "album_genres"])

    if not id_col or not genre_col:
        print(f"ERROR: Columns missing. Need an ID and a Genre column.", file=sys.stderr)
        sys.exit(4)

    df = df[df[genre_col].notna() & (df[genre_col].astype(str).str.strip() != "")]
    print(f"🎯 Processing {len(df)} tracks...", flush=True)

    edited, skipped = 0, 0
    for _, row in df.iterrows():
        try:
            tid = int(row[id_col])
            want_genres = parse_genre_cell(row[genre_col])
            track = plex.fetchItem(tid)
            
            # For Tracks, we use the .edit() method to LOCK the metadata
            if dry_run:
                print(f"[DRY-RUN] Track: {track.title} -> {want_genres}")
            else:
                # Plex API syntax for locking track genres
                edits = {"genre.locked": 1}
                for i, g in enumerate(want_genres):
                    edits[f"genre[{i}].tag.tag"] = g
                
                # Applying the edit
                track.edit(**edits)
                print(f"✅ Updated & Locked: {track.title}")
                edited += 1

        except Exception as e:
            print(f"❌ Error Track_ID={row.get(id_col)}: {e}")
            skipped += 1

    print(f"Done. Edited={edited} Skipped={skipped}", flush=True)

if __name__ == "__main__":
    main()