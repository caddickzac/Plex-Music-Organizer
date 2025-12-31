#!/usr/bin/env python3
"""
Playlist Creator — Sonic / History / Genre Mixer (album-level year & genre)

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

    "history_min_rating": 0,
    "history_max_play_count": -1,

    "min_year": 0,
    "max_year": 0,
    "min_duration_sec": 0,
    "max_duration_sec": 0,

    "recently_added_days": 0,
    "recently_added_weight": 0.0,

    "max_tracks_per_artist": 0,
    "max_tracks_per_album": 0,

    "genre_strict": 1,
    "allow_off_genre_fraction": 0.2,
    "exclude_genres": [],
    "include_collections": [],
    "exclude_collections": [],

    "use_time_periods": 1,
    "seed_fallback_mode": "history",
    "seed_mode": "sonic_album_mix",

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
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from typing import List, Dict, Optional, Set, Tuple

from plexapi.server import PlexServer  # type: ignore
from plexapi.audio import Track, Album, Artist  # type: ignore


# ---------------------------------------------------------------------------
# Simple logging / progress helpers
# ---------------------------------------------------------------------------

BAR_LEN = 30


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
# Helpers: album lookup, album year, album genres
# ---------------------------------------------------------------------------

def get_album_for_track(track: Track, plex: PlexServer) -> Optional[Album]:
    """
    Safely resolve the Album corresponding to this Track.
    """
    try:
        if getattr(track, "parentRatingKey", None):
            obj = plex.fetchItem(track.parentRatingKey)
            if isinstance(obj, Album):
                return obj
        # fallback to track.album()
        a = getattr(track, "album", None)
        if callable(a):
            obj2 = a()
            if isinstance(obj2, Album):
                return obj2
    except Exception:
        return None
    return None


def get_album_year_for_track(track: Track, plex: PlexServer) -> Optional[int]:
    """
    Year filter is **album-first**:
      1) album.year
      2) album.originallyAvailableAt.year
      3) track.year
      4) track.originallyAvailableAt.year
    """
    album = get_album_for_track(track, plex)
    year = None

    try:
        if album is not None:
            y = getattr(album, "year", None)
            if y:
                year = int(y)
            else:
                oa = getattr(album, "originallyAvailableAt", None)
                if oa:
                    year = int(oa.year)
    except Exception:
        year = None

    if year is not None:
        return year

    # fallback to track-level if album missing
    try:
        y = getattr(track, "year", None)
        if y:
            return int(y)
        oa = getattr(track, "originallyAvailableAt", None)
        if oa:
            return int(oa.year)
    except Exception:
        return None
    return None


def get_album_genres_for_track(track: Track, plex: PlexServer) -> List[str]:
    """
    All genre-based filters/searches use **album genres**.
    Returns lowercased genre names.
    """
    album = get_album_for_track(track, plex)
    if album is None:
        return []
    try:
        genres = getattr(album, "genres", None) or []
        out = []
        for g in genres:
            s = str(g).strip().lower()
            if s:
                out.append(s)
        return out
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Rating filters (with allow_unrated)
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
            album = get_album_for_track(track, plex)
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
    if exclude_days <= 0:
        return False

    if getattr(track, "ratingKey", None) in excluded_keys:
        return True

    last_played = getattr(track, "lastViewedAt", None)
    if not last_played:
        return False

    cutoff = datetime.now() - timedelta(days=exclude_days)
    return last_played >= cutoff


# ---------------------------------------------------------------------------
# Play count & duration & year filters
# ---------------------------------------------------------------------------

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


def passes_year_filter(
    track: Track,
    plex: PlexServer,
    min_year: Optional[int],
    max_year: Optional[int],
) -> bool:
    """
    Year filter based on **album-year** (with album-first logic).
    If year cannot be determined, we do NOT reject the track purely on year.
    """
    if min_year is None and max_year is None:
        return True

    year = get_album_year_for_track(track, plex)
    if year is None:
        return True

    if min_year is not None and year < min_year:
        return False
    if max_year is not None and year > max_year:
        return False
    return True


def passes_duration_filter(
    track: Track,
    min_sec: Optional[int],
    max_sec: Optional[int],
) -> bool:
    if min_sec is None and max_sec is None:
        return True
    try:
        dur_ms = getattr(track, "duration", None)
        if dur_ms is None:
            return True
        dur_sec = dur_ms / 1000.0
    except Exception:
        return True

    if min_sec is not None and dur_sec < min_sec:
        return False
    if max_sec is not None and dur_sec > max_sec:
        return False
    return True


# ---------------------------------------------------------------------------
# Recency bias (recently added)
# ---------------------------------------------------------------------------

def recency_score(track: Track, recent_days: int) -> float:
    """
    0–1 recency score based on addedAt vs now. Only meaningful when recent_days > 0.
    """
    if recent_days <= 0:
        return 0.0
    try:
        added = getattr(track, "addedAt", None)
        if not added:
            return 0.0
        delta = datetime.now() - added
        d = float(delta.days)
        if d < 0:
            return 1.0
        if d >= recent_days:
            return 0.0
        return max(0.0, 1.0 - d / float(recent_days))
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Genre / collection constraints (album-level genres)
# ---------------------------------------------------------------------------

def album_collection_names(album: Optional[Album]) -> List[str]:
    if album is None:
        return []
    try:
        cols = getattr(album, "collections", None) or []
        out = []
        for c in cols:
            s = getattr(c, "tag", None) or str(c)
            s = s.strip()
            if s:
                out.append(s)
        return out
    except Exception:
        return []


def passes_collection_filters(
    track: Track,
    plex: PlexServer,
    include_collections: List[str],
    exclude_collections: List[str],
) -> bool:
    """
    Collections are evaluated at the album level.
    - include_collections: if non-empty, album must belong to at least one.
    - exclude_collections: if album belongs to any, reject.
    """
    album = get_album_for_track(track, plex)
    album_cols = album_collection_names(album)
    album_cols_lower = [c.lower() for c in album_cols]

    if exclude_collections:
        for col in exclude_collections:
            col_l = col.strip().lower()
            if not col_l:
                continue
            if any(col_l == c or col_l in c for c in album_cols_lower):
                return False

    if include_collections:
        ok = False
        for col in include_collections:
            col_l = col.strip().lower()
            if not col_l:
                continue
            if any(col_l == c or col_l in c for c in album_cols_lower):
                ok = True
                break
        if not ok:
            return False

    return True


def track_is_in_seed_genres(
    track: Track,
    plex: PlexServer,
    genre_seeds: List[str],
) -> bool:
    """
    Check if track's **album genres** overlap (fuzzy) with any seed genre.
    """
    if not genre_seeds:
        return True
    album_genres = get_album_genres_for_track(track, plex)
    if not album_genres:
        return False
    seeds = [g.strip().lower() for g in genre_seeds if g.strip()]
    for g in album_genres:
        for s in seeds:
            if s in g:
                return True
    return False


def track_has_excluded_genre(
    track: Track,
    plex: PlexServer,
    exclude_genres: List[str],
) -> bool:
    """
    Hard exclusion based on **album genres**.
    """
    if not exclude_genres:
        return False
    album_genres = get_album_genres_for_track(track, plex)
    if not album_genres:
        return False
    ex = [g.strip().lower() for g in exclude_genres if g.strip()]
    for g in album_genres:
        for s in ex:
            if s in g:
                return True
    return False


# ---------------------------------------------------------------------------
# Sonic similarity helpers (album / track / artist)
# ---------------------------------------------------------------------------

def get_sonic_similar_albums(album: Album, limit: int) -> List[Album]:
    try:
        return list(album.sonicallySimilar(limit=limit))  # type: ignore[attr-defined]
    except Exception as e:
        log_warning(f"album.sonicallySimilar not available on '{album.title}': {e}")
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
    try:
        return list(track.sonicallySimilar(limit=limit))  # type: ignore[attr-defined]
    except Exception as e:
        log_warning(f"track.sonicallySimilar not available on '{track.title}': {e}")
        try:
            rating_key = getattr(track, "ratingKey", None)
            if not rating_key:
                return []
            endpoint = f"/library/metadata/{rating_key}/nearest?context=sonicallySimilar&limit={limit}"
            return list(track.fetchItems(endpoint))  # type: ignore[attr-defined]
        except Exception as e2:
            log_warning(f"Fallback sonic-track API failed: {e2}")
            return []


def get_sonic_similar_artists(artist: Artist, limit: int) -> List[Artist]:
    try:
        return list(artist.sonicallySimilar(limit=limit))  # type: ignore[attr-defined]
    except Exception as e:
        log_warning(f"artist.sonicallySimilar not available on '{artist.title}': {e}")
        try:
            rating_key = getattr(artist, "ratingKey", None)
            if not rating_key:
                return []
            endpoint = f"/library/metadata/{rating_key}/nearest?context=sonicallySimilar&limit={limit}"
            return list(artist.fetchItems(endpoint))  # type: ignore[attr-defined]
        except Exception as e2:
            log_warning(f"Fallback sonic-artist API failed: {e2}")
            return []


# ---------------------------------------------------------------------------
# Popularity scoring + album / artist track selection
# ---------------------------------------------------------------------------

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


def pick_track_from_album(
    album: Album,
    plex: PlexServer,
    exploit_weight: float,
    min_track: int,
    min_album: int,
    min_artist: int,
    allow_unrated: bool,
    exclude_keys: Set[str],
) -> Optional[Track]:
    """
    Pick a single track from an album using an explore/exploit mix
    based on popularity_score and randomisation.
    """
    try:
        tracks = album.tracks()
    except Exception:
        return None

    candidates: List[Track] = []
    for t in tracks:
        if getattr(t, "ratingKey", None) in exclude_keys:
            continue
        if not passes_min_ratings(t, plex, min_track, min_album, min_artist, allow_unrated):
            continue
        candidates.append(t)

    if not candidates:
        return None

    ordered = sorted(candidates, key=popularity_score, reverse=True)

    r = random.random()
    if r < exploit_weight:
        # top-k slice
        k = max(1, min(5, len(ordered) // 3))
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
            album, plex, exploit_weight,
            min_track, min_album, min_artist,
            allow_unrated,
            exclude_keys,
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
        try:
            item = plex.fetchItem(entry.ratingKey)
            if not isinstance(item, Track):
                continue

            # apply history_min_rating/history_max_play_count at seed stage
            if history_min_rating > 0:
                tr = getattr(item, "userRating", None)
                if tr is None or tr < history_min_rating:
                    continue
            if history_max_play_count is not None:
                vc = getattr(item, "viewCount", None) or 0
                if vc > history_max_play_count:
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
                item = plex.fetchItem(f"/library/metadata/{k}")
            else:
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
# Sonic expansion strategies
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
) -> List[Track]:
    # collect unique seed albums
    albums: List[Album] = []
    seen_albums: Set[str] = set()
    for t in seed_tracks:
        album = get_album_for_track(t, plex)
        if isinstance(album, Album):
            ak = getattr(album, "ratingKey", None)
            if ak and ak not in seen_albums:
                seen_albums.add(ak)
                albums.append(album)

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
            album, plex, exploit_weight,
            min_track, min_album, min_artist,
            allow_unrated,
            exclude_keys,
        )
        if t is not None:
            results.append(t)

    log_detail(f"Sonic albums → picked tracks: {len(results)}")
    return results


def expand_via_sonic_tracks(
    seed_tracks: List[Track],
    plex: PlexServer,
    sonic_limit: int,
    exploit_weight: float,
    min_track: int,
    min_album: int,
    min_artist: int,
    allow_unrated: bool,
    exclude_keys: Set[str],
) -> List[Track]:
    results: List[Track] = []
    for t in seed_tracks:
        sims = get_sonic_similar_tracks(t, limit=sonic_limit)
        for s in sims:
            if not isinstance(s, Track):
                continue
            if getattr(s, "ratingKey", None) in exclude_keys:
                continue
            if not passes_min_ratings(
                s, plex, min_track, min_album, min_artist, allow_unrated
            ):
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
            artist, plex, exploit_weight,
            min_track, min_album, min_artist,
            allow_unrated,
            exclude_keys,
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
) -> List[Track]:
    albums: List[Album] = []
    seen: Set[str] = set()
    for t in seed_tracks:
        album = get_album_for_track(t, plex)
        if isinstance(album, Album):
            rk = getattr(album, "ratingKey", None)
            if rk and rk not in seen:
                seen.add(rk)
                albums.append(album)

    log_detail(f"Album echoes: unique albums = {len(albums)}")

    results: List[Track] = []
    for album in albums:
        t = pick_track_from_album(
            album, plex, exploit_weight,
            min_track, min_album, min_artist,
            allow_unrated,
            exclude_keys,
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


def build_playlist_title(seed_mode: str, period: str) -> str:
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
) -> str:
    day_name = datetime.now().strftime("%A")

    genres = [str(g) for t in tracks for g in (getattr(t, "genres", None) or [])]
    artists = [getattr(t, "grandparentTitle", "") or "" for t in tracks]
    genre_counts = Counter(genres)
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

    exclude_days = int(pl_cfg.get("exclude_played_days", 3))
    lookback_days = int(pl_cfg.get("history_lookback_days", 30))
    max_tracks = int(pl_cfg.get("max_tracks", 50))
    sonic_limit = int(pl_cfg.get("sonic_similar_limit", 20))
    historical_ratio = float(pl_cfg.get("historical_ratio", 0.3))
    exploit_weight = float(pl_cfg.get("exploit_weight", 0.7))  # optional

    min_rating = pl_cfg.get("min_rating", {}) or {}
    min_track = int(min_rating.get("track", 0))
    min_album = int(min_rating.get("album", 0))
    min_artist = int(min_rating.get("artist", 0))

    allow_unrated = bool(pl_cfg.get("allow_unrated", 1))

    # Play count filters (–1 means "no bound")
    raw_min_pc = pl_cfg.get("min_play_count", -1)
    raw_max_pc = pl_cfg.get("max_play_count", -1)
    try:
        min_play_count = int(raw_min_pc)
    except Exception:
        min_play_count = -1
    try:
        max_play_count = int(raw_max_pc)
    except Exception:
        max_play_count = -1

    if min_play_count < 0:
        min_play_count = None
    if max_play_count < 0:
        max_play_count = None

    # History filters
    history_min_rating = int(pl_cfg.get("history_min_rating", 0))
    raw_hist_max_pc = pl_cfg.get("history_max_play_count", -1)
    try:
        history_max_play_count = int(raw_hist_max_pc)
    except Exception:
        history_max_play_count = -1
    if history_max_play_count < 0:
        history_max_play_count = None

    # Era / year filter (album-level)
    def _to_year(v):
        try:
            v_int = int(v)
            return v_int if v_int > 0 else None
        except Exception:
            return None

    min_year = _to_year(pl_cfg.get("min_year", 0))
    max_year = _to_year(pl_cfg.get("max_year", 0))

    # Duration filter (seconds)
    def _to_sec(v):
        try:
            v_int = int(v)
            return v_int if v_int > 0 else None
        except Exception:
            return None

    min_duration_sec = _to_sec(pl_cfg.get("min_duration_sec", 0))
    max_duration_sec = _to_sec(pl_cfg.get("max_duration_sec", 0))

    # Recency bias
    recently_added_days = int(pl_cfg.get("recently_added_days", 0))
    recently_added_weight = float(pl_cfg.get("recently_added_weight", 0.0))

    # Artist / album diversity caps
    max_tracks_per_artist = int(pl_cfg.get("max_tracks_per_artist", 0))
    max_tracks_per_album = int(pl_cfg.get("max_tracks_per_album", 0))

    if max_tracks_per_artist <= 0:
        max_tracks_per_artist = None  # type: ignore[assignment]
    if max_tracks_per_album <= 0:
        max_tracks_per_album = None  # type: ignore[assignment]

    use_time_periods = bool(pl_cfg.get("use_time_periods", 0))
    seed_fallback_mode = (pl_cfg.get("seed_fallback_mode") or "history").lower()

    seed_track_keys = list(pl_cfg.get("seed_track_keys", []) or [])
    seed_artist_names = list(pl_cfg.get("seed_artist_names", []) or [])
    seed_playlist_names = list(pl_cfg.get("seed_playlist_names", []) or [])
    seed_collection_names = list(pl_cfg.get("seed_collection_names", []) or [])
    genre_seeds = list(pl_cfg.get("genre_seeds", []) or [])

    # Genre strict / exclusions
    genre_strict = bool(pl_cfg.get("genre_strict", 0))
    allow_off_genre_fraction = float(pl_cfg.get("allow_off_genre_fraction", 0.2))
    exclude_genres = list(pl_cfg.get("exclude_genres", []) or [])
    include_collections = list(pl_cfg.get("include_collections", []) or [])
    exclude_collections = list(pl_cfg.get("exclude_collections", []) or [])

    seed_mode = (pl_cfg.get("seed_mode") or "").strip()

    log_status(0, "Starting Playlist Creator...")

    # Connect to Plex
    plex = PlexServer(url, token, timeout=60)
    music_section = next(
        (s for s in plex.library.sections() if getattr(s, "title", "") == music_lib_name),
        None,
    )
    if music_section is None:
        log(f"ERROR: Music library '{music_lib_name}' not found.")
        return 3

    # Determine time period
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
    else:
        seed_mode = seed_mode.lower()

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
        f"Year filter → min_year={min_year}, max_year={max_year}"
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
        f"Include collections={include_collections}, exclude_collections={exclude_collections}, "
        f"exclude_genres={exclude_genres}"
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

    # History seeds
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
    # Step 2: Fallback if very few seeds
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
    # Step 3: Expand from seeds via sonic / echoes / genre / history mix
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
    # Step 4: Filter, dedupe, rating filters, play count, year/genre, etc.
    # ------------------------------------------------------------------
    log_status(50, "Filtering and deduplicating candidates...")

    cand_seen: Set[str] = set()
    filtered: List[Track] = []
    rejected_counts = defaultdict(int)

    for t in candidates:
        rk = getattr(t, "ratingKey", None)
        if not rk or rk in cand_seen:
            continue
        cand_seen.add(rk)

        if is_recently_played(t, exclude_days, excluded_keys):
            rejected_counts["recently_played"] += 1
            continue
        if not passes_min_ratings(
            t, plex, min_track, min_album, min_artist, allow_unrated
        ):
            rejected_counts["min_ratings"] += 1
            continue
        if not passes_playcount(t, min_play_count, max_play_count):
            rejected_counts["playcount"] += 1
            continue
        if not passes_year_filter(t, plex, min_year, max_year):
            rejected_counts["year"] += 1
            continue
        if not passes_duration_filter(t, min_duration_sec, max_duration_sec):
            rejected_counts["duration"] += 1
            continue
        if not passes_collection_filters(t, plex, include_collections, exclude_collections):
            rejected_counts["collections"] += 1
            continue
        if track_has_excluded_genre(t, plex, exclude_genres):
            rejected_counts["exclude_genres"] += 1
            continue

        filtered.append(t)

    log_detail(f"Candidates after dedupe & filters: {len(filtered)}")
    if rejected_counts:
        log_detail(f"Rejected counts by reason: {dict(rejected_counts)}")

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

        extra: List[Track] = []
        for t in fb_tracks:
            rk = getattr(t, "ratingKey", None)
            if not rk or rk in cand_seen:
                continue
            if is_recently_played(t, exclude_days, excluded_keys):
                continue
            if not passes_min_ratings(
                t, plex, min_track, min_album, min_artist, allow_unrated
            ):
                continue
            if not passes_playcount(t, min_play_count, max_play_count):
                continue
            if not passes_year_filter(t, plex, min_year, max_year):
                continue
            if not passes_duration_filter(t, min_duration_sec, max_duration_sec):
                continue
            if not passes_collection_filters(t, plex, include_collections, exclude_collections):
                continue
            if track_has_excluded_genre(t, plex, exclude_genres):
                continue

            cand_seen.add(rk)
            extra.append(t)
            if len(filtered) + len(extra) >= max_tracks * 2:
                break

        filtered.extend(extra)
        log_detail(f"After fallback, candidates count: {len(filtered)}")

    if not filtered:
        log("ERROR: No tracks available after filtering.")
        return 4

    # ------------------------------------------------------------------
    # Step 4b: Genre strictness (album-level)
    # ------------------------------------------------------------------
    if genre_strict and genre_seeds:
        in_genre: List[Track] = []
        off_genre: List[Track] = []
        for t in filtered:
            if track_is_in_seed_genres(t, plex, genre_seeds):
                in_genre.append(t)
            else:
                off_genre.append(t)

        random.shuffle(in_genre)
        random.shuffle(off_genre)

        max_off = int(len(filtered) * allow_off_genre_fraction)
        filtered = in_genre + off_genre[:max_off]
        log_detail(
            f"After genre strictness: in_genre={len(in_genre)}, "
            f"off_genre_used={min(len(off_genre), max_off)}, "
            f"total={len(filtered)}"
        )

        if not filtered:
            log("ERROR: No tracks remain after genre strict filtering.")
            return 4

    # ------------------------------------------------------------------
    # Step 4c: Recency bias ordering
    # ------------------------------------------------------------------
    if recently_added_days > 0 and recently_added_weight > 0.0:
        def _score(t: Track) -> float:
            return popularity_score(t) + recently_added_weight * recency_score(t, recently_added_days)

        filtered.sort(key=_score, reverse=True)
    else:
        random.shuffle(filtered)

    # ------------------------------------------------------------------
    # Step 4d: Artist / album caps and final truncation
    # ------------------------------------------------------------------
    final_tracks: List[Track] = []
    artist_counts: Dict[str, int] = defaultdict(int)
    album_counts: Dict[str, int] = defaultdict(int)

    for t in filtered:
        if len(final_tracks) >= max_tracks:
            break

        artist_key = (
            getattr(t, "grandparentRatingKey", None)
            or getattr(t, "grandparentTitle", None)
            or ""
        )
        album_key = (
            getattr(t, "parentRatingKey", None)
            or getattr(t, "parentTitle", None)
            or ""
        )

        if max_tracks_per_artist is not None and artist_key:
            if artist_counts[artist_key] >= max_tracks_per_artist:
                continue
        if max_tracks_per_album is not None and album_key:
            if album_counts[album_key] >= max_tracks_per_album:
                continue

        final_tracks.append(t)
        if artist_key:
            artist_counts[artist_key] += 1
        if album_key:
            album_counts[album_key] += 1

    log_status(60, f"Final track count before playlist creation: {len(final_tracks)}")

    if not final_tracks:
        log("ERROR: No tracks selected after applying artist/album caps.")
        return 4

    # ------------------------------------------------------------------
    # Step 5: Create playlist
    # ------------------------------------------------------------------
    log_status(80, "Generating title and description...")
    title = build_playlist_title(seed_mode, period)
    description = build_playlist_description(seed_mode, period, final_tracks)

    log_status(90, "Creating playlist in Plex...")
    try:
        playlist = plex.createPlaylist(title, items=final_tracks)
        try:
            playlist.editSummary(description)
        except Exception as e:
            log_warning(f"Could not edit playlist summary: {e}")
    except Exception as e:
        log(f"Error creating playlist: {e}")
        return 5

    log_status(100, "Playlist creation complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
