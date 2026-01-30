#!/usr/bin/env python3
"""
Add album-level collections from CSV.

Env:
  PLEX_BASEURL, PLEX_TOKEN

Stdin JSON:
  { "csv_path": "/full/path/to/file.csv", "action": "add: album collections" }

CSV requirements:
  - Column with album identifier (any of):
      album_id | album_rating_key | album_ratingkey | Album_ID
    Fallback: Track_ID (we'll resolve album from track.parentRatingKey)
  - Column with collection names:
      Add_to_album_collection   # comma-separated; commas only

Behavior:
  - For each album, add missing collection tags.
  - Creates collections implicitly by tagging albums.
  - Prints summary and exits 0 on success.
"""

import os
import sys
import json
from typing import Dict, List, Optional, Tuple, Set

import pandas as pd
from plexapi.server import PlexServer  # type: ignore


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


def collect_targets(df: pd.DataFrame) -> Tuple[Dict[int, Set[str]], str]:
    """
    Return {album_id: {collection1, collection2, ...}}, plus the column name used for ID resolution.
    Accept album_id-like columns or fallback to Track_ID -> album_id.
    """
    id_col = find_column(
        list(df.columns),
        [
            "album_id",
            "album_rating_key",
            "album_ratingkey",
            "album_rating_key",
            "albumid",
            "albumratingkey",
            "album_ratingkey",
        ],
    )

    # Will we need to resolve album via track?
    track_id_col = find_column(list(df.columns), ["track_id", "track_rating_key", "rating_key", "ratingkey"])

    coll_col = find_column(list(df.columns), ["add_to_album_collection"])
    if not coll_col:
        present = ", ".join(df.columns)
        sys.stderr.write(
            "ERROR: Missing required column 'Add_to_album_collection'. "
            f"Present columns: [{present}]\n"
        )
        sys.exit(4)

    # Build raw mapping first; if we must resolve via Track_ID, we store track_ids here
    desired: Dict[int, Set[str]] = {}
    unresolved_track_to_colls: Dict[int, Set[str]] = {}

    # Helper to split by commas (only commas), strip, and ignore blanks
    def split_collections(val) -> List[str]:
        if pd.isna(val):
            return []
        text = str(val)
        parts = [p.strip() for p in text.split(",")]
        return [p for p in parts if p]

    edited_rows = 0

    if id_col:
        for _, row in df.iterrows():
            collections = split_collections(row.get(coll_col))
            if not collections:
                continue
            try:
                album_id = int(row.get(id_col))
            except Exception:
                continue
            if album_id <= 0:
                continue
            desired.setdefault(album_id, set()).update(collections)
            edited_rows += 1
        id_used = id_col
    else:
        # Fall back to Track_ID -> Album
        if not track_id_col:
            present = ", ".join(df.columns)
            sys.stderr.write(
                "ERROR: Could not find required ID column. Need one of "
                "[album_id | album_rating_key | album_ratingkey] or a track id from "
                "[Track_ID | track_rating_key | rating_key]. "
                f"Present columns: [{present}]\n"
            )
            sys.exit(4)
        for _, row in df.iterrows():
            collections = split_collections(row.get(coll_col))
            if not collections:
                continue
            try:
                track_id = int(row.get(track_id_col))
            except Exception:
                continue
            if track_id <= 0:
                continue
            unresolved_track_to_colls.setdefault(track_id, set()).update(collections)
            edited_rows += 1
        id_used = track_id_col

    print(f"Rows with candidate album collections: {edited_rows}", flush=True)
    return (desired, id_used) if id_col else (unresolved_track_to_colls, id_used)


def resolve_album_ids_from_tracks(plex: PlexServer, track_to_colls: Dict[int, Set[str]]) -> Dict[int, Set[str]]:
    """
    Given {track_id: {colls}}, return {album_id: {colls}} by following parentRatingKey.
    """
    result: Dict[int, Set[str]] = {}
    if not track_to_colls:
        return result

    # Light preflight
    sample_ids = list(track_to_colls.keys())[:50]
    ok = 0
    for tid in sample_ids:
        try:
            tr = plex.fetchItem(int(tid))
            album_id = getattr(tr, "parentRatingKey", None)
            if album_id is not None:
                ok += 1
        except Exception:
            pass
    print(f"Preflight: resolved {ok}/{len(sample_ids)} Track_IDs in a sample of {len(sample_ids)}.", flush=True)

    for tid, colls in track_to_colls.items():
        try:
            tr = plex.fetchItem(int(tid))
            aid = getattr(tr, "parentRatingKey", None)
            if aid is None:
                continue
            aid = int(aid)
            result.setdefault(aid, set()).update(colls)
        except Exception:
            continue
    return result


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

    targets, id_used = collect_targets(df)

    # If we gathered by Track_ID, resolve to album ids now
    if id_used and norm(id_used) in {"track_id", "track_rating_key", "rating_key", "ratingkey"}:
        targets = resolve_album_ids_from_tracks(plex, targets)

    unique_album_ids = list(targets.keys())
    print(f"Unique target albums: {len(unique_album_ids)}", flush=True)

    edited_albums = 0
    skipped_albums = 0
    pairs_added = 0

    # Process per-album
    for i, album_id in enumerate(unique_album_ids, start=1):
        desired_colls = sorted({c for c in targets.get(album_id, set()) if c})
        if not desired_colls:
            skipped_albums += 1
            continue

        try:
            album = plex.fetchItem(int(album_id))
        except Exception as e:
            print(f"Skip album_id={album_id}: fetch failed: {e}", flush=True)
            skipped_albums += 1
            continue

        # Existing collection tags on this album
        try:
            existing = set()
            # album.collections can be None or list of MediaTag objects with .tag
            coll_attr = getattr(album, "collections", None)
            if coll_attr:
                existing = {getattr(t, "tag", "").strip() for t in coll_attr if getattr(t, "tag", "").strip()}
        except Exception:
            existing = set()

        to_add = [c for c in desired_colls if c not in existing]
        if not to_add:
            skipped_albums += 1
        else:
            try:
                album.addCollection(to_add)  # accepts list; creates collection if missing
                edited_albums += 1
                pairs_added += len(to_add)
            except Exception as e:
                print(f"Album {album_id}: failed to add {to_add}: {e}", flush=True)
                skipped_albums += 1

        if i % 100 == 0 or i == len(unique_album_ids):
            print(f"Progress: {i}/{len(unique_album_ids)} albums processed.", flush=True)

    print(
        f"Summary: albums_edited={edited_albums}, albums_skipped={skipped_albums}, tags_added={pairs_added}",
        flush=True,
    )
    print(f"Done. Edited={edited_albums} Skipped={skipped_albums}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
