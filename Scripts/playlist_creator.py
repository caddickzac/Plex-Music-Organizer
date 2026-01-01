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

    "custom_title": "",

    "seed_track_keys": [],
    "seed_artist_names": [],
    "seed_playlist_names": [],
    "seed_collection_names": [],
    "genre_seeds": []
  }
}
"""

from __future__ import annotations

import sys
import json
import random
import time
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from typing import List, Dict, Optional, Set, Tuple

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


def get_sonic_similar_albums(album: Album, limit: int) -> List[Album]:
    try:
        rk = getattr(album, "ratingKey", None)
        if rk:
            endpoint = f"/library/metadata/{rk}/nearest?context=sonicallySimilar&limit={limit}"
            return list(album.fetchItems(endpoint))
    except Exception as e:
        log_warning(f"Sonic similarity failed for album '{album.title}': {e}")
    return []


# ---------------------------------------------------------------------------
# Album / artist helpers
# ---------------------------------------------------------------------------

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


def collect_seed_artists(music_section, artist_names: List[str]) -> List[Artist]:
    artists: List[Artist] = []
    if not artist_names:
        return artists
    for name in artist_names:
        try:
            res = music_section.search(title=name, libtype="artist")
            if res:
                artists.append(res[0])
            else:
                log_warning(f"Seed artist '{name}' not found.")
        except Exception as e:
            log_warning(f"Artist search failed for '{name}': {e}")
    return artists


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

    album = resolve_album(track, plex)
    
    if album and (min_year or max_year):
        y = _album_year(album)
        if y:
            if min_year and y < min_year:
                reject_reasons["year"] += 1
                return False
            if max_year and y > max_year:
                reject_reasons["year"] += 1
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
    for album in expanded_albums:
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

    log_detail(f"Sonic albums → picked tracks: {len(results)}")
    return results


def expand_via_sonic_tracks(
    seed_tracks: List[Track],
    plex: PlexServer,
    sonic_limit: int,
    exploit_weight: float,   # kept for signature parity; not used here currently
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
    results: List[Track] = []
    for t in seed_tracks:
        sims = get_sonic_similar_tracks(t, limit=sonic_limit)
        for s in sims:
            if not isinstance(s, Track):
                continue
            rk = getattr(s, "ratingKey", None)
            if not rk or rk in exclude_keys:
                continue

            if not passes_min_ratings(
                s, plex, min_track, min_album, min_artist, allow_unrated
            ):
                continue

            if not passes_playcount(s, min_play_count, max_play_count):
                continue

            album = resolve_album(s, plex)
            year_val = _album_year(album)
            if (min_year is not None or max_year is not None) and year_val is not None:
                if min_year is not None and year_val < min_year:
                    continue
                if max_year is not None and year_val > max_year:
                    continue

            # Collections / genres (album-level only)
            coll_names, album_genres = _album_collections_and_genres(album)
            if include_collections and not coll_names.intersection(include_collections):
                continue
            if exclude_collections and coll_names.intersection(exclude_collections):
                continue
            if exclude_genres and album_genres.intersection(exclude_genres):
                continue

            # Duration
            if min_duration_sec is not None or max_duration_sec is not None:
                dur_ms = getattr(s, "duration", None)
                if dur_ms is not None:
                    dur_sec = int(dur_ms // 1000)
                    if min_duration_sec is not None and dur_sec < min_duration_sec:
                        continue
                    if max_duration_sec is not None and dur_sec > max_duration_sec:
                        continue

            results.append(s)

    log_detail(f"Sonic tracks → picked tracks: {len(results)}")
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
        t = pick_track_from_artist(
            artist,
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

    log_detail(f"Sonic artists → picked tracks: {len(results)}")
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
    Build description using ALBUM-LEVEL genres only.
    """
    day_name = datetime.now().strftime("%A")

    # Album-level genre list
    genre_list: List[str] = []
    for t in tracks:
        album = resolve_album(t, plex)
        if album is None:
            continue
        try:
            for g in getattr(album, "genres", None) or []:
                s = str(g).strip()
                if s:
                    genre_list.append(s)
        except Exception:
            pass

    # Artist names (from grandparentTitle where possible)
    artists = [getattr(t, "grandparentTitle", "") or "" for t in tracks]
    genre_counts = Counter(genre_list)
    artist_counts = Counter(artists)

    top_genres = [g for g, _ in genre_counts.most_common(3)]
    top_artists = [a for a, _ in artist_counts.most_common(5)]

    parts = [f"Seed mode: {seed_mode_label(seed_mode)}."]
    if top_genres:
        parts.append("Top genres: " + ", ".join(top_genres) + ".")
    if top_artists:
        parts.append("Frequent artists: " + ", ".join(top_artists) + ".")

    if period and period != "Anytime":
        parts.append(f"Built for a {period.lower()} session on {day_name}.")
    else:
        parts.append(f"Built for {day_name} listening.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    start_time = time.time()  # Start the clock

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

    min_play_count = _parse_optional_int(pl_cfg.get("min_play_count", -1), zero_means_none=False)
    max_play_count = _parse_optional_int(pl_cfg.get("max_play_count", -1), zero_means_none=False)

    use_time_periods = bool(pl_cfg.get("use_time_periods", 0))
    seed_fallback_mode = (pl_cfg.get("seed_fallback_mode") or "history").lower()

    seed_track_keys = list(pl_cfg.get("seed_track_keys", []) or [])
    seed_artist_names = list(pl_cfg.get("seed_artist_names", []) or [])
    seed_playlist_names = list(pl_cfg.get("seed_playlist_names", []) or [])
    seed_collection_names = list(pl_cfg.get("seed_collection_names", []) or [])
    genre_seeds = [str(g).strip() for g in (pl_cfg.get("genre_seeds", []) or []) if str(g).strip()]

    seed_mode = (pl_cfg.get("seed_mode") or "").strip().lower()
    custom_title = (pl_cfg.get("custom_title") or "").strip()

    min_year = _parse_optional_int(pl_cfg.get("min_year", 0), zero_means_none=True)
    max_year = _parse_optional_int(pl_cfg.get("max_year", 0), zero_means_none=True)
    min_duration_sec = _parse_optional_int(pl_cfg.get("min_duration_sec", 0), zero_means_none=True)
    max_duration_sec = _parse_optional_int(pl_cfg.get("max_duration_sec", 0), zero_means_none=True)

    recently_added_days = int(pl_cfg.get("recently_added_days", 0))
    recently_added_weight = float(pl_cfg.get("recently_added_weight", 0.0))
    recently_added_weight = max(0.0, min(1.0, recently_added_weight))

    # FIX (Step A): Treat 0 as None (no cap) to avoid rejecting all tracks
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

    # Artist seeds (for sonic artists)
    seed_artists = collect_seed_artists(music_section, seed_artist_names)
    seed_source_counts["artists"] += len(seed_artists)

    # Deduplicate seeds by ratingKey
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
    if len(seed_tracks) < MIN_SEEDS:
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

    # ------------------------------------------------------------------
    # Step 3: Expand from seeds via sonic / echoes / genre balancing
    #         (album-year / collections / duration filters applied inside).
    # ------------------------------------------------------------------
    log_status(35, "Expanding from seeds...")

    candidates: List[Track] = []

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

    # ------------------------------------------------------------------
    # Step 4: Static filters + fallback if underfilled
    # ------------------------------------------------------------------
    log_status(50, "Filtering and deduplicating candidates...")

    cand_seen: Set[str] = set()
    filtered: List[Track] = []
    reject_reasons: Counter = Counter()

    def _try_add_candidate(t: Track) -> None:
        # Recency check (exclude_played_days)
        if is_recently_played(t, exclude_days, excluded_keys):
            reject_reasons["recently_played"] += 1
            return

        if track_passes_static_filters(
            t,
            plex,
            cand_seen,
            excluded_keys,
            min_track,
            min_album,
            min_artist,
            allow_unrated,
            min_play_count,
            max_play_count,
            min_year,
            max_year,
            min_duration_sec,
            max_duration_sec,
            include_collections,
            exclude_collections,
            exclude_genres,
            reject_reasons,
        ):
            filtered.append(t)

    for t in candidates:
        _try_add_candidate(t)

    log_detail(f"Candidates after dedupe & filters: {len(filtered)}")
    if reject_reasons:
        log_detail(f"Rejected counts by reason: {dict(reject_reasons)}")

    if filtered:
        names = [t.title for t in filtered[:25]]
        log_detail(
            "Candidate tracks after static filters (showing up to 25): "
            + ", ".join(names)
        )

    # If still underfilled, fall back again to history / genre
    if len(filtered) < max_tracks:
        log_detail(
            f"Filtered candidates < max_tracks ({len(filtered)} < {max_tracks}); "
            f"using fallback='{seed_fallback_mode}'."
        )
        if seed_fallback_mode == "history":
            fb_tracks = history_seeds
        elif seed_fallback_mode == "genre":
            fb_tracks = genre_tracks
        else:
            fb_tracks = []

        for t in fb_tracks:
            _try_add_candidate(t)

        log_detail(f"After fallback, candidates count: {len(filtered)}")

    if not filtered:
        log("ERROR: No tracks available after filtering.")
        return 4

    # ------------------------------------------------------------------
    # Step 5: Recency bias + artist/album caps + genre strict
    # ------------------------------------------------------------------
    now = datetime.now()
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

        artist_name = _artist_name(t)
        album_key, album_genres = _album_key_and_genres(t)

        on_genre = True
        if seed_genre_set and not album_genres.intersection(seed_genre_set):
            on_genre = False

        # FIX (Step C): Improved Genre Strictness Check
        if genre_strict and seed_genre_set and not on_genre:
            if off_limit is not None and off_genre_count >= off_limit:
                reject_reasons["genre_off_limit"] += 1
                continue

        # Diversity Checks (Now works correctly if caps are None)
        if max_tracks_per_artist is not None and artist_counts[artist_name] >= max_tracks_per_artist:
            reject_reasons["artist_cap"] += 1
            continue
        if max_tracks_per_album is not None and album_key and album_counts[album_key] >= max_tracks_per_album:
            reject_reasons["album_cap"] += 1
            continue

        final_tracks.append(t)
        artist_counts[artist_name] += 1
        if album_key: album_counts[album_key] += 1
        if not on_genre: off_genre_count += 1

    # ------------------------------------------------------------------
    # Step 6: Create playlist and Summary
    # ------------------------------------------------------------------
    log_status(80, "Generating title and description...")
    title = build_playlist_title(seed_mode, period, custom_title=custom_title)
    description = build_playlist_description(seed_mode, period, final_tracks, plex)

    log_status(90, "Creating playlist in Plex...")
    try:
        playlist = plex.createPlaylist(title, items=final_tracks)
        playlist.editSummary(description)
    except Exception as e:
        log(f"Error creating playlist: {e}")
        return 5

    log_status(100, "Playlist creation complete.")

    # FIX (Step B): Moved summary ABOVE return statement
    end_time = time.time()
    print("\n" + "="*50)
    print(f"PLAYLIST GENERATION SUMMARY")
    print(f"Status: SUCCESS")
    print(f"Total Candidates Processed: {len(candidates)}")
    print(f"Final Tracks in Playlist: {len(final_tracks)}")
    print(f"Total Execution Time: {end_time - start_time:.2f} seconds")
    print("="*50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
