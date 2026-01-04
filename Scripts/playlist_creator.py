#!/usr/bin/env python3
"""
Playlist Creator — Sonic / History / Genre Mixer (v3.1, filtered-first sampling)

Reads JSON config from stdin, e.g.:

{
  "plex": {
    "url": "http://127.0.0.1:32400",
    "token": "XXXX",
    "music_library": "Music"
  },
  "playlist": {
    "exclude_played_days": 3,
    "history_lookback_days": 30,
    "max_tracks": 50,
    "sonic_similar_limit": 20,
    "historical_ratio": 0.3,
    "exploit_weight": 0.7,

    "min_rating": {
      "track": 7,
      "album": 0,
      "artist": 0
    },
    "allow_unrated": 1,

    "min_play_count": -1,
    "max_play_count": -1,

    "use_time_periods": 1,
    "seed_fallback_mode": "history",
    "seed_mode": "sonic_album_mix",

    "min_year": 0,
    "max_year": 0,
    "min_duration_sec": 0,
    "max_duration_sec": 0,

    "recently_added_days": 0,
    "recently_added_weight": 0.0,

    "max_tracks_per_artist": 0,
    "max_tracks_per_album": 0,

    "history_min_rating": 0,
    "history_max_play_count": -1,

    "genre_strict": 1,
    "allow_off_genre_fraction": 0.2,

    "include_collections": [],
    "exclude_collections": [],
    "exclude_genres": [],

    "custom_title": "My Favorite Mix",
    "preset_name": "Sonic_v1",
    "seed_mode": "track_sonic",

    "seed_track_keys": [],
    "seed_artist_names": [],
    "seed_playlist_names": [],
    "seed_collection_names": [],
    "genre_seeds": []
  }
}
"""

from __future__ import annotations

import os 
import io
import csv
import sys
import json
import random
import time
import textwrap
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from typing import List, Dict, Optional, Set, Tuple
from PIL import Image, ImageDraw, ImageFont
from plexapi.server import PlexServer  # type: ignore
from plexapi.audio import Track, Album, Artist  # type: ignore

# ---------------------------------------------------------------------------
# GLOBAL CACHE & CONSTANTS
# ---------------------------------------------------------------------------

BAR_LEN = 30
_ALBUM_CACHE = {}

# ---------------------------------------------------------------------------
# Simple logging / progress helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)

def log_status(percent: int, message: str) -> None:
    percent = max(0, min(100, percent))
    filled = int(BAR_LEN * percent // 100)
    bar = "=" * filled + "-" * (BAR_LEN - filled)
    print(f"[{bar}] {percent:3d}%  {message}", flush=True)

def log_detail(message: str) -> None:
    print(f"Detail: {message}", flush=True)

def log_warning(message: str) -> None:
    print(f"Warning: {message}", flush=True)


# ---------------------------------------------------------------------------
# Time-of-day / period helpers
# ---------------------------------------------------------------------------

DEFAULT_PERIODS: Dict[str, List[int]] = {
    "Morning": list(range(6, 12)),
    "Afternoon": list(range(12, 17)),
    "Evening": list(range(17, 22)),
    "Late Night": [23, 0, 1, 2, 3, 4, 5],
}


def get_current_time_period(periods: Dict[str, List[int]]) -> str:
    hour = datetime.now().hour
    for name, hours in periods.items():
        if hour in hours:
            return name
    return "Anytime"


def period_hours(period: str, periods: Dict[str, List[int]]) -> List[int]:
    return periods.get(period, list(range(0, 24)))


# ---------------------------------------------------------------------------
# Rating / playcount / recency filters
# ---------------------------------------------------------------------------

def passes_min_ratings(
    track: Track,
    plex: PlexServer,
    min_track: int,
    min_album: int,
    min_artist: int,
    allow_unrated: bool,
) -> bool:
    """
    Enforce minimum userRating thresholds for track/album/artist.

    - Ratings use Plex's 1–10 scale; 0 means 'no filter'.
    - If allow_unrated is True, items with userRating is None are allowed.
    - If allow_unrated is False, items with userRating is None are rejected
      whenever a minimum is set for that level.
    """
    try:
        # Track rating
        if min_track > 0:
            tr = getattr(track, "userRating", None)
            if tr is None and not allow_unrated:
                return False
            if tr is not None and tr < min_track:
                return False

        # Album rating
        if min_album > 0:
            album = None
            try:
                if getattr(track, "parentRatingKey", None):
                    album = plex.fetchItem(track.parentRatingKey)
            except Exception:
                album = None

            if album is not None:
                ar = getattr(album, "userRating", None)
                if ar is None and not allow_unrated:
                    return False
                if ar is not None and ar < min_album:
                    return False

        # Artist rating
        if min_artist > 0:
            artist = None
            try:
                if callable(getattr(track, "artist", None)):
                    artist = track.artist()
            except Exception:
                artist = None

            if artist is not None:
                rr = getattr(artist, "userRating", None)
                if rr is None and not allow_unrated:
                    return False
                if rr is not None and rr < min_artist:
                    return False

        return True
    except Exception:
        # Conservative default: keep the track if something weird happens
        return True


def is_recently_played(
    track: Track,
    exclude_days: int,
    excluded_keys: Set[str],
) -> bool:
    """
    Check recency using exclude_days and excluded_keys.
    excluded_keys usually comes from history() within an exclude window.
    """
    if exclude_days <= 0:
        return False

    if getattr(track, "ratingKey", None) in excluded_keys:
        return True

    last_played = getattr(track, "lastViewedAt", None)
    if not last_played:
        return False

    cutoff = datetime.now() - timedelta(days=exclude_days)
    return last_played >= cutoff


def passes_playcount(
    track: Track,
    min_play_count: Optional[int],
    max_play_count: Optional[int],
) -> bool:
    """
    Enforce inclusive bounds on Plex viewCount (play count).

    - If min_play_count is not None, require viewCount >= min_play_count.
    - If max_play_count is not None, require viewCount <= max_play_count.
    - viewCount of None is treated as 0 (never played).
    """
    try:
        vc = getattr(track, "viewCount", None)
    except Exception:
        vc = None

    if vc is None:
        vc = 0

    if min_play_count is not None and vc < min_play_count:
        return False
    if max_play_count is not None and vc > max_play_count:
        return False
    return True


def popularity_score(track: Track) -> float:
    """
    Simple popularity proxy: ratingCount (Track_Popularity) if present,
    else viewCount, else 0.
    """
    try:
        rc = getattr(track, "ratingCount", None)
        if rc is None:
            rc = getattr(track, "viewCount", 0) or 0
        return float(rc or 0.0)
    except Exception:
        return 0.0

# ---------------------------------------------------------------------------
# Thumbnail Helper
# ---------------------------------------------------------------------------

def create_playlist_thumbnail(title, output_path="thumb.png"):
    # 1. Setup Image (Square, Black)
    size = 1000
    img = Image.new('RGB', (size, size), color='black')
    draw = ImageDraw.Draw(img)
    
    # 2. Load Fonts (Adjust path if on Windows vs Linux)
    try:
        # Bold/Large for Title, Regular for Date
        title_font = ImageFont.truetype("arial.ttf", 80)
        date_font = ImageFont.truetype("arial.ttf", 50)
    except:
        title_font = ImageFont.load_default()
        date_font = ImageFont.load_default()

    # 3. Handle Title (Top Right with Textwrap)
    margin = 40
    # Wraps text to roughly 15 characters per line
    wrapped_title = textwrap.fill(title, width=15) 
    
    # Draw title anchored to the right (ra = Right-Ascender)
    draw.multiline_text((size - margin, margin), wrapped_title, 
                        font=title_font, fill="white", 
                        align="right", anchor="ra", spacing=10)

    # 4. Handle Date (Bottom Left)
    current_date = datetime.now().strftime("%m/%d/%Y")
    draw.text((margin, size - margin), current_date, 
              font=date_font, fill="white", anchor="ld") # ld = Left-Descender

    img.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# Sonic similarity helpers (album / track / artist)
# ---------------------------------------------------------------------------

def get_sonic_similar_albums(album: Album, limit: int) -> List[Album]:
    try:
        return list(album.sonicallySimilar(limit=limit))  # type: ignore[attr-defined]
    except Exception as e:
        # log_warning(f"album.sonicallySimilar not available on '{album.title}': {e}")
        # Fallback: nearest API (if album has ratingKey)
        try:
            rating_key = getattr(album, "ratingKey", None)
            if not rating_key:
                return []
            endpoint = f"/library/metadata/{rating_key}/nearest?context=sonicallySimilar&limit={limit}"
            return list(album.fetchItems(endpoint))  # type: ignore[attr-defined]
        except Exception as e2:
            log_warning(f"Fallback sonic-album API failed: {e2}")
            return []


def get_sonic_similar_tracks(track: Track, limit: int) -> List[Track]:
    """
    FIX: Replaced direct .sonicallySimilar call with Hub/Nearest API.
    """
    try:
        # 1. Try Related Hub (Newer API)
        related = track.getRelated(hub='sonic', count=limit)
        items = [t for t in related if isinstance(t, Track)]
        if items:
            return items
    except Exception:
        pass

    try:
        # 2. Fallback: nearest API via ratingKey
        rk = getattr(track, "ratingKey", None)
        if rk:
            endpoint = f"/library/metadata/{rk}/nearest?context=sonicallySimilar&limit={limit}"
            return list(track.fetchItems(endpoint))
    except Exception as e:
        log_warning(f"Sonic similarity failed for '{track.title}': {e}")
    
    return []


def get_sonic_similar_artists(artist: Artist, limit: int) -> List[Artist]:
    """
    Fetches sonically similar artists using the Plex 'nearest' API endpoint.
    """
    try:
        rk = getattr(artist, "ratingKey", None)
        if rk:
            # Using the 'nearest' endpoint with context 'sonicallySimilar'
            endpoint = f"/library/metadata/{rk}/nearest?context=sonicallySimilar&limit={limit}"
            return list(artist.fetchItems(endpoint))
    except Exception as e:
        log_warning(f"Sonic similarity failed for artist '{artist.title}': {e}")
    return []


# ---------------------------------------------------------------------------
# Album / artist helpers
# ---------------------------------------------------------------------------

def parse_seeds(input_str):
    # This treats the string like a single line of a CSV file
    f = io.StringIO(input_str)
    reader = csv.reader(f, quotechar='"', delimiter=',', quoting=csv.QUOTE_ALL, skipinitialspace=False)
    
    try:
        raw_names = next(reader)
        # Manually strip spaces from the OUTSIDE of the names only
        parsed_names = [n.strip() for n in raw_names if n.strip()]
    except StopIteration:
        parsed_names = []

    log_detail(f"Parsed artist names: {parsed_names}")

def resolve_album(track: Track, plex: PlexServer) -> Optional[Album]:
    ak = getattr(track, "parentRatingKey", None)
    if not ak:
        return None
    
    if ak in _ALBUM_CACHE:
        return _ALBUM_CACHE[ak]
    
    try:
        album = plex.fetchItem(ak)
        if isinstance(album, Album):
            _ALBUM_CACHE[ak] = album
            return album
    except Exception:
        pass
    return None


def _album_year(album: Optional[Album]) -> Optional[int]:
    """
    Get album year, preferring originallyAvailableAt.year,
    falling back to album.year (int).
    """
    if album is None:
        return None
    year_val: Optional[int] = None
    try:
        dt = getattr(album, "originallyAvailableAt", None)
        if dt:
            year_val = dt.year
    except Exception:
        year_val = None
    if year_val is None:
        try:
            y2 = getattr(album, "year", None)
            if isinstance(y2, int):
                year_val = y2
        except Exception:
            year_val = None
    return year_val


def _normalize_name_set(objs) -> Set[str]:
    out: Set[str] = set()
    if not objs:
        return out
    for o in objs:
        try:
            # Get the actual 'tag' (name) if it's a Plex object, otherwise use string
            name = getattr(o, 'tag', str(o)).strip()
            if name:
                out.add(name)
        except Exception:
            pass
    return out


def _album_collections_and_genres(album: Optional[Album]) -> Tuple[Set[str], Set[str]]:
    coll_names: Set[str] = set()
    genre_names: Set[str] = set()
    if album is None:
        return coll_names, genre_names
    
    # Use the fixed normalization helper
    coll_names |= _normalize_name_set(getattr(album, "collections", []))
    
    genres = getattr(album, "genres", [])
    genre_names |= {g.tag.strip().lower() if hasattr(g, 'tag') else str(g).strip().lower() 
                    for g in (genres or [])}
    
    return coll_names, genre_names

def pick_track_from_album(
    album: Album,
    plex: PlexServer,
    exploit_weight: float,
    min_track: int,
    min_album: int,
    min_artist: int,
    allow_unrated: bool,
    exclude_keys: Set[str],
    min_play_count: Optional[int],
    max_play_count: Optional[int],
    min_year: Optional[int],
    max_year: Optional[int],
    min_duration_sec: Optional[int],
    max_duration_sec: Optional[int],
    include_collections: Set[str],
    exclude_collections: Set[str],
    exclude_genres: Set[str],
) -> Optional[Track]:
    """
    Pick a single track from an album using an explore/exploit mix.

    IMPORTANT: This applies the *static filters upfront* at the
    album- and track-level (year, collections, genres, duration, playcount),
    so that we don't sample from albums/tracks that will be rejected later.
    """
    # Album-level year / collections / genres
    year_val = _album_year(album)
    if (min_year is not None or max_year is not None) and year_val is not None:
        if min_year is not None and year_val < min_year:
            return None
        if max_year is not None and year_val > max_year:
            return None

    coll_names, genre_names = _album_collections_and_genres(album)

    # Include collections: only enforce if list is non-empty
    if include_collections:
        if not coll_names.intersection(include_collections):
            return None

    # Exclude collections
    if exclude_collections and coll_names.intersection(exclude_collections):
        return None

    # Exclude genres (album-level)
    if exclude_genres and genre_names.intersection(exclude_genres):
        return None

    try:
        tracks = album.tracks()
    except Exception:
        return None

    candidates: List[Track] = []
    for t in tracks:
        rk = getattr(t, "ratingKey", None)
        if not rk or rk in exclude_keys:
            continue

        if not passes_min_ratings(t, plex, min_track, min_album, min_artist, allow_unrated):
            continue

        if not passes_playcount(t, min_play_count, max_play_count):
            continue

        # Duration filter (track-level, seconds)
        if min_duration_sec is not None or max_duration_sec is not None:
            dur_ms = getattr(t, "duration", None)
            if dur_ms is not None:
                dur_sec = int(dur_ms // 1000)
                if min_duration_sec is not None and dur_sec < min_duration_sec:
                    continue
                if max_duration_sec is not None and dur_sec > max_duration_sec:
                    continue

        candidates.append(t)

    if not candidates:
        return None

    ordered = sorted(candidates, key=popularity_score, reverse=True)
    exploit_weight = max(0.0, min(1.0, exploit_weight))

    r = random.random()
    if r < exploit_weight:
        # top-k slice
        k = max(1, min(5, max(1, len(ordered) // 3)))
        choice = random.choice(ordered[:k])
        mode = "exploit"
    else:
        # bias toward mid–high popularity
        idx = int(random.random() ** 2 * (len(ordered) - 1))
        choice = ordered[idx]
        mode = "explore"

    log_detail(
        f"  Album '{album.title}' → picked '{choice.title}' "
        f"({mode}, pop={popularity_score(choice)})"
    )
    return choice


def pick_track_from_artist(
    artist: Artist,
    plex: PlexServer,
    exploit_weight: float,
    min_track: int,
    min_album: int,
    min_artist: int,
    allow_unrated: bool,
    exclude_keys: Set[str],
    min_play_count: Optional[int],
    max_play_count: Optional[int],
    min_year: Optional[int],
    max_year: Optional[int],
    min_duration_sec: Optional[int],
    max_duration_sec: Optional[int],
    include_collections: Set[str],
    exclude_collections: Set[str],
    exclude_genres: Set[str],
) -> Optional[Track]:
    try:
        albums = artist.albums()
    except Exception:
        return None
    if not albums:
        return None

    random.shuffle(albums)

    for album in albums:
        t = pick_track_from_album(
            album,
            plex,
            exploit_weight,
            min_track,
            min_album,
            min_artist,
            allow_unrated,
            exclude_keys,
            min_play_count,
            max_play_count,
            min_year,
            max_year,
            min_duration_sec,
            max_duration_sec,
            include_collections,
            exclude_collections,
            exclude_genres,
        )
        if t is not None:
            return t
    return None


# ---------------------------------------------------------------------------
# History & genre seed collection
# ---------------------------------------------------------------------------

def collect_history_seeds(
    plex: PlexServer,
    music_section,
    period: str,
    lookback_days: int,
    exclude_days: int,
    use_time_periods: bool,
    history_min_rating: int,
    history_max_play_count: Optional[int],
) -> Tuple[List[Track], Set[str]]:
    """
    Return:
      - history_tracks: List[Track] (NOT TrackHistory)
      - excluded_keys: set of ratingKeys that are too recent to use
    """
    now = datetime.now()
    history_start = now - timedelta(days=lookback_days)
    exclude_start = now - timedelta(days=exclude_days)

    if use_time_periods:
        hours = set(period_hours(period, DEFAULT_PERIODS))
    else:
        hours = set(range(0, 24))

    # Raw history entries (TrackHistory objects)
    history_entries = [
        entry for entry in music_section.history(mindate=history_start)
        if entry.viewedAt and entry.viewedAt.hour in hours
    ]
    exclude_entries = [
        entry for entry in music_section.history(mindate=exclude_start)
        if entry.viewedAt
    ]

    excluded_keys = {entry.ratingKey for entry in exclude_entries}

    history_tracks: List[Track] = []
    for entry in history_entries:
        if entry.ratingKey in excluded_keys:
            continue
        try:
            item = plex.fetchItem(entry.ratingKey)
            if not isinstance(item, Track):
                continue

            # Optional history-only filters
            if history_min_rating > 0:
                r = getattr(item, "userRating", None)
                if r is None or r < history_min_rating:
                    continue

            if history_max_play_count is not None:
                vc = getattr(item, "viewCount", None)
                if vc is not None and vc > history_max_play_count:
                    continue

            history_tracks.append(item)
        except Exception:
            pass

    log_detail(
        f"History window: last {lookback_days} days; exclude window: last {exclude_days} days."
    )
    log_detail(
        f"Historical tracks found in window: {len(history_entries)}; "
        f"excluded keys (recently played): {len(excluded_keys)}"
    )
    return history_tracks, excluded_keys


def collect_genre_tracks(
    music_section,
    plex: PlexServer,
    genres: List[str],
) -> List[Track]:
    """
    Collect genre seeds at the **album level**:
      - searchAlbums(genre=...) in the music section
      - take all tracks from those albums
    """
    if not genres:
        return []

    albums: List[Album] = []
    seen_album_keys: Set[str] = set()

    for g in genres:
        try:
            res = music_section.searchAlbums(genre=g)
        except Exception as e:
            log_warning(f"Genre album search failed for '{g}': {e}")
            continue
        for a in res:
            if not isinstance(a, Album):
                continue
            rk = getattr(a, "ratingKey", None)
            if rk and rk not in seen_album_keys:
                seen_album_keys.add(rk)
                albums.append(a)

    tracks: List[Track] = []
    seen_track_keys: Set[str] = set()
    for album in albums:
        try:
            for t in album.tracks():
                rk = getattr(t, "ratingKey", None)
                if rk and rk not in seen_track_keys:
                    seen_track_keys.add(rk)
                    tracks.append(t)
        except Exception:
            continue

    return tracks


# ---------------------------------------------------------------------------
# Seed collection from various sources
# ---------------------------------------------------------------------------

def collect_seed_tracks_from_keys(
    plex: PlexServer,
    keys: List[str],
) -> List[Track]:
    """
    Interpret each key as a Plex ratingKey.

    - If it's all digits, call /library/metadata/<key>.
    - Otherwise, pass through to plex.fetchItem (for full paths).
    """
    seeds: List[Track] = []
    for raw in keys:
        k = str(raw).strip()
        if not k:
            continue

        try:
            if k.isdigit():
                # Treat as ratingKey
                item = plex.fetchItem(f"/library/metadata/{k}")
            else:
                # Allow full keys like "/library/metadata/12345"
                item = plex.fetchItem(k)

            if isinstance(item, Track):
                seeds.append(item)
            else:
                log_warning(
                    f"Seed key '{k}' did not resolve to a Track; got {type(item)}"
                )
        except Exception as e:
            log_warning(f"Could not fetch seed track '{k}': {e}")
    return seeds


def collect_seed_tracks_from_playlists(
    plex: PlexServer,
    music_section,
    playlist_names: List[str],
) -> List[Track]:
    seeds: List[Track] = []
    if not playlist_names:
        return seeds

    pl_map = {
        pl.title: pl
        for pl in plex.playlists()
        if getattr(pl, "playlistType", "") == "audio"
    }
    for name in playlist_names:
        pl = pl_map.get(name)
        if not pl:
            log_warning(f"Seed playlist '{name}' not found.")
            continue
        try:
            for t in pl.items():
                if isinstance(t, Track):
                    seeds.append(t)
        except Exception as e:
            log_warning(f"Error reading playlist '{name}': {e}")
    return seeds


def collect_seed_tracks_from_collections(
    music_section,
    collection_names: List[str],
) -> List[Track]:
    seeds: List[Track] = []
    if not collection_names:
        return seeds
    for col in collection_names:
        try:
            items = music_section.search(collection=col)
            for item in items:
                try:
                    if hasattr(item, "tracks"):
                        seeds.extend(item.tracks())
                except Exception:
                    pass
        except Exception as e:
            log_warning(f"Collection search failed for '{col}': {e}")
    return seeds


def collect_seed_artists(music_section, names):
    found_artists = []
    for name in names:
        # 1. Try exact match first
        matches = music_section.search(title=name, libtype='artist')
        exact = next((a for a in matches if a.title.lower() == name.lower()), None)
        
        if not exact:
            # 2. SAFETY: Try matching while ignoring spaces (e.g., "Davis,Jr." vs "Davis, Jr.")
            normalized_name = name.replace(" ", "").lower()
            exact = next((a for a in matches if a.title.replace(" ", "").lower() == normalized_name), None)

        if exact:
            found_artists.append(exact)
            log_detail(f"Found seed artist: {exact.title}")
        else:
            log_detail(f"⚠️ Skipping: No library match for '{name}'")
            
    return found_artists


# ---------------------------------------------------------------------------
# Domain / static filter helper (used early and late)
# ---------------------------------------------------------------------------

def track_passes_static_filters(
    track: Track,
    plex: PlexServer,
    cand_seen: Set[str],
    excluded_keys: Set[str],   # Argument #4: Corrected to match the call in main()
    min_track: int,
    min_album: int,
    min_artist: int,
    allow_unrated: bool,
    min_play_count: Optional[int],
    max_play_count: Optional[int],
    min_year: Optional[int],
    max_year: Optional[int],
    min_duration_sec: Optional[int],
    max_duration_sec: Optional[int],
    include_collections: Set[str],
    exclude_collections: Set[str],
    exclude_genres: Set[str],
    reject_reasons: Counter,
) -> bool:
    """
    V3.3 Final: Synced with 18 arguments to prevent TypeError.
    """
    rk = getattr(track, "ratingKey", None)
    if not rk:
        return False
    
    rk_str = str(rk)
    if rk_str in cand_seen:
        reject_reasons["duplicate"] += 1
        return False
    cand_seen.add(rk_str)

    if not passes_min_ratings(track, plex, min_track, min_album, min_artist, allow_unrated):
        reject_reasons["min_ratings"] += 1
        return False

    if not passes_playcount(track, min_play_count, max_play_count):
        reject_reasons["play_count"] += 1
        return False

    # Duration Logic
    dur_ms = getattr(track, "duration", 0)
    if dur_ms:
        ds = int(dur_ms // 1000)
        if min_duration_sec and ds < min_duration_sec:
            reject_reasons["duration"] += 1
            return False
        if max_duration_sec and ds > max_duration_sec:
            reject_reasons["duration"] += 1
            return False

    # Force focus on Album Year
    album = resolve_album(track, plex)
    
    if min_year > 0 or max_year > 0:
        # 1. Try to get year from Album
        y = _album_year(album) if album else 0
        
        # 2. If Album Year is missing, try Track Year as a backup
        if not y or y == 0:
            y = getattr(track, 'year', 0) or 0

        # 3. Final Check
        if not y or y == 0:
            # Only reject if we have NO date at all to verify
            reject_reasons["year_missing"] += 1
            return False
            
        if min_year > 0 and y < min_year:
            reject_reasons["year_too_old"] += 1
            return False
        if max_year > 0 and y > max_year:
            reject_reasons["year_too_new"] += 1
            return False

    colls, genres = _album_collections_and_genres(album)
    
    if include_collections and not colls.intersection(include_collections):
        reject_reasons["collections"] += 1
        return False
    
    if exclude_collections and colls.intersection(exclude_collections):
        reject_reasons["collections"] += 1
        return False

    if exclude_genres and genres.intersection(exclude_genres):
        reject_reasons["genre_exclude"] += 1
        return False

    return True

# ---------------------------------------------------------------------------
# Sonic expansion strategies (now static-filter aware)
# ---------------------------------------------------------------------------

def expand_strict_collection(music_section, collection_names, slider_val, limit):
    all_possible_tracks = []

    for col_name in collection_names:
        try:
            # 1. Fetch the collection object directly by title
            # This works for both Smart and Regular collections
            collection = music_section.collection(col_name)
            
            # 2. Get the items (Albums, Artists, or Tracks)
            items = collection.items()
            log_detail(f"Collection '{col_name}' found {len(items)} items.")
            
            for item in items:
                if item.type == 'artist':
                    for album in item.albums():
                        all_possible_tracks.extend(album.tracks())
                elif item.type == 'album':
                    all_possible_tracks.extend(item.tracks())
                elif item.type == 'track':
                    all_possible_tracks.append(item)

        except Exception as e:
            log_warning(f"Direct fetch failed for collection '{col_name}': {e}")
            # Optional: Fallback to the old search method if direct fetch fails

    scored_list = []
    now = datetime.now()
    for track in all_possible_tracks:
        # Recency: 180-day linear decay
        age_days = (now - track.addedAt).days
        r_score = max(0, 100 - (age_days * (100 / 180)))
        
        # Legacy: Plays + Rating
        plays = getattr(track, 'viewCount', 0) or 0
        l_score = min(100, (plays * 5) + ((track.userRating or 0) * 10))
        
        # Apply Slider
        weight = (r_score * slider_val) + (l_score * (1.0 - slider_val))
        
        # New Music Boost
        if slider_val > 0.5 and plays == 0:
            weight += 30
            
        scored_list.append((track, weight))

    scored_list.sort(key=lambda x: x[1], reverse=True)
    return [t[0] for t in scored_list[:limit]]

def expand_via_sonic_albums(
    seed_tracks: List[Track],
    plex: PlexServer,
    sonic_limit: int,
    exploit_weight: float,
    min_track: int,
    min_album: int,
    min_artist: int,
    allow_unrated: bool,
    exclude_keys: Set[str],
    min_play_count: Optional[int],
    max_play_count: Optional[int],
    min_year: Optional[int],
    max_year: Optional[int],
    min_duration_sec: Optional[int],
    max_duration_sec: Optional[int],
    include_collections: Set[str],
    exclude_collections: Set[str],
    exclude_genres: Set[str],
) -> List[Track]:
    # collect unique seed albums
    albums: List[Album] = []
    seen_albums: Set[str] = set()
    for t in seed_tracks:
        a = resolve_album(t, plex)
        if isinstance(a, Album):
            ak = getattr(a, "ratingKey", None)
            if ak and ak not in seen_albums:
                seen_albums.add(ak)
                albums.append(a)

    log_detail(f"Sonic albums: unique seed albums = {len(albums)}")

    expanded_albums: List[Album] = list(albums)
    for album in albums:
        sims = get_sonic_similar_albums(album, limit=sonic_limit)
        for s in sims:
            if not isinstance(s, Album):
                continue
            rk = getattr(s, "ratingKey", None)
            if rk and rk not in seen_albums:
                seen_albums.add(rk)
                expanded_albums.append(s)

    log_detail(
        f"Sonic albums: total candidate albums (seed + similar) = {len(expanded_albums)}"
    )
    if expanded_albums:
        names = [a.title for a in expanded_albums[:20]]
        log_detail(f"Candidate albums (showing up to 20): {', '.join(names)}")

    results: List[Track] = []
    # Loop through the 67 albums you found
    for album in expanded_albums:
        try:
            # HARVEST up to 6 tracks from each album instead of picking 1
            album_tracks = album.tracks()
            count = 0
            for t in album_tracks:
                # Add basic safety check (not recently played)
                rk = getattr(t, "ratingKey", None)
                if rk and str(rk) not in kwargs.get('exclude_keys', set()):
                    results.append(t)
                    count += 1
                
                if count >= 6: # Max tracks per album cap
                    break
        except Exception:
            continue

    log_detail(f"Sonic albums → harvested tracks: {len(results)}")
    return results


def expand_via_sonic_albums(
    seed_tracks: List[Track],
    plex: PlexServer,
    sonic_limit: int,
    exploit_weight: float,
    min_track: int,
    min_album: int,
    min_artist: int,
    allow_unrated: bool,
    exclude_keys: Set[str],  # This is already here!
    min_play_count: Optional[int],
    max_play_count: Optional[int],
    min_year: Optional[int],
    max_year: Optional[int],
    min_duration_sec: Optional[int],
    max_duration_sec: Optional[int],
    include_collections: Set[str],
    exclude_collections: Set[str],
    exclude_genres: Set[str],
) -> List[Track]:
    # 1. Collect unique seed albums
    albums: List[Album] = []
    seen_albums: Set[str] = set()
    for t in seed_tracks:
        a = resolve_album(t, plex)
        if isinstance(a, Album):
            ak = getattr(a, "ratingKey", None)
            if ak and ak not in seen_albums:
                seen_albums.add(ak)
                albums.append(a)

    log_detail(f"Sonic albums: unique seed albums = {len(albums)}")

    # 2. Expand to similar albums
    expanded_albums: List[Album] = list(albums)
    for album in albums:
        sims = get_sonic_similar_albums(album, limit=sonic_limit)
        for s in sims:
            if not isinstance(s, Album):
                continue
            rk = getattr(s, "ratingKey", None)
            if rk and rk not in seen_albums:
                seen_albums.add(rk)
                expanded_albums.append(s)

    log_detail(f"Sonic albums: total candidate albums = {len(expanded_albums)}")

    # 3. Harvest tracks from the albums
    results: List[Track] = []
    for album in expanded_albums:
        try:
            album_tracks = album.tracks()
            count = 0
            for t in album_tracks:
                rk = getattr(t, "ratingKey", None)
                # Check against the exclude_keys passed into the function
                if rk and str(rk) not in exclude_keys:
                    results.append(t)
                    count += 1
                
                # Cap tracks per album to keep variety high
                if count >= 6: 
                    break
        except Exception:
            continue

    log_detail(f"Sonic albums → harvested tracks: {len(results)}")
    return results

def expand_via_sonic_artists(
    seed_artists: List[Artist],
    plex: PlexServer,
    sonic_limit: int,
    exploit_weight: float,
    min_track: int,
    min_album: int,
    min_artist: int,
    allow_unrated: bool,
    exclude_keys: Set[str],
    min_play_count: Optional[int],
    max_play_count: Optional[int],
    min_year: Optional[int],
    max_year: Optional[int],
    min_duration_sec: Optional[int],
    max_duration_sec: Optional[int],
    include_collections: Set[str],
    exclude_collections: Set[str],
    exclude_genres: Set[str],
) -> List[Track]:
    artists: List[Artist] = list(seed_artists)
    seen: Set[str] = set()

    for a in seed_artists:
        rk = getattr(a, "ratingKey", None)
        if rk:
            seen.add(rk)

    for a in seed_artists:
        sims = get_sonic_similar_artists(a, limit=sonic_limit)
        for s in sims:
            if not isinstance(s, Artist):
                continue
            rk = getattr(s, "ratingKey", None)
            if rk and rk not in seen:
                seen.add(rk)
                artists.append(s)

    log_detail(
        f"Sonic artists: total candidate artists (seed + similar) = {len(artists)}"
    )

    results: List[Track] = []
    for artist in artists:
        # INSTEAD OF pick_track_from_artist (which is 1-or-0)
        # Try to get ALL valid tracks from the artist
        try:
            all_t = artist.tracks()
            valid_from_this_artist = 0
            for track in all_t:
                # Apply your filters here or in a helper
                if track.ratingKey not in exclude_keys:
                    results.append(track)
                    valid_from_this_artist += 1
                
                # Stop after 5-10 tracks so the artist doesn't dominate
                if valid_from_this_artist >= 25: 
                    break
        except Exception:
            continue

    log_detail(f"Sonic artists → harvested tracks: {len(results)}")
    return results


def expand_album_echoes(
    seed_tracks: List[Track],
    plex: PlexServer,
    exploit_weight: float,
    min_track: int,
    min_album: int,
    min_artist: int,
    allow_unrated: bool,
    exclude_keys: Set[str],
    min_play_count: Optional[int],
    max_play_count: Optional[int],
    min_year: Optional[int],
    max_year: Optional[int],
    min_duration_sec: Optional[int],
    max_duration_sec: Optional[int],
    include_collections: Set[str],
    exclude_collections: Set[str],
    exclude_genres: Set[str],
) -> List[Track]:
    albums: List[Album] = []
    seen: Set[str] = set()
    for t in seed_tracks:
        a = resolve_album(t, plex)
        if isinstance(a, Album):
            rk = getattr(a, "ratingKey", None)
            if rk and rk not in seen:
                seen.add(rk)
                albums.append(a)

    log_detail(f"Album echoes: unique albums = {len(albums)}")

    results: List[Track] = []
    for album in albums:
        t = pick_track_from_album(
            album,
            plex,
            exploit_weight,
            min_track,
            min_album,
            min_artist,
            allow_unrated,
            exclude_keys,
            min_play_count,
            max_play_count,
            min_year,
            max_year,
            min_duration_sec,
            max_duration_sec,
            include_collections,
            exclude_collections,
            exclude_genres,
        )
        if t is not None:
            results.append(t)

    log_detail(f"Album echoes → picked tracks: {len(results)}")
    return results


# ---------------------------------------------------------------------------
# Playlist naming
# ---------------------------------------------------------------------------

def seed_mode_label(seed_mode: str) -> str:
    mapping = {
        "history": "History Mix",
        "genre": "Genre Mix",
        "sonic_album_mix": "Sonic Albums",
        "sonic_artist_mix": "Sonic Artists",
        "sonic_combo": "Sonic Combo",
        "album_echoes": "Album Echoes",
        "track_sonic": "Sonic Tracks",
    }
    return mapping.get(seed_mode, seed_mode or "Mix")


def build_playlist_title(
    seed_mode: str,
    period: str,
    custom_title: Optional[str] = None
) -> str:
    """
    If custom_title is provided and non-empty, use it verbatim.
    Otherwise fall back to the automatic seed-mode / day-based title.
    """
    if custom_title:
        t = custom_title.strip()
        if t:
            return t

    today = datetime.now()
    day_name = today.strftime("%A")
    date_str = today.strftime("%y-%m-%d")
    label = seed_mode_label(seed_mode)
    if period and period != "Anytime":
        return f"Playlist Creator • {label} • {day_name} {period} ({date_str})"
    else:
        return f"Playlist Creator • {label} • {day_name} ({date_str})"


def build_playlist_description(
    seed_mode: str,
    period: str,
    tracks: List[Track],
    plex: PlexServer,
) -> str:
    """
    ULTRA-FAST: No network calls. 
    Uses grandparentTitle (Artist) and parentTitle (Album) which are already in memory.
    """
    day_name = datetime.now().strftime("%A")

    # Use attributes already attached to the track objects
    artists = [getattr(t, "grandparentTitle", "Various") for t in tracks]
    albums = [getattr(t, "parentTitle", "Unknown Album") for t in tracks]
    
    artist_counts = Counter(artists)
    top_artists = [a for a, _ in artist_counts.most_common(5) if a]

    parts = [f"Sonic Mix: {seed_mode_label(seed_mode)}."]
    
    # We skip the album-genre lookup because that was the 4-minute bottleneck!
    # Instead, we mention the variety of artists found.
    if top_artists:
        parts.append("Features: " + ", ".join(top_artists) + ".")

    if period and period != "Anytime":
        parts.append(f"Generated for a {period.lower()} session on {day_name}.")
    else:
        parts.append(f"Generated on {day_name}.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    start_time = time.time()  # Start the clock
    final_tracks = []

    # Read JSON payload from stdin
    try:
        raw = sys.stdin.read()
        cfg = json.loads(raw)
    except Exception as e:
        log("ERROR: Could not read/parse JSON from stdin.")
        log(str(e))
        return 2

    plex_cfg = cfg.get("plex", {}) or {}
    pl_cfg = cfg.get("playlist", {}) or {}

    url = plex_cfg.get("url") or plex_cfg.get("baseurl")
    token = plex_cfg.get("token")
    music_lib_name = plex_cfg.get("music_library", "Music")

    if not (url and token):
        log("ERROR: Missing plex.url or plex.token in payload.")
        return 2

    # Basic playlist controls
    exclude_days = int(pl_cfg.get("exclude_played_days", 3))
    lookback_days = int(pl_cfg.get("history_lookback_days", 30))
    max_tracks = int(pl_cfg.get("max_tracks", 50))
    sonic_limit = int(pl_cfg.get("sonic_similar_limit", 20))
    historical_ratio = float(pl_cfg.get("historical_ratio", 0.3))
    exploit_weight = float(pl_cfg.get("exploit_weight", 0.7))

    min_rating = pl_cfg.get("min_rating", {}) or {}
    min_track = int(min_rating.get("track", 0))
    min_album = int(min_rating.get("album", 0))
    min_artist = int(min_rating.get("artist", 0))
    allow_unrated = bool(pl_cfg.get("allow_unrated", 1))

    new_vs_legacy_slider = float(pl_cfg.get("new_vs_legacy_slider", 0.5))

    def _parse_optional_int(val, zero_means_none: bool = False, default_none: bool = True) -> Optional[int]:
        try:
            iv = int(val)
        except Exception:
            return None if default_none else 0
        if iv < 0:
            return None
        if zero_means_none and iv == 0:
            return None
        return iv

    min_play_count = pl_cfg.get("min_play_count")
    if min_play_count is None or min_play_count == -1: min_play_count = None
    
    max_play_count = pl_cfg.get("max_play_count")
    if max_play_count is None or max_play_count == -1: max_play_count = None

    use_time_periods = bool(pl_cfg.get("use_time_periods", 0))
    seed_fallback_mode = (pl_cfg.get("seed_fallback_mode") or "history").lower()

    seed_track_keys = list(pl_cfg.get("seed_track_keys", []) or [])
    seed_artist_names = list(pl_cfg.get("seed_artist_names", []) or [])
    seed_playlist_names = list(pl_cfg.get("seed_playlist_names", []) or [])
    seed_collection_names = list(pl_cfg.get("seed_collection_names", []) or [])
    genre_seeds = [str(g).strip() for g in (pl_cfg.get("genre_seeds", []) or []) if str(g).strip()]

    seed_mode = (pl_cfg.get("seed_mode") or "").strip().lower()

    custom_title = (pl_cfg.get("custom_title") or "").strip()
    preset_name = (pl_cfg.get("preset_name") or "").strip()

    # 1. First, determine the time period
    period = "Anytime"
    if use_time_periods:
        period = get_current_period() # Assuming this helper exists in your script

    # 2. NOW build the title (Now that 'period' and 'custom_title' are ready)
    title = build_playlist_title(
        seed_mode, 
        period, 
        custom_title=custom_title
    )

    # ------------------------------------------------------------------
    # ... [The rest of the script runs here: Step 1, Step 2, Step 3] ...
    # ------------------------------------------------------------------

    # 3. ONLY at the very end, once final_tracks is actually built:
    if final_tracks:
        description = build_playlist_description(
            seed_mode, 
            period, 
            final_tracks, 
            plex,
            preset_name=preset_name
        )

    min_year = int(pl_cfg.get("min_year") or 0)
    max_year = int(pl_cfg.get("max_year") or 0)
    min_duration_sec = int(pl_cfg.get("min_duration_sec") or 0)
    max_duration_sec = int(pl_cfg.get("max_duration_sec") or 0)

    recently_added_days = int(pl_cfg.get("recently_added_days", 0))
    recently_added_weight = float(pl_cfg.get("recently_added_weight", 0.0))
    recently_added_weight = max(0.0, min(1.0, recently_added_weight))

    max_tracks_per_artist = _parse_optional_int(pl_cfg.get("max_tracks_per_artist", 0), zero_means_none=True)
    max_tracks_per_album = _parse_optional_int(pl_cfg.get("max_tracks_per_album", 0), zero_means_none=True)

    history_min_rating = int(pl_cfg.get("history_min_rating", 0))
    history_max_play_count = _parse_optional_int(pl_cfg.get("history_max_play_count", -1))

    genre_strict = bool(pl_cfg.get("genre_strict", 0))
    allow_off_genre_fraction = float(pl_cfg.get("allow_off_genre_fraction", 0.2))
    allow_off_genre_fraction = max(0.0, min(1.0, allow_off_genre_fraction))

    include_collections = {str(x).strip() for x in pl_cfg.get("include_collections", []) if str(x).strip()}
    exclude_collections = {str(x).strip() for x in (pl_cfg.get("exclude_collections", []) or []) if str(x).strip()}
    exclude_genres = {str(x).strip().lower() for x in (pl_cfg.get("exclude_genres", []) or []) if str(x).strip()}

    def _artist_name(tr: Track) -> str:
        # Use grandparentTitle (Plex's standard for Track Artist) to avoid network calls
        return getattr(tr, 'grandparentTitle', 'Unknown Artist')

    def _artist_key(tr: Track) -> str:
        """Returns a unique key for the artist."""
        return getattr(tr.artist, "ratingKey", _artist_name(tr))

    def _album_key_and_genres(tr: Track) -> Tuple[Optional[str], Set[str]]:
        """
        High-speed version: Uses pre-fetched ID and 
        assumes search-level genre filtering is sufficient.
        """
        # 1. Get Album ID (parentRatingKey is pre-fetched)
        ak = getattr(tr, 'parentRatingKey', None)
        
        # 2. Return the seed genres so the shaping loop doesn't reject them.
        # We lowercase them to match the script's internal comparison logic.
        gnames = {g.lower() for g in seed_genre_set}
        
        return str(ak) if ak else None, gnames

    log_status(0, "Starting Playlist Creator...")

    plex = PlexServer(url, token, timeout=60)
    music_section = next(
        (s for s in plex.library.sections() if getattr(s, "title", "") == music_lib_name),
        None,
    )
    if music_section is None:
        log(f"ERROR: Music library '{music_lib_name}' not found.")
        return 3

    if use_time_periods:
        period = get_current_time_period(DEFAULT_PERIODS)
    else:
        period = "Anytime"
    log_status(10, f"Current time period: {period}")

    # Infer seed_mode if not provided
    use_sonic_albums = False
    use_sonic_artists = False
    use_sonic_tracks = False

    # 1. Initialize the slider default at the top of the block
    new_vs_legacy_slider = 0.5

    if not seed_mode:
        if seed_playlist_names or seed_collection_names or seed_artist_names or seed_track_keys:
            seed_mode = "sonic_album_mix"
        elif genre_seeds:
            seed_mode = "genre"
        else:
            seed_mode = "history"

    if seed_mode == "sonic_album_mix":
        use_sonic_albums = True
    elif seed_mode == "sonic_artist_mix":
        use_sonic_artists = True
    elif seed_mode == "strict_collection":
        # New mode: No sonic flags needed as it uses a custom harvester
        use_sonic_albums = False
        use_sonic_artists = False
        new_vs_legacy_slider = float(pl_cfg.get("new_vs_legacy_slider", 0.5))
    elif seed_mode == "sonic_combo":
        use_sonic_albums = True
        use_sonic_artists = True
    elif seed_mode == "track_sonic":
        use_sonic_tracks = True
    elif seed_mode in ("album_echoes", "history", "genre"):
        # uses albums only / pure history / pure genre
        pass
    else:
        log_warning(
            f"Unknown seed_mode '{seed_mode}', defaulting to 'history'."
        )
        seed_mode = "history"

    log_detail(f"Seed mode: {seed_mode}")
    log_detail(f"Explore/Exploit weight (popularity): {exploit_weight:.2f}")
    log_detail(
        f"Min ratings → track={min_track}, album={min_album}, artist={min_artist}, "
        f"allow_unrated={allow_unrated}"
    )
    log_detail(
        f"Play count filter → min={min_play_count}, max={max_play_count}"
    )
    log_detail(
        f"Year filter (album-level) → min_year={min_year}, max_year={max_year}"
    )
    log_detail(
        f"Duration filter → min_sec={min_duration_sec}, max_sec={max_duration_sec}"
    )
    log_detail(
        f"Recently added bias → days={recently_added_days}, weight={recently_added_weight:.2f}"
    )
    log_detail(
        f"Artist/Album caps → max_tracks_per_artist={max_tracks_per_artist}, "
        f"max_tracks_per_album={max_tracks_per_album}"
    )
    log_detail(
        f"History filters → history_min_rating={history_min_rating}, "
        f"history_max_play_count={history_max_play_count}"
    )
    log_detail(
        f"Genre strict={genre_strict}, allow_off_genre_fraction={allow_off_genre_fraction}, "
        f"genre_seeds={genre_seeds}"
    )
    log_detail(
        f"Include collections={list(include_collections)}, "
        f"exclude_collections={list(exclude_collections)}, "
        f"exclude_genres={list(exclude_genres)}"
    )
    log_detail(
        f"use_sonic_albums={use_sonic_albums}, "
        f"use_sonic_artists={use_sonic_artists}, "
        f"use_sonic_tracks={use_sonic_tracks}"
    )

    # ------------------------------------------------------------------
    # Step 1: Collect seed tracks
    # ------------------------------------------------------------------
    seed_tracks: List[Track] = []
    seed_source_counts = defaultdict(int)

    # Explicit track keys
    explicit_track_seeds = collect_seed_tracks_from_keys(plex, seed_track_keys)
    seed_tracks.extend(explicit_track_seeds)
    seed_source_counts["track_keys"] += len(explicit_track_seeds)

    # Playlists
    pl_seeds = collect_seed_tracks_from_playlists(
        plex, music_section, seed_playlist_names
    )
    seed_tracks.extend(pl_seeds)
    seed_source_counts["playlists"] += len(pl_seeds)

    # Collections
    coll_seeds = collect_seed_tracks_from_collections(
        music_section, seed_collection_names
    )
    seed_tracks.extend(coll_seeds)
    seed_source_counts["collections"] += len(coll_seeds)

    # Genre seeds (album-level)
    genre_tracks = collect_genre_tracks(music_section, plex, genre_seeds)

    if seed_mode == "genre":
        seed_tracks.extend(genre_tracks)

    seed_source_counts["genres"] += len(genre_tracks)

    # History seeds (if seed_mode uses history as seeds, or for fallback)
    history_seeds, excluded_keys = collect_history_seeds(
        plex,
        music_section,
        period,
        lookback_days,
        exclude_days,
        use_time_periods,
        history_min_rating,
        history_max_play_count,
    )

    if seed_mode == "history":
        seed_tracks.extend(history_seeds)
    seed_source_counts["history"] += len(history_seeds)

    # Use the value from the JSON config, default to empty string if missing
    artist_input_string = pl_cfg.get("seed_artist_names", "")
    
    # If it's already a list (from JSON), join it; if it's a string, use it
    if isinstance(artist_input_string, list):
        artist_input_string = ",".join(artist_input_string)

    f = io.StringIO(artist_input_string)
    # skipinitialspace=True helps handle spaces after commas
    reader = csv.reader(f, skipinitialspace=True)

    try:
        raw_names = next(reader)
        # This strips spaces AND extra single/double quotes from the edges of names
        seed_artist_names = [n.strip(" '\"") for n in raw_names if n.strip()]
    except StopIteration:
        seed_artist_names = []

    # 3. THE "JR." SAFETY NET
    # If the parser split "Sammy Davis, Jr." into ["Sammy Davis", "Jr."]
    # this logic glues them back together correctly for Plex.
    final_names = []
    for name in seed_artist_names:
        # Check if the current bit is just 'Jr' or 'Jr.'
        if name.lower().rstrip('.') == "jr" and final_names:
            # Re-attach to the previous name with the exact library formatting
            final_names[-1] = f"{final_names[-1]}, Jr."
        else:
            final_names.append(name)
    
    seed_artist_names = final_names
    log_detail(f"Parsed artist names: {seed_artist_names}")

    # LOGGING: See exactly what the parser produced
    if seed_artist_names:
        log_detail(f"Parsed artist names: {seed_artist_names}")

    seed_artists = collect_seed_artists(music_section, seed_artist_names)
    seed_source_counts["artists"] += len(seed_artists)

    # Turn Artists into Seed Tracks
    for artist in seed_artists:
        try:
            # Grab tracks and filter out any None types just in case
            artist_tracks = [tr for tr in artist.tracks() if tr]
            # Take up to 5 tracks as the 'sonic profile' for this artist
            seed_tracks.extend(artist_tracks[:5])
            log_detail(f"Added {len(artist_tracks[:5])} seed tracks for artist: {artist.title}")
        except Exception as e:
            log_detail(f"Could not get tracks for artist {getattr(artist, 'title', 'Unknown')}: {e}")

    # Deduplicate seeds
    seen_seed_keys: Set[str] = set()
    unique_seeds: List[Track] = []
    for t in seed_tracks:
        rk = getattr(t, "ratingKey", None)
        if rk and rk not in seen_seed_keys:
            seen_seed_keys.add(rk)
            unique_seeds.append(t)
    seed_tracks = unique_seeds

    log_detail(
        f"Total seed tracks collected (after dedupe): {len(seed_tracks)}"
    )
    log_detail(
        "Seed counts by source: "
        + str(dict(seed_source_counts))
    )
    if seed_tracks:
        names = [t.title for t in seed_tracks[:15]]
        log_detail(
            "Seed tracks (showing up to 15): " + ", ".join(names)
        )

    # ------------------------------------------------------------------
    # Step 2: If we have very few explicit seeds, optionally fall back
    #         to history or genre for additional seeds.
    # ------------------------------------------------------------------
    MIN_SEEDS = 10
    # ONLY do this if we aren't in strict_collection mode!
    if seed_mode != "strict_collection" and len(seed_tracks) < MIN_SEEDS:
        log_detail(
            f"Seeds below minimum ({len(seed_tracks)} < {MIN_SEEDS}); "
            f"using fallback='{seed_fallback_mode}'."
        )
        if seed_fallback_mode == "history":
             for t in history_seeds:
                rk = getattr(t, "ratingKey", None)
                if rk and rk not in seen_seed_keys:
                    seen_seed_keys.add(rk)
                    seed_tracks.append(t)
        elif seed_fallback_mode == "genre":
            for t in genre_tracks:
                rk = getattr(t, "ratingKey", None)
                if rk and rk not in seen_seed_keys:
                    seen_seed_keys.add(rk)
                    seed_tracks.append(t)
    elif seed_mode == "strict_collection":
        log_detail("Strict Mode: Skipping fallback to ensure playlist stays pure.")

    # ------------------------------------------------------------------
    # Step 3: Expand from seeds via sonic / echoes / genre balancing
    #         (album-year / collections / duration filters applied inside).
    # ------------------------------------------------------------------
    log_status(35, "Expanding from seeds...")

    candidates: List[Track] = []

    if seed_mode == "strict_collection" and include_collections:
        log_status(35, f"Expanding via Strict Collection (Slider: {new_vs_legacy_slider})...")
        
        # 1. Run the function and store tracks in strict_results
        strict_results = expand_strict_collection(
            music_section, 
            include_collections, 
            new_vs_legacy_slider,
            max_tracks * 4
        )
        
        # 2. Add those results to the main 'candidates' list
        # Use strict_results here!
        candidates.extend(strict_results) 
        
        log_detail(f"Strict collection -> Added {len(strict_results)} candidates to pool.")

    # Sonic albums
    if use_sonic_albums and seed_tracks:
        sonic_album_tracks = expand_via_sonic_albums(
            seed_tracks,
            plex,
            sonic_limit,
            exploit_weight,
            min_track,
            min_album,
            min_artist,
            allow_unrated,
            excluded_keys,
            min_play_count,
            max_play_count,
            min_year,
            max_year,
            min_duration_sec,
            max_duration_sec,
            include_collections,
            exclude_collections,
            exclude_genres,
        )
        candidates.extend(sonic_album_tracks)

    # Sonic artists
    if use_sonic_artists and seed_artists:
        sonic_artist_tracks = expand_via_sonic_artists(
            seed_artists,
            plex,
            sonic_limit,
            exploit_weight,
            min_track,
            min_album,
            min_artist,
            allow_unrated,
            excluded_keys,
            min_play_count,
            max_play_count,
            min_year,
            max_year,
            min_duration_sec,
            max_duration_sec,
            include_collections,
            exclude_collections,
            exclude_genres,
        )
        candidates.extend(sonic_artist_tracks)

    # Sonic tracks directly
    if use_sonic_tracks and seed_tracks:
        sonic_track_tracks = expand_via_sonic_tracks(
            seed_tracks,
            plex,
            sonic_limit,
            exploit_weight,
            min_track,
            min_album,
            min_artist,
            allow_unrated,
            excluded_keys,
            min_play_count,
            max_play_count,
            min_year,
            max_year,
            min_duration_sec,
            max_duration_sec,
            include_collections,
            exclude_collections,
            exclude_genres,
        )
        candidates.extend(sonic_track_tracks)

    # Album echoes (no sonic expansion, just one track per seed album)
    if seed_mode == "album_echoes" and seed_tracks:
        echoes = expand_album_echoes(
            seed_tracks,
            plex,
            exploit_weight,
            min_track,
            min_album,
            min_artist,
            allow_unrated,
            excluded_keys,
            min_play_count,
            max_play_count,
            min_year,
            max_year,
            min_duration_sec,
            max_duration_sec,
            include_collections,
            exclude_collections,
            exclude_genres,
        )
        candidates.extend(echoes)

    # Pure genre mode: sample from genre_tracks directly
    if seed_mode == "genre" and genre_tracks:
        candidates.extend(genre_tracks)

    # History: we want a historical_ratio of seeds from history
    if history_seeds and historical_ratio > 0:
        target_hist = int(max_tracks * historical_ratio)
        random.shuffle(history_seeds)
        hist_pick = history_seeds[:target_hist]
        candidates.extend(hist_pick)

    # Always let the original seeds be eligible too
    candidates.extend(seed_tracks)

    # ---------------------------------------------------------
    # STEP 4: UNIFIED FILTERING
    # ---------------------------------------------------------
    raw_candidates = []

    if seed_mode == "strict_collection":
        raw_candidates = candidates # From your Step 3
    
    elif seed_mode == "genre":
        raw_candidates = genre_tracks # From your Step 1
        
    elif seed_mode == "sonic_artist_mix":
        # This uses the 300 tracks you just harvested!
        raw_candidates = sonic_artist_tracks 

    elif seed_mode == "sonic_album_mix":
        raw_candidates = sonic_album_tracks if 'sonic_album_tracks' in locals() else []

    elif seed_mode == "track_sonic":
        raw_candidates = sonic_tracks
        
    else:
        # Default fallback for History or other standard modes
        raw_candidates = history_seeds

    log_status(50, f"Filtering {len(raw_candidates)} candidates for {seed_mode}...")

    # Now we run ONE filtering loop that applies to EVERYTHING
    filtered = []
    seen_ids = set()
    required_genres = [g.lower() for g in genre_seeds]
    reject_reasons = Counter()

    for t in raw_candidates:
        if track_passes_static_filters(
            t, plex, seen_ids, excluded_keys,
            min_track, min_album, min_artist, allow_unrated,
            min_play_count, max_play_count,
            min_year, max_year,
            min_duration_sec, max_duration_sec,
            include_collections, exclude_collections, exclude_genres,
            reject_reasons
        ):
            # --- PRIORITY GENRE CHECK (Short-Circuit) ---
            candidate_genres = []
            
            try:
                # 1. Check Track Level
                t_genres = [g.tag.lower() for g in getattr(t, 'genres', [])]
                if t_genres:
                    candidate_genres = t_genres
                else:
                    # 2. Check Album Level if Track was empty
                    album = t.album()
                    al_genres = [g.tag.lower() for g in getattr(album, 'genres', [])]
                    if al_genres:
                        candidate_genres = al_genres
                    else:
                        # 3. Check Artist Level if Album was empty
                        artist = t.artist()
                        candidate_genres = [g.tag.lower() for g in getattr(artist, 'genres', [])]

                # Now, if we have a required genre (like 'Jazz'), check our best found level
                if required_genres:
                    if not any(rg in candidate_genres for rg in required_genres):
                        reject_reasons["wrong_genre_filtered"] += 1
                        continue
                        
            except Exception as e:
                # Fallback if metadata is totally missing or inaccessible
                continue
            # ---------------------------------------------

            filtered.append(t)

    log_detail(f"Final pool size: {len(filtered)} tracks.")
    
    # This line is super helpful for debugging:
    if reject_reasons:
        log_detail(f"Filter Rejections: {dict(reject_reasons)}")

    # ------------------------------------------------------------------
    # Step 5: Recency bias + artist/album caps + genre strict
    # ------------------------------------------------------------------
    now = datetime.now()

    # Store rejections specifically for the shaping loop
    shaping_rejections = defaultdict(int)

    recent_cutoff = now - timedelta(days=recently_added_days) if recently_added_days > 0 else None

    def _is_recent(tr: Track) -> bool:
        if recent_cutoff is None:
            return False
        added = getattr(tr, "addedAt", None)
        return added >= recent_cutoff if added else False

    recent_pool: List[Track] = [t for t in filtered if _is_recent(t)]
    older_pool: List[Track] = [t for t in filtered if not _is_recent(t)]

    seed_genre_set = {g.lower() for g in genre_seeds}
    # FIX (Step C): Ensure 0 tracks doesn't break logic 
    off_limit = int(max_tracks * allow_off_genre_fraction) if genre_strict else None

    artist_counts: Dict[str, int] = defaultdict(int)
    album_counts: Dict[str, int] = defaultdict(int)
    off_genre_count = 0
    final_tracks: List[Track] = []

    attempts = 0
    max_attempts = len(filtered) * 5

    while len(final_tracks) < max_tracks and (recent_pool or older_pool) and attempts < max_attempts:
        attempts += 1
        pool = None
        if recent_pool and older_pool and recently_added_weight > 0.0:
            pool = recent_pool if random.random() < recently_added_weight else older_pool
        elif recent_pool: pool = recent_pool
        elif older_pool: pool = older_pool

        if not pool: break
        t = pool.pop(random.randrange(len(pool)))

        # --- THE FIX: BYPASS FOR STRICT MODE ---
        if seed_mode == "strict_collection":
            # In strict mode, we trust the harvester. No collection/genre checks needed.
            on_genre = True 
        else:
            # ORIGINAL BOUNCER LOGIC
            album = resolve_album(t, plex)
            colls, _ = _album_collections_and_genres(album)
            
            if include_collections and not colls.intersection(include_collections):
                shaping_rejections["not_in_included_collection"] += 1
                continue

            artist_name = _artist_name(t)
            album_key, album_genres = _album_key_and_genres(t)

            on_genre = True
            if seed_genre_set and not album_genres.intersection(seed_genre_set):
                on_genre = False

            if genre_strict and seed_genre_set and not on_genre:
                if off_limit is not None and off_genre_count >= off_limit:
                    shaping_rejections["genre_mismatch_limit"] += 1
                    continue

        # Diversity Checks (Still helpful in Strict Mode to prevent 50 songs from 1 album)
        artist_name = _artist_name(t)
        album_key, _ = _album_key_and_genres(t)
        if max_tracks_per_artist is not None and artist_counts[artist_name] >= max_tracks_per_artist:
            continue
        if max_tracks_per_album is not None and album_key and album_counts[album_key] >= max_tracks_per_album:
            continue

        # SUCCESS: Add the track
        final_tracks.append(t)
        artist_counts[artist_name] += 1
        if album_key: album_counts[album_key] += 1
        if not on_genre: off_genre_count += 1

    # --- THE SAFETY NET (OUTSIDE THE LOOP) ---
    if not final_tracks:
        if seed_mode == "strict_collection":
            log("⚠️ Shaping loop failed. Using raw candidates as fallback.")
            final_tracks = filtered[:max_tracks]
        else:
            log("❌ ERROR: Shaping loop resulted in 0 tracks. Check filters.")
            return 5

    # ------------------------------------------------------------------
    # Step 6: Update existing playlist OR Create new + Thumbnail
    # ------------------------------------------------------------------
    log_status(80, "Generating title and description...")
    title = build_playlist_title(seed_mode, period, custom_title=custom_title)
    description = build_playlist_description(seed_mode, period, final_tracks, plex)

    log_status(90, "Syncing playlist content and thumbnail...")
    try:
        # 1. Search for existing playlist
        all_playlists = plex.playlists()
        playlist = next((pl for pl in all_playlists if pl.title == title), None)

        if playlist:
            log(f"🔄 Found existing playlist '{title}'. Updating tracks...")
            current_items = playlist.items()
            if current_items:
                playlist.removeItems(current_items)
            playlist.addItems(final_tracks)
        else:
            log(f"✨ Creating new playlist: '{title}'")
            playlist = plex.createPlaylist(title, items=final_tracks)

        # 2. Metadata & Thumbnail
        playlist.edit(summary=description) 

        thumb_filename = f"thumb_{playlist.ratingKey}.png"
        create_playlist_thumbnail(title, thumb_filename) # Ensure this helper is at the top of your file
        
        playlist.uploadPoster(filepath=thumb_filename)
        log(f"✅ Thumbnail uploaded for Playlist ID {playlist.ratingKey}")
        
        if os.path.exists(thumb_filename):
            os.remove(thumb_filename)

    except Exception as e:
        log(f"Error during playlist sync: {e}")
        return 5

    log_status(100, "Playlist creation complete.")

    end_time = time.time()
    print("\n" + "="*50)
    print(f"PLAYLIST GENERATION SUMMARY")
    print(f"Status: SUCCESS")
    print(f"Execution Time: {end_time - start_time:.2f}s")
    print(f"Final Playlist Size: {len(final_tracks)}")
    print("-" * 20)
    print(f"Candidates Considered in Loop: {attempts}")
    print(f"Shaping Rejections: {dict(shaping_rejections)}")
    print("="*50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
