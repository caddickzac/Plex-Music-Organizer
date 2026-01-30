#!/usr/bin/env python3
"""
relabel_date_created_from_csv.py

Update Plex music tracks' "Date Created" (Plex 'addedAt') from a CSV.

INPUT (from stdin JSON, like other scripts in your app):
    {
      "action": "relabel: date created",
      "csv_path": "/path/to/your.csv"
    }

REQUIRED ENV:
  PLEX_BASEURL (preferred) or PLEX_URL
  PLEX_TOKEN   (preferred) or PLEX_API_TOKEN

CSV REQUIRED COLUMNS (case-insensitive):
  Track ID:
    - Track_ID | track_id | track_rating_key | rating_key | ratingKey | id
  Date to set (must be the existing "Date Created" column):
    - Date Created

DATE PARSING:
  - Accepts "YYYY-MM-DD", "YYYY/MM/DD", "YYYY-MM-DD HH:MM[:SS]", "MM/DD/YYYY", and epoch seconds.
  - Stored to Plex as YYYY-MM-DD (date-only). If you need time-of-day, change `_format_for_plex`.

EXIT CODES:
  0 = OK
  4 = CSV schema error
  2 = Missing Plex credentials

OUTPUT:
  - Per-row logs on failures
  - Final summary line: "Done. Edited=N Skipped=M"
"""

import os
import sys
import json
import csv
from typing import Optional, Tuple, List
from datetime import datetime, timezone
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
TRACK_ID_COLUMNS = [
    "track_id", "track_rating_key", "rating_key", "ratingkey", "id"
]
# Only "Date Created" (normalized -> date_created)
NEW_DATE_COLUMNS = [
    "date_created"
]

def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "_")

def _find_column(header: List[str], candidates: List[str]) -> Optional[str]:
    norm_header = {_norm(h): h for h in header}
    for c in candidates:
        if c in norm_header:
            return norm_header[c]
    return None

def _parse_input() -> Tuple[str, str]:
    """Read JSON from stdin (preferred). Fallbacks: argv[1] as csv path."""
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

    return csv_path, str(data.get("action", "relabel: date created"))

def _parse_date_value(value: str) -> Optional[str]:
    """
    Return a date string formatted for Plex edit (YYYY-MM-DD).
    Accepts common date formats and epoch seconds.
    """
    if value is None:
        return None
    t = str(value).strip()
    if not t:
        return None

    # Epoch seconds → YYYY-MM-DD (UTC)
    if t.isdigit():
        try:
            dt = datetime.fromtimestamp(int(t), tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    # Normalize separators
    t = t.replace("/", "-")

    # 1) YYYY-MM-DD or YYYY-MM-DD HH:MM[:SS]
    try:
        date_part = t.split()[0]
        parts = date_part.split("-")
        if len(parts) == 3:
            a, b, c = parts
            if len(a) == 4:
                yyyy, mm, dd = a, b.zfill(2), c.zfill(2)
            else:
                # assume MM-DD-YYYY
                mm, dd, yyyy = a.zfill(2), b.zfill(2), c
            if len(yyyy) == 4 and 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31:
                return f"{yyyy}-{mm}-{dd}"
    except Exception:
        pass

    # 2) MM/DD/YYYY styles were normalized above; if they slipped through, give up safely
    return None

def _format_for_plex(yyyy_mm_dd: str) -> str:
    """If you want time-of-day, change this to 'YYYY-MM-DD HH:MM:SS'."""
    return yyyy_mm_dd

def main() -> None:
    csv_path, action = _parse_input()
    print(f"Connecting to Plex @ {PLEX_BASEURL} ...", flush=True)
    plex = PlexServer(PLEX_BASEURL, PLEX_TOKEN)

    # Load CSV header
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            if not header:
                print("ERROR: CSV has no header.", flush=True)
                sys.exit(4)

            track_col = _find_column([h for h in header], TRACK_ID_COLUMNS + ["track_id"])
            date_col  = _find_column([h for h in header], NEW_DATE_COLUMNS)

            if not track_col or not date_col:
                present = list(header)
                print(
                    "ERROR: Could not find required columns.\n"
                    f"Present columns: {present}\n"
                    "Need a track id column from: Track_ID | track_id | track_rating_key | rating_key | ratingKey | id\n"
                    "And a date column named exactly: Date Created",
                    flush=True
                )
                sys.exit(4)

            # Preflight: check first 50 rows have resolvable IDs & parsable dates
            rows = list(reader)
            sample = rows[:50]
            ok = 0
            for r in sample:
                tid = (r.get(track_col) or "").strip()
                dv  = (r.get(date_col) or "").strip()
                if not tid or not tid.isdigit():
                    continue
                parsed = _parse_date_value(dv)
                if not parsed:
                    continue
                ok += 1
            print(f"Preflight: resolvable rows with valid date in sample of {len(sample)} = {ok}", flush=True)

            # Process all rows
            edited = 0
            skipped = 0
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

                try:
                    track = plex.fetchItem(f"/library/metadata/{int(raw_id)}")
                except Exception as e:
                    print(f"⚠️  Track_ID {raw_id}: fetch failed: {e}", flush=True)
                    skipped += 1
                    continue

                try:
                    # Attempt to edit 'addedAt' (Date Created / Date Added in Plex)
                    edits = {
                        "addedAt.value": date_for_plex,
                        "addedAt.locked": 1,  # lock field so Plex agents don't overwrite
                    }
                    track.edit(**edits)
                    track.reload()
                    edited += 1
                except Exception as e:
                    print(f"❌ Track_ID {raw_id}: failed to set Date Created → '{date_for_plex}'. Error: {e}", flush=True)
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
