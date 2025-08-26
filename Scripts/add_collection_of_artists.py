#!/usr/bin/env python3
"""
Add artist-level collections from CSV.

Env:
  PLEX_BASEURL, PLEX_TOKEN   (or PLEX_URL, PLEX_API_TOKEN)

Stdin JSON:
  { "csv_path": "/full/path/to/file.csv", "action": "add: artist collections" }

CSV requirements (any one ID path + collections column):
  - Artist ID column (preferred):  artist_id | artist_rating_key | artist_ratingkey | Artist_ID
    OR Album ID column:            album_id | album_rating_key | album_ratingkey | Album_ID
    OR Track ID column:            track_id | track_rating_key | rating_key | ratingkey | Track_ID
  - Collections column (required): Add_to_artist_collection   # comma-separated (commas only)

Behavior:
  - For each artist, add any missing collection tags listed in Add_to_artist_collection.
  - If CSV only has Track_ID, artist is resolved via track.grandparentRatingKey.
  - If CSV only has album_id, artist is resolved via album.parentRatingKey.
  - De-dupes per-artist and per-tag; safe to re-run.
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
    Returns ({artist_id: {collection,...}}, id_source_col_name)
    Collect artist_id directly when available; otherwise stage album_ids or track_ids for resolution.
    """
    artist_id_col = find_column(
        list(df.columns),
        [
            "artist_id",
            "artist_rating_key",
            "artist_ratingkey",
            "artist_rating_key",
            "artistid",
            "artistid_",
            "artist_id_",
            "artist",
            "artist_key",
            "artistkey",
            "artist_ratingkey",
            "artist_rating_key",
        ],
    )

    album_id_col = find_column(list(df.columns), ["album_id", "album_rating_key", "album_ratingkey", "album_rating_key", "albumid", "albumid_"])
    track_id_col = find_column(list(df.columns), ["track_id", "track_rating_key", "track_ratingkey", "rating_key", "ratingkey", "track_id_"])
    coll_col = find_column(list(df.columns), ["add_to_artist_collection"])

    if not coll_col:
        present = ", ".join(df.columns)
        sys.stderr.write(
            "ERROR: Missing required column 'Add_to_artist_collection'. "
            f"Present columns: [{present}]\n"
        )
        sys.exit(4)

    staged: Dict[int, Set[str]] = {}
    id_used = None

    if artist_id_col:
        id_used = artist_id_col
        for _, row in df.iterrows():
            collections = split_collections(row.get(coll_col))
            if not collections:
                continue
            try:
                aid = int(row.get(artist_id_col))
            except Exception:
                continue
            if aid <= 0:
                continue
            staged.setdefault(aid, set()).update(collections)

    elif album_id_col:
        id_used = album_id_col
        for _, row in df.iterrows():
            collections = split_collections(row.get(coll_col))
            if not collections:
                continue
            try:
                alid = int(row.get(album_id_col))
            except Exception:
                continue
            if alid <= 0:
                continue
            staged.setdefault(alid, set()).update(collections)

    elif track_id_col:
        id_used = track_id_col
        for _, row in df.iterrows():
            collections = split_collections(row.get(coll_col))
            if not collections:
                continue
            try:
                tid = int(row.get(track_id_col))
            except Exception:
                continue
            if tid <= 0:
                continue
            staged.setdefault(tid, set()).update(collections)
    else:
        present = ", ".join(df.columns)
        sys.stderr.write(
            "ERROR: Could not find an ID column. Need one of:\n"
            "  artist id: [artist_id | artist_rating_key | artist_ratingkey]\n"
            "  OR album id: [album_id | album_rating_key | album_ratingkey]\n"
            "  OR track id: [track_id | track_rating_key | rating_key]\n"
            f"Present columns: [{present}]\n"
        )
        sys.exit(4)

    print(f"Rows with candidate artist collections: {sum(bool(v) for v in df[coll_col].astype(str).str.strip() != '')}", flush=True)
    return staged, id_used


# ---------------------------
# ID resolution helpers
# ---------------------------
def resolve_artist_ids_from_tracks(plex: PlexServer, track_to_colls: Dict[int, Set[str]]) -> Dict[int, Set[str]]:
    """Track_ID -> artist_id via Track.grandparentRatingKey."""
    out: Dict[int, Set[str]] = {}
    if not track_to_colls:
        return out

    sample = list(track_to_colls.keys())[:50]
    ok = 0
    for tid in sample:
        try:
            t = plex.fetchItem(int(tid))
            if getattr(t, "grandparentRatingKey", None) is not None:
                ok += 1
        except Exception:
            pass
    print(f"Preflight: resolved {ok}/{len(sample)} Track_IDs in a sample of {len(sample)}.", flush=True)

    for tid, colls in track_to_colls.items():
        try:
            t = plex.fetchItem(int(tid))
            aid = getattr(t, "grandparentRatingKey", None)
            if aid is None:
                continue
            aid = int(aid)
            out.setdefault(aid, set()).update(colls)
        except Exception:
            continue
    return out


def resolve_artist_ids_from_albums(plex: PlexServer, album_to_colls: Dict[int, Set[str]]) -> Dict[int, Set[str]]:
    """Album_ID -> artist_id via Album.parentRatingKey."""
    out: Dict[int, Set[str]] = {}
    if not album_to_colls:
        return out

    sample = list(album_to_colls.keys())[:50]
    ok = 0
    for aid in sample:
        try:
            al = plex.fetchItem(int(aid))
            if getattr(al, "parentRatingKey", None) is not None:
                ok += 1
        except Exception:
            pass
    print(f"Preflight: resolved {ok}/{len(sample)} Album_IDs in a sample of {len(sample)}.", flush=True)

    for alid, colls in album_to_colls.items():
        try:
            al = plex.fetchItem(int(alid))
            aid = getattr(al, "parentRatingKey", None)  # album's parent is the artist
            if aid is None:
                continue
            aid = int(aid)
            out.setdefault(aid, set()).update(colls)
        except Exception:
            continue
    return out


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
    id_used_norm = norm(id_used)

    # Resolve to artist IDs when needed
    if id_used_norm in {"album_id", "album_rating_key", "album_ratingkey"}:
        staged = resolve_artist_ids_from_albums(plex, staged)
    elif id_used_norm in {"track_id", "track_rating_key", "track_ratingkey", "rating_key", "ratingkey"}:
        staged = resolve_artist_ids_from_tracks(plex, staged)

    artist_ids = list(staged.keys())
    print(f"Unique target artists: {len(artist_ids)}", flush=True)

    edited_artists = 0
    skipped_artists = 0
    tags_added = 0

    for i, artist_id in enumerate(artist_ids, start=1):
        desired = sorted({c for c in staged.get(artist_id, set()) if c})
        if not desired:
            skipped_artists += 1
            continue

        try:
            artist = plex.fetchItem(int(artist_id))
        except Exception as e:
            print(f"Skip artist_id={artist_id}: fetch failed: {e}", flush=True)
            skipped_artists += 1
            continue

        # Existing collection tags on the artist
        existing = set()
        try:
            coll_attr = getattr(artist, "collections", None)
            if coll_attr:
                existing = {getattr(t, "tag", "").strip() for t in coll_attr if getattr(t, "tag", "").strip()}
        except Exception:
            pass

        to_add = [c for c in desired if c not in existing]
        if not to_add:
            skipped_artists += 1
        else:
            try:
                artist.addCollection(to_add)  # creates collection if missing
                edited_artists += 1
                tags_added += len(to_add)
            except Exception as e:
                print(f"Artist {artist_id}: failed to add {to_add}: {e}", flush=True)
                skipped_artists += 1

        if i % 100 == 0 or i == len(artist_ids):
            print(f"Progress: {i}/{len(artist_ids)} artists processed.", flush=True)

    print(
        f"Summary: artists_edited={edited_artists}, artists_skipped={skipped_artists}, tags_added={tags_added}",
        flush=True,
    )
    print(f"Done. Edited={edited_artists} Skipped={skipped_artists}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
