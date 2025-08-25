#!/usr/bin/env python3
"""
Add tracks to (or create) Plex playlists from a CSV.

Reads credentials from env:
  PLEX_BASEURL (preferred) or PLEX_URL
  PLEX_TOKEN   (preferred) or PLEX_API_TOKEN
Optional:
  PLEX_TIMEOUT (seconds, default 120)

Input (JSON on stdin from your Streamlit app):
  { "csv_path": "<path to csv>" }

CSV must have:
  - Track_ID (or rating_key / track_rating_key / media_ids)
  - Add_to_playlist  (comma-separated playlist names only, e.g.
      "Music | Popular: Jazz, Music | Rainy Day")
"""

from __future__ import annotations
import os, sys, json
from collections import defaultdict
import pandas as pd
from plexapi.server import PlexServer, CONFIG

def log(msg: str) -> None:
    print(msg, flush=True)

def warn(msg: str) -> None:
    print(f"WARNING: {msg}", flush=True)

def err(msg: str) -> None:
    print(f"ERROR: {msg}", flush=True)

ID_CANDIDATES = [
    "track_id", "track_rating_key", "rating_key", "media_ids", "media_id"
]
PL_CANDIDATES = [
    "add_to_playlist", "add_to_playlists", "playlists_to_add",
    "playlist", "playlists"
]

def find_col(cols_lower, candidates):
    # cols_lower is a list of lower-cased column names
    s = set(cols_lower)
    for want in candidates:
        if want in s:
            return want
    return None

def fetch_track(plex: PlexServer, track_id: int):
    return plex.fetchItem(f"/library/metadata/{int(track_id)}")

def main():
    # ----- Parse stdin payload -----
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    csv_path = payload.get("csv_path")
    if not csv_path or not os.path.isfile(csv_path):
        err(f"CSV not found: {csv_path!r}")
        sys.exit(2)

    # ----- Plex auth -----
    base = os.environ.get("PLEX_BASEURL") or os.environ.get("PLEX_URL")
    token = os.environ.get("PLEX_TOKEN") or os.environ.get("PLEX_API_TOKEN")
    if not base or not token:
        err("Missing PLEX_BASEURL/PLEX_TOKEN (or PLEX_URL/PLEX_API_TOKEN).")
        sys.exit(2)

    timeout = int(os.environ.get("PLEX_TIMEOUT", "120"))
    CONFIG.timeout = timeout

    log(f"Connecting to Plex @ {base} ...")
    plex = PlexServer(base, token)

    # ----- Read CSV -----
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        err(f"Failed to read CSV: {e}")
        sys.exit(2)

    # Resolve column names case-insensitively
    cols_lower = [c.lower() for c in df.columns]
    id_key_l = find_col(cols_lower, ID_CANDIDATES)
    pl_key_l = find_col(cols_lower, PL_CANDIDATES)

    if not id_key_l or not pl_key_l:
        err(
            "Could not find required columns.\n"
            f"Present columns: {list(df.columns)}\n"
            f"Need an ID column from: {' | '.join(ID_CANDIDATES)}\n"
            f"And a playlist column from: {' | '.join(PL_CANDIDATES)}"
        )
        sys.exit(4)

    id_col = df.columns[cols_lower.index(id_key_l)]
    pl_col = df.columns[cols_lower.index(pl_key_l)]

    # Keep only rows that have both ID and playlist text
    df = df[df[id_col].notna() & df[pl_col].notna()]
    df = df[df[pl_col].astype(str).str.strip() != ""]
    if df.empty:
        warn("No rows with both Track_ID and Add_to_playlist present.")
        log("Done. Edited=0 Skipped=0")
        return

    # ----- Preflight: resolve a small sample of IDs -----
    sample = df.head(min(50, len(df)))
    ok = 0
    for _, r in sample.iterrows():
        try:
            _ = fetch_track(plex, int(r[id_col]))
            ok += 1
        except Exception:
            pass
    log(f"Preflight: resolved {ok}/{len(sample)} Track_IDs in a sample of {len(sample)}.")

    # ----- Build playlist -> track objects -----
    # IMPORTANT: split ONLY on commas (","); do NOT split on "|" or ";"
    def split_playlists(s: str):
        return [p.strip() for p in str(s).split(",") if p.strip()]

    track_cache = {}
    items_by_playlist: dict[str, list] = defaultdict(list)

    rows_with_playlists = 0
    for _, row in df.iterrows():
        parts = split_playlists(row[pl_col])
        if not parts:
            continue
        rows_with_playlists += 1

        try:
            tid = int(row[id_col])
        except Exception:
            continue

        if tid not in track_cache:
            try:
                track_cache[tid] = fetch_track(plex, tid)
            except Exception as e:
                warn(f"Failed to fetch Track_ID {tid}: {e}")
                track_cache[tid] = None

        trk = track_cache.get(tid)
        if trk is None:
            continue

        for name in parts:
            items_by_playlist[name].append(trk)

    log(f"Rows with candidate playlists: {rows_with_playlists}")
    if not items_by_playlist:
        warn("No playlist work to do (nothing parsed from Add_to_playlist).")
        log("Done. Edited=0 Skipped=0")
        return

    # Deduplicate per playlist by ratingKey
    for name, items in list(items_by_playlist.items()):
        uniq = {}
        for t in items:
            rk = getattr(t, "ratingKey", None)
            if rk is not None:
                uniq[rk] = t
        items_by_playlist[name] = list(uniq.values())

    # Existing audio playlists
    existing_audio = {
        pl.title: pl for pl in plex.playlists()
        if getattr(pl, "playlistType", "") == "audio"
    }

    created = 0
    updated = 0
    edited = 0
    skipped = 0

    def chunks(seq, size):
        for i in range(0, len(seq), size):
            yield seq[i:i+size]

    log(f"Summary: playlists_total={len(items_by_playlist)}")
    for pname, items in items_by_playlist.items():
        if not items:
            warn(f"Playlist '{pname}': no valid items parsed; skipping.")
            continue

        try:
            if pname in existing_audio:
                pl = existing_audio[pname]
                added_here = 0
                for batch in chunks(items, 200):
                    try:
                        pl.addItems(batch)
                        added_here += len(batch)
                    except Exception as e:
                        warn(f"Could not add a batch to existing '{pname}': {e}")
                        skipped += len(batch)
                if added_here:
                    updated += 1
                    edited += added_here
                    log(f"Updated playlist '{pname}' with {added_here} tracks.")
            else:
                first = items[:1]
                rest  = items[1:]
                if not first:
                    warn(f"Playlist '{pname}': empty after dedup; skipping create.")
                    continue
                try:
                    pl = plex.createPlaylist(pname, items=first)
                except Exception as e:
                    raise RuntimeError(f"Could not create playlist '{pname}': {e}")
                created += 1
                edited += len(first)
                added_here = 0
                for batch in chunks(rest, 200):
                    try:
                        pl.addItems(batch)
                        added_here += len(batch)
                    except Exception as e:
                        warn(f"Could not add a batch to new '{pname}': {e}")
                        skipped += len(batch)
                if added_here:
                    edited += added_here
                log(f"Created playlist '{pname}' with {1 + added_here} tracks.")
        except Exception as e:
            err(str(e))
            skipped += len(items)

    log(f"Done. Edited={edited} Skipped={skipped}")
    sys.exit(0)

if __name__ == "__main__":
    main()
