#!/usr/bin/env python3
"""
Add track-level collections from CSV.

Env:
  PLEX_BASEURL, PLEX_TOKEN   (or PLEX_URL, PLEX_API_TOKEN)

Stdin JSON:
  { "csv_path": "/full/path/to/file.csv", "action": "add: track collections" }

CSV requirements:
  - Track ID column (one of): track_id | track_rating_key | track_ratingkey | rating_key | ratingkey | Track_ID
  - Collections column: Add_to_track_collection   # comma-separated (commas only)

Behavior:
  - For each Track_ID, add any missing collection tags listed in Add_to_track_collection.
  - De-dupes per-track and per-tag; safe to re-run.
  - Prints a clean summary and exits 0 on success.
"""

import os
import sys
import json
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
from plexapi.server import PlexServer  # type: ignore


# ---------------------------
# Helpers
# ---------------------------
def norm(s: str) -> str:
    return s.strip().lower().replace(" ", "_")


def find_column(columns: List[str], candidates: List[str]) -> Optional[str]:
    by_norm = {norm(c): c for c in columns}
    for cand in candidates:
        if cand in by_norm:
            return by_norm[cand]
    return None


def connect() -> PlexServer:
    base = os.environ.get("PLEX_BASEURL") or os.environ.get("PLEX_URL")
    token = os.environ.get("PLEX_TOKEN") or os.environ.get("PLEX_API_TOKEN")
    if not base or not token:
        sys.stderr.write("ERROR: Missing PLEX_BASEURL/PLEX_TOKEN (or PLEX_URL/PLEX_API_TOKEN).\n")
        sys.exit(2)
    print(f"Connecting to Plex @ {base} ...", flush=True)
    return PlexServer(base, token)


def parse_payload() -> Dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw or "{}")
    except Exception:
        return {}


def split_collections(val) -> List[str]:
    """Split on commas only, trim whitespace, drop blanks."""
    if pd.isna(val):
        return []
    parts = [p.strip() for p in str(val).split(",")]
    return [p for p in parts if p]


# ---------------------------
# Collect targets from CSV
# ---------------------------
def collect_targets(df: pd.DataFrame) -> Tuple[Dict[int, Set[str]], str]:
    """
    Returns ({track_id: {collection,...}}, id_col_name)
    """
    id_col = find_column(
        list(df.columns),
        [
            "track_id",
            "track_rating_key",
            "track_ratingkey",
            "rating_key",
            "ratingkey",
            "track_id_",
            "trackid",
            "Track_ID".lower(),  # case-insensitive via norm()
        ],
    )
    coll_col = find_column(list(df.columns), ["add_to_track_collection"])

    if not coll_col:
        present = ", ".join(df.columns)
        sys.stderr.write(
            "ERROR: Missing required column 'Add_to_track_collection'. "
            f"Present columns: [{present}]\n"
        )
        sys.exit(4)

    if not id_col:
        present = ", ".join(df.columns)
        sys.stderr.write(
            "ERROR: Could not find a Track ID column. Need one of: "
            "[track_id | track_rating_key | track_ratingkey | rating_key | ratingkey | Track_ID]. "
            f"Present columns: [{present}]\n"
        )
        sys.exit(4)

    staged: Dict[int, Set[str]] = {}
    for _, row in df.iterrows():
        collections = split_collections(row.get(coll_col))
        if not collections:
            continue
        try:
            tid = int(row.get(id_col))
        except Exception:
            continue
        if tid <= 0:
            continue
        staged.setdefault(tid, set()).update(collections)

    rows_with_vals = (df[coll_col].astype(str).str.strip() != "").sum()
    print(f"Rows with candidate track collections: {rows_with_vals}", flush=True)
    return staged, id_col


# ---------------------------
# Main
# ---------------------------
def main():
    payload = parse_payload()
    csv_path = payload.get("csv_path")
    if not csv_path or not os.path.isfile(csv_path):
        sys.stderr.write("ERROR: csv_path missing or not a file.\n")
        sys.exit(2)

    plex = connect()

    # Load CSV
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        sys.stderr.write(f"ERROR: Could not read CSV: {e}\n")
        sys.exit(2)

    staged, id_used = collect_targets(df)
    track_ids = list(staged.keys())
    if not track_ids:
        print("No tracks to update (no non-empty Add_to_track_collection rows).", flush=True)
        print("Done. Edited=0 Skipped=0", flush=True)
        sys.exit(0)

    # Preflight sample fetch to confirm IDs are valid ratingKeys
    sample = track_ids[:50]
    ok = 0
    for tid in sample:
        try:
            _ = plex.fetchItem(int(tid))
            ok += 1
        except Exception:
            pass
    print(f"Preflight: resolved {ok}/{len(sample)} Track_IDs in a sample of {len(sample)}.", flush=True)

    edited_tracks = 0
    skipped_tracks = 0
    tags_added = 0

    for i, track_id in enumerate(track_ids, start=1):
        desired = sorted({c for c in staged.get(track_id, set()) if c})
        if not desired:
            skipped_tracks += 1
            continue

        try:
            track = plex.fetchItem(int(track_id))
        except Exception as e:
            print(f"Skip Track_ID={track_id}: fetch failed: {e}", flush=True)
            skipped_tracks += 1
            continue

        # Existing collection tags on the track
        existing = set()
        try:
            coll_attr = getattr(track, "collections", None)
            if coll_attr:
                existing = {getattr(t, "tag", "").strip() for t in coll_attr if getattr(t, "tag", "").strip()}
        except Exception:
            pass

        to_add = [c for c in desired if c not in existing]
        if not to_add:
            skipped_tracks += 1
        else:
            try:
                track.addCollection(to_add)  # creates collection(s) if missing
                edited_tracks += 1
                tags_added += len(to_add)
            except Exception as e:
                print(f"Track {track_id}: failed to add {to_add}: {e}", flush=True)
                skipped_tracks += 1

        if i % 200 == 0 or i == len(track_ids):
            print(f"Progress: {i}/{len(track_ids)} tracks processed.", flush=True)

    print(
        f"Summary: tracks_edited={edited_tracks}, tracks_skipped={skipped_tracks}, tags_added={tags_added}",
        flush=True,
    )
    print(f"Done. Edited={edited_tracks} Skipped={skipped_tracks}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
