#!/usr/bin/env python3
"""
relabel_album_date_from_csv.py

Update the ALBUM release date (Plex 'originallyAvailableAt') using the CSV "Date"
column. We index rows by Track_ID for convenience, but edits are applied to the
track's parent ALBUM.

INPUT via stdin JSON (like your other scripts):
  {
    "action": "relabel: album date",
    "csv_path": "/path/to/your.csv"
  }

ENV:
  PLEX_BASEURL (or PLEX_URL)
  PLEX_TOKEN   (or PLEX_API_TOKEN)

CSV columns (case-insensitive):
  - Track_ID | track_id | track_rating_key | rating_key | ratingKey | id
  - Date | Album_Date | originallyAvailableAt | Release_Date

Parses common date formats and writes YYYY-MM-DD to Plex. Locks the field.
Prints: "Done. Edited=N Skipped=M"
"""

import os
import sys
import json
import csv
from typing import Optional, Tuple, List, Dict, Set
from plexapi.server import PlexServer

# --- Console encoding safety (Windows) ---
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# --- Config from environment ---
PLEX_BASEURL = os.environ.get("PLEX_BASEURL") or os.environ.get("PLEX_URL")
PLEX_TOKEN   = os.environ.get("PLEX_TOKEN")   or os.environ.get("PLEX_API_TOKEN")

if not PLEX_BASEURL or not PLEX_TOKEN:
    sys.stderr.write("ERROR: Missing PLEX_BASEURL/PLEX_TOKEN (or PLEX_URL/PLEX_API_TOKEN).\n")
    sys.exit(2)

# --- Helpers ---
TRACK_ID_COLUMNS = ["track_id", "track_rating_key", "rating_key", "ratingkey", "id"]
DATE_COLUMNS     = ["date", "album_date", "originallyavailableat", "release_date"]

def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "_")

def _find_column(header: List[str], candidates: List[str]) -> Optional[str]:
    norm_header = {_norm(h): h for h in header}
    for c in candidates:
        if c in norm_header:
            return norm_header[c]
    return None

def _parse_input() -> Tuple[str, str]:
    data = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            data = json.loads(raw)
    except Exception:
        data = {}
    csv_path = data.get("csv_path") if isinstance(data, dict) else None
    if not csv_path and len(sys.argv) > 1:
        csv_path = sys.argv[1]
    if not csv_path:
        raise SystemExit("ERROR: No csv_path provided via stdin JSON or argv.")
    return csv_path, str(data.get("action", "relabel: album date"))

def _parse_date_value(s: str) -> Optional[str]:
    """
    Return a date string formatted as YYYY-MM-DD (Plex-friendly).
    Accepts YYYY-MM-DD, YYYY/MM/DD, MM/DD/YYYY, MM-DD-YYYY, and
    date+time variants (time is discarded).
    """
    if not s or not str(s).strip():
        return None
    t = str(s).strip().replace("/", "-")
    # Take only date portion if time is present
    date_part = t.split()[0]
    parts = date_part.split("-")
    try:
        if len(parts) == 3:
            a, b, c = parts
            if len(a) == 4:  # YYYY-MM-DD
                yyyy, mm, dd = a, b.zfill(2), c.zfill(2)
            else:            # MM-DD-YYYY
                mm, dd, yyyy = a.zfill(2), b.zfill(2), c
            if len(yyyy) == 4 and yyyy.isdigit() and 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31:
                return f"{yyyy}-{mm}-{dd}"
    except Exception:
        pass
    return None

def _format_for_plex(yyyy_mm_dd: str) -> str:
    # If you want to include time-of-day, change here.
    return yyyy_mm_dd

def main() -> None:
    csv_path, action = _parse_input()
    print(f"Connecting to Plex @ {PLEX_BASEURL} ...", flush=True)
    plex = PlexServer(PLEX_BASEURL, PLEX_TOKEN)

    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            if not header:
                print("ERROR: CSV has no header.", flush=True)
                sys.exit(4)

            track_col = _find_column(header, TRACK_ID_COLUMNS + ["track_id", "track_id".upper()])
            date_col  = _find_column(header, DATE_COLUMNS + ["date"])

            if not track_col or not date_col:
                present = list(header)
                print(
                    "ERROR: Could not find required columns.\n"
                    f"Present columns: {present}\n"
                    "Need a track id column from: Track_ID | track_id | track_rating_key | rating_key | ratingKey | id\n"
                    "And a date column from: Date | Album_Date | originallyAvailableAt | Release_Date",
                    flush=True
                )
                sys.exit(4)

            rows = list(reader)
            sample = rows[:50]
            ok = 0
            for r in sample:
                tid = (r.get(track_col) or "").strip()
                dv  = (r.get(date_col) or "").strip()
                if not tid or not tid.isdigit():
                    continue
                if _parse_date_value(dv):
                    ok += 1
            print(f"Preflight: resolvable rows w/ valid date in sample {len(sample)} → {ok}", flush=True)

            edited = 0
            skipped = 0

            # Avoid re-editing the same album to the same value repeatedly
            applied: Set[tuple] = set()  # {(album_rating_key, yyyy-mm-dd)}

            for r in rows:
                raw_id = (r.get(track_col) or "").strip()
                raw_dt = (r.get(date_col)  or "").strip()

                if not raw_id or not raw_id.isdigit():
                    skipped += 1
                    continue

                parsed = _parse_date_value(raw_dt)
                if not parsed:
                    print(f"⚠️  Skip Track_ID {raw_id}: unrecognized date format '{raw_dt}'", flush=True)
                    skipped += 1
                    continue

                date_for_plex = _format_for_plex(parsed)

                # Fetch track, then parent album
                try:
                    track = plex.fetchItem(f"/library/metadata/{int(raw_id)}")
                except Exception as e:
                    print(f"⚠️  Track_ID {raw_id}: fetch failed: {e}", flush=True)
                    skipped += 1
                    continue

                album_rating_key = getattr(track, "parentRatingKey", None)
                if not album_rating_key:
                    print(f"⚠️  Track_ID {raw_id}: no parent album found.", flush=True)
                    skipped += 1
                    continue

                # De-dupe by (album, date)
                if (album_rating_key, date_for_plex) in applied:
                    continue

                try:
                    album = plex.fetchItem(f"/library/metadata/{int(album_rating_key)}")
                except Exception as e:
                    print(f"⚠️  Album fetch failed for Track_ID {raw_id} (Album_ID {album_rating_key}): {e}", flush=True)
                    skipped += 1
                    continue

                try:
                    edits = {
                        "originallyAvailableAt.value": date_for_plex,
                        "originallyAvailableAt.locked": 1,
                    }
                    album.edit(**edits)
                    album.reload()
                    edited += 1
                    applied.add((album_rating_key, date_for_plex))
                except Exception as e:
                    print(f"❌ Album_ID {album_rating_key} (from Track_ID {raw_id}): failed to set release date → '{date_for_plex}'. Error: {e}", flush=True)
                    skipped += 1

            print(f"Summary: edited={edited}, skipped={skipped}", flush=True)
            print(f"Done. Edited={edited} Skipped={skipped}", flush=True)

    except FileNotFoundError:
        print(f"ERROR: CSV not found: {csv_path}", flush=True)
        sys.exit(4)
    except Exception as e:
        print(f"ERROR: Unhandled failure: {e}", flush=True)
        sys.exit(4)

if __name__ == "__main__":
    main()
