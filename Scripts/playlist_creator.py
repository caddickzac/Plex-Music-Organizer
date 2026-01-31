#!/usr/bin/env python3
"""
Playlist Creator — Unified Version (v3.5)
Compatible with: Unraid (Docker) AND Windows/Mac (Laptop)
Features:
  - "History + Seeds" (Union)
  - "Sonic History" (Intersection) + Smart Backfill
  - "Track Sonic" (Track-to-Track Similarity)
  - "Smart Seed Selection" (Explore/Exploit for Artist seeds)
  - "Track-First Genre" (Prioritizes Track metadata)
"""

from __future__ import annotations
import argparse
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
import warnings
# Suppress the noise about "edit" vs "editSummary"
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Try/Except to handle missing libraries on different machines
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("❌ ERROR: Pillow library not found. Install with: pip install Pillow")
    sys.exit(1)

try:
    from plexapi.server import PlexServer
    from plexapi.audio import Track, Album, Artist
except ImportError:
    print("❌ ERROR: PlexAPI library not found. Install with: pip install plexapi")
    sys.exit(1)

# ---------------------------------------------------------------------------
# GLOBAL CACHE & CONSTANTS
# ---------------------------------------------------------------------------

BAR_LEN = 30
_ALBUM_CACHE = {}
_ARTIST_METADATA_CACHE = {}

# ---------------------------------------------------------------------------
# LOGGING HELPER
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
# TIME PERIODS
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
# THUMBNAIL GENERATOR
# ---------------------------------------------------------------------------

def create_playlist_thumbnail(title, output_path="thumb.png"):
    size = 1000
    img = Image.new('RGB', (size, size), color='black')
    draw = ImageDraw.Draw(img)
    
    title_font = None
    date_font = None

    linux_fonts = [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ]
    
    # 1. Try Linux Paths
    for fpath in linux_fonts:
        if os.path.exists(fpath):
            try:
                title_font = ImageFont.truetype(fpath, 95)
                date_font = ImageFont.truetype(fpath, 80)
                break
            except:
                continue

    # 2. Try Windows/Standard Paths
    if title_font is None:
        try:
            title_font = ImageFont.truetype("arial.ttf", 95)
            date_font = ImageFont.truetype("arial.ttf", 80)
        except OSError:
            log_warning("Could not load custom fonts. Using default.")
            title_font = ImageFont.load_default()
            date_font = ImageFont.load_default()

    margin = 40
    wrapped_title = textwrap.fill(title, width=15)
    draw.multiline_text((size - margin, margin), wrapped_title, 
                        font=title_font, fill="white", 
                        align="right", anchor="ra", spacing=10)

    current_date = datetime.now().strftime("%m/%d/%Y")
    draw.text((margin, size - margin), current_date, 
              font=date_font, fill="white", anchor="ld")

    img.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# FILTERS & RATINGS
# ---------------------------------------------------------------------------

def passes_min_ratings(track: Track, plex: PlexServer, min_track: int, min_album: int, min_artist: int, allow_unrated: bool) -> bool:
    try:
        if min_track > 0:
            tr = getattr(track, "userRating", None)
            if tr is None and not allow_unrated: return False
            if tr is not None and tr < min_track: return False

        if min_album > 0:
            album = None
            try:
                if getattr(track, "parentRatingKey", None):
                    album = plex.fetchItem(track.parentRatingKey)
            except: pass
            if album:
                ar = getattr(album, "userRating", None)
                if ar is None and not allow_unrated: return False
                if ar is not None and ar < min_album: return False

        if min_artist > 0:
            artist = None
            try:
                if callable(getattr(track, "artist", None)):
                    artist = track.artist()
            except: pass
            if artist:
                rr = getattr(artist, "userRating", None)
                if rr is None and not allow_unrated: return False
                if rr is not None and rr < min_artist: return False
        return True
    except:
        return True

def passes_playcount(track: Track, min_play_count: Optional[int], max_play_count: Optional[int]) -> bool:
    vc = getattr(track, "viewCount", 0) or 0
    if min_play_count is not None and vc < min_play_count: return False
    if max_play_count is not None and vc > max_play_count: return False
    return True

def popularity_score(track: Track) -> float:
    try:
        return float(getattr(track, "ratingCount", 0) or getattr(track, "viewCount", 0) or 0.0)
    except:
        return 0.0

# ---------------------------------------------------------------------------
# SONIC HELPERS
# ---------------------------------------------------------------------------

def get_sonic_similar_albums(album: Album, limit: int) -> List[Album]:
    try:
        return list(album.sonicallySimilar(limit=limit))
    except:
        try:
            rk = getattr(album, "ratingKey", None)
            if not rk: return []
            endpoint = f"/library/metadata/{rk}/nearest?context=sonicallySimilar&limit={limit}"
            return list(album.fetchItems(endpoint))
        except:
            return []

def get_sonic_similar_tracks(track: Track, limit: int) -> List[Track]:
    try:
        related = track.getRelated(hub='sonic', count=limit)
        items = [t for t in related if isinstance(t, Track)]
        if items: return items
    except: pass
    try:
        rk = getattr(track, "ratingKey", None)
        if rk:
            endpoint = f"/library/metadata/{rk}/nearest?context=sonicallySimilar&limit={limit}"
            return list(track.fetchItems(endpoint))
    except: pass
    return []

def get_sonic_similar_artists(artist: Artist, limit: int) -> List[Artist]:
    try:
        rk = getattr(artist, "ratingKey", None)
        if rk:
            endpoint = f"/library/metadata/{rk}/nearest?context=sonicallySimilar&limit={limit}"
            return list(artist.fetchItems(endpoint))
    except: pass
    return []

# ---------------------------------------------------------------------------
# METADATA HELPERS
# ---------------------------------------------------------------------------

def smart_sort_candidates(candidates: List[Track], exploit_weight: float, use_popularity: bool = True, recent_days: int = 0, recent_weight: float = 1.0) -> List[Track]:
    """
    Sorts candidates based on Explore/Exploit AND Recency Bias.
    exploit_weight: 1.0 = Sort by Best (Popularity/Similarity), 0.0 = Random Shuffle.
    recent_weight: Multiplier for score (e.g. 1.5 = 50% boost) if added within recent_days.
    """
    if not candidates: return []
    
    # 0.0 = Pure Shuffle (Short circuit for speed)
    if exploit_weight <= 0.01:
        shuffled = list(candidates)
        random.shuffle(shuffled)
        return shuffled

    scored_items = []
    now = datetime.now()
    
    if use_popularity:
        # Strategy: Popularity (Deep Dive, Genre)
        pop_scores = []
        for t in candidates:
            # Score = Rating (weight 10) + ViewCount (weight 1)
            raw_pop = float(getattr(t, "viewCount", 0) or 0) + (float(getattr(t, "ratingCount", 0) or 0) * 10)
            pop_scores.append(raw_pop)
        
        max_pop = max(pop_scores) if pop_scores else 1.0
        if max_pop == 0: max_pop = 1.0
        
        for i, t in enumerate(candidates):
            quality = pop_scores[i] / max_pop
            
            # --- RECENCY BOOST ---
            if recent_days > 0 and getattr(t, 'addedAt', None):
                try:
                    delta = (now - t.addedAt).days
                    if delta <= recent_days:
                        quality *= recent_weight
                except: pass
            
            # THE FORMULA: Signal (Quality) vs Noise (Random)
            score = (quality * exploit_weight) + (random.random() * (1.0 - exploit_weight))
            scored_items.append((score, t))
            
    else:
        # Strategy: Rank/Similarity (Sonic Modes)
        # We assume the incoming list is ALREADY sorted by similarity (Best = Index 0)
        n = len(candidates)
        for i, t in enumerate(candidates):
            # Convert Index to Score (Index 0 = 1.0, Index Last = 0.0)
            quality = 1.0 - (i / n) if n > 1 else 1.0
            
            # --- RECENCY BOOST ---
            if recent_days > 0 and getattr(t, 'addedAt', None):
                try:
                    delta = (now - t.addedAt).days
                    if delta <= recent_days:
                        quality *= recent_weight
                except: pass

            score = (quality * exploit_weight) + (random.random() * (1.0 - exploit_weight))
            scored_items.append((score, t))

    # Sort Descending (Highest Score First)
    scored_items.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored_items]

def resolve_album(track: Track, plex: PlexServer) -> Optional[Album]:
    ak = getattr(track, "parentRatingKey", None)
    if not ak: return None
    if ak in _ALBUM_CACHE: return _ALBUM_CACHE[ak]
    try:
        album = plex.fetchItem(ak)
        if isinstance(album, Album):
            _ALBUM_CACHE[ak] = album
            return album
    except: pass
    return None

def _album_year(album: Optional[Album]) -> Optional[int]:
    if not album: return None
    try:
        if getattr(album, "originallyAvailableAt", None):
            return album.originallyAvailableAt.year
    except: pass
    try:
        return int(album.year)
    except: return None

def _album_collections_and_genres(album: Optional[Album]) -> Tuple[Set[str], Set[str]]:
    c, g = set(), set()
    if not album: return c, g
    for x in getattr(album, "collections", []):
        name = getattr(x, 'tag', str(x)).strip()
        if name: c.add(name)
    for x in getattr(album, "genres", []):
        name = getattr(x, 'tag', str(x)).strip().lower()
        if name: g.add(name)
    return c, g

def get_track_genres_with_fallback(track: Track) -> Set[str]:
    """
    Returns a set of lowercase genre strings.
    Priority: Track Metadata -> Album Metadata -> Artist Metadata
    """
    # 1. Track Level
    try:
        t_genres = {g.tag.lower() for g in getattr(track, 'genres', [])}
        if t_genres:
            return t_genres
    except: pass

    # 2. Album Level
    try:
        if track.album():
            a_genres = {g.tag.lower() for g in getattr(track.album(), 'genres', [])}
            if a_genres:
                return a_genres
    except: pass

    # 3. Artist Level
    try:
        if track.artist():
            ar_genres = {g.tag.lower() for g in getattr(track.artist(), 'genres', [])}
            if ar_genres:
                return ar_genres
    except: pass

    return set()

def clean_title(title: str) -> str:
    """
    Normalizes a track title to catch fuzzy duplicates.
    Removes: case, punctuation, and common suffixes like 'Remaster', 'Live', 'Feat'.
    """
    if not title: return ""
    
    # 1. Lowercase and strip
    t = title.lower().strip()
    
    # 2. Remove common junk using regex
    # Removes (Live), [Remastered], - Remaster, etc.
    patterns = [
        r"\(.*live.*\)", r"\[.*live.*\]", r"\-.*live.*",
        r"\(.*remaster.*\)", r"\[.*remaster.*\]", r"\-.*remaster.*",
        r"\(.*deluxe.*\)", r"\[.*deluxe.*\]",
        r"\(.*feat.*\)", r"\[.*feat.*\]", r"feat\..*",
        r"\s-\s.*$" # Remove anything after a " - " (often used for subtitles)
    ]
    
    import re
    for pat in patterns:
        t = re.sub(pat, "", t)
        
    # 3. Remove punctuation
    t = re.sub(r"[^\w\s]", "", t)
    
    # 4. Collapse whitespace
    return " ".join(t.split())

# ---------------------------------------------------------------------------
# FILTER LOGIC
# ---------------------------------------------------------------------------

def track_passes_static_filters(
    track: Track, plex: PlexServer, cand_seen: Set[str], excluded_keys: Set[str],
    min_track: int, min_album: int, min_artist: int, allow_unrated: bool,
    min_play_count: Optional[int], max_play_count: Optional[int],
    min_year: Optional[int], max_year: Optional[int],
    min_duration_sec: Optional[int], max_duration_sec: Optional[int],
    include_collections: Set[str], exclude_collections: Set[str], exclude_genres: Set[str],
    reject_reasons: Counter
) -> bool:
    
    rk = getattr(track, "ratingKey", None)
    if not rk: return False
    
    # 1. Global Dedupe & Exclusion Check
    if str(rk) in cand_seen:
        reject_reasons["duplicate"] += 1
        return False
    if str(rk) in excluded_keys:
        reject_reasons["excluded_key"] += 1
        return False

    # 2. Rating Checks
    if not passes_min_ratings(track, plex, min_track, min_album, min_artist, allow_unrated):
        reject_reasons["min_ratings"] += 1
        return False

    # 3. Play Stats
    if not passes_playcount(track, min_play_count, max_play_count):
        reject_reasons["play_count"] += 1
        return False

    # 4. Duration
    dur_ms = getattr(track, "duration", 0)
    if dur_ms:
        ds = int(dur_ms // 1000)
        if min_duration_sec and ds < min_duration_sec:
            reject_reasons["duration"] += 1
            return False
        if max_duration_sec and ds > max_duration_sec:
            reject_reasons["duration"] += 1
            return False

    # 5. Metadata Checks (Album, Track, and Artist Levels)
    album = resolve_album(track, plex)
    
    # A. Year Check
    if min_year > 0 or max_year > 0:
        y = _album_year(album) or getattr(track, 'year', 0) or 0
        if not y:
            reject_reasons["year_missing"] += 1
            return False
        if min_year > 0 and y < min_year:
            reject_reasons["year_too_old"] += 1
            return False
        if max_year > 0 and y > max_year:
            reject_reasons["year_too_new"] += 1
            return False

    # B. Gather Metadata for Inclusion/Exclusion (Lazy Load Artist)
    
    # -- Level 1: Album & Track (Fast) --
    alb_colls, alb_genres = _album_collections_and_genres(album)
    
    try:
        trk_colls = {c.tag.strip() for c in getattr(track, 'collections', [])}
        trk_genres = {g.tag.strip().lower() for g in getattr(track, 'genres', [])}
    except:
        trk_colls, trk_genres = set(), set()

    # -- Level 2: Artist (Cached Fetch) --
    art_colls, art_genres = set(), set()
    
    # Fetch artist if we have ANY collection/genre filters active (Include OR Exclude)
    if exclude_collections or exclude_genres or include_collections:
        ark = getattr(track, "grandparentRatingKey", None)
        if ark:
            if ark in _ARTIST_METADATA_CACHE:
                art_colls, art_genres = _ARTIST_METADATA_CACHE[ark]
            else:
                try:
                    # Fetch Artist object to check its collections/genres
                    artist_obj = plex.fetchItem(ark)
                    art_colls = {c.tag.strip() for c in getattr(artist_obj, 'collections', [])}
                    art_genres = {g.tag.strip().lower() for g in getattr(artist_obj, 'genres', [])}
                    _ARTIST_METADATA_CACHE[ark] = (art_colls, art_genres)
                except:
                    _ARTIST_METADATA_CACHE[ark] = (set(), set())

    # 6. Apply Inclusions (ALL LEVELS: Track OR Album OR Artist)
    # If include_collections is set, the item MUST match at least one level.
    if include_collections:
        if not (alb_colls | trk_colls | art_colls).intersection(include_collections):
            reject_reasons["collections"] += 1
            return False
    
    # 7. Apply Exclusions (ALL LEVELS: Track OR Album OR Artist)
    if exclude_collections:
        if (alb_colls | trk_colls | art_colls).intersection(exclude_collections):
            reject_reasons["collections"] += 1
            return False

    if exclude_genres:
        if (alb_genres | trk_genres | art_genres).intersection(exclude_genres):
            reject_reasons["genre_exclude"] += 1
            return False

    # Track is valid!
    return True

# ---------------------------------------------------------------------------
# SELECTION STRATEGIES (Explore/Exploit)
# ---------------------------------------------------------------------------

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
    # Check Album Filters First
    year_val = _album_year(album)
    if (min_year > 0 or max_year > 0) and year_val:
        if min_year > 0 and year_val < min_year: return None
        if max_year > 0 and year_val > max_year: return None

    colls, genres = _album_collections_and_genres(album)
    if include_collections and not colls.intersection(include_collections): return None
    if exclude_collections and colls.intersection(exclude_collections): return None
    if exclude_genres and genres.intersection(exclude_genres): return None

    try:
        tracks = album.tracks()
    except: return None

    candidates = []
    dummy_counter = Counter()
    for t in tracks:
        # Re-use the main filter logic for consistent behavior
        # Note: We pass empty set for cand_seen because we check exclude_keys separately
        if track_passes_static_filters(
            t, plex, set(), exclude_keys, 
            min_track, min_album, min_artist, allow_unrated,
            min_play_count, max_play_count, 0, 0, # Skip year checks (done at album)
            min_duration_sec, max_duration_sec,
            set(), set(), set(), dummy_counter
        ):
            candidates.append(t)

    if not candidates: return None

    ordered = sorted(candidates, key=popularity_score, reverse=True)
    exploit_weight = max(0.0, min(1.0, exploit_weight))

    if random.random() < exploit_weight:
        # Exploit: Top 30%
        k = max(1, len(ordered) // 3)
        return random.choice(ordered[:k])
    else:
        # Explore: Weighted random towards popular
        idx = int(random.random() ** 2 * (len(ordered) - 1))
        return ordered[idx]

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
    except: return None
    
    if not albums: return None
    random.shuffle(albums)

    for album in albums:
        t = pick_track_from_album(
            album, plex, exploit_weight,
            min_track, min_album, min_artist, allow_unrated,
            exclude_keys, min_play_count, max_play_count,
            min_year, max_year, min_duration_sec, max_duration_sec,
            include_collections, exclude_collections, exclude_genres
        )
        if t: return t
    return None

# ---------------------------------------------------------------------------
# EXPANSION STRATEGIES
# ---------------------------------------------------------------------------

def expand_strict_collection(music_section, collection_names, slider_val, limit):
    all_possible_tracks = []
    for col_name in collection_names:
        try:
            collection = music_section.collection(col_name)
            items = collection.items()
            for item in items:
                if item.type == 'artist':
                    for album in item.albums(): all_possible_tracks.extend(album.tracks())
                elif item.type == 'album':
                    all_possible_tracks.extend(item.tracks())
                elif item.type == 'track':
                    all_possible_tracks.append(item)
        except: pass

    scored_list = []
    now = datetime.now()
    for track in all_possible_tracks:
        age_days = (now - track.addedAt).days
        r_score = max(0, 100 - (age_days * (100 / 180)))
        plays = getattr(track, 'viewCount', 0) or 0
        l_score = min(100, (plays * 5) + ((track.userRating or 0) * 10))
        weight = (r_score * slider_val) + (l_score * (1.0 - slider_val))
        if slider_val > 0.5 and plays == 0: weight += 30
        scored_list.append((track, weight))

    scored_list.sort(key=lambda x: x[1], reverse=True)
    return [t[0] for t in scored_list[:limit]]

def expand_via_sonic_albums(seed_tracks, plex, sonic_limit, exclude_keys, filter_criteria, **kwargs):
    albums = []
    seen = set()
    
    # 1. Resolve Seed Albums
    for t in seed_tracks:
        a = resolve_album(t, plex)
        if a:
            rk = getattr(a, "ratingKey", None)
            if rk and rk not in seen:
                seen.add(rk)
                albums.append(a)

    # 2. Find Sonic Similar Albums (BOOSTED)
    expanded_albums = list(albums)
    for album in albums:
        # BOOST: Fetch 2x the requested limit (min 40) to ensure diversity.
        # This prevents running out of tracks when Artist Caps are strict.
        boosted_limit = max(40, sonic_limit * 2)
        
        for s in get_sonic_similar_albums(album, limit=boosted_limit):
            rk = getattr(s, "ratingKey", None)
            if rk and rk not in seen:
                seen.add(rk)
                expanded_albums.append(s)

    results = []
    dummy_rejects = Counter()

    # 3. EXTRACT NEW VARIABLES
    exploit_weight = float(kwargs.get('exploit_weight', 0.5))
    recent_days = int(kwargs.get('recent_days', 0))
    recent_weight = float(kwargs.get('recent_weight', 1.0))
    
    for album in expanded_albums:
        try:
            tracks = album.tracks()

            # --- SMART SORT: Pick the Best/Newest tracks from this album ---
            tracks = smart_sort_candidates(
                tracks, exploit_weight, use_popularity=True,
                recent_days=recent_days, recent_weight=recent_weight
            )
            # ---------------------------------------------------------------

            count = 0
            for t in tracks:
                if track_passes_static_filters(t, plex, set(), exclude_keys, **filter_criteria, reject_reasons=dummy_rejects):
                    results.append(t)
                    count += 1
                
                # Cap tracks per album to maintain variety
                if count >= 6: break
        except: continue
        
    return results

def expand_via_sonic_artists(seed_artists, plex, sonic_limit, exclude_keys, filter_criteria, **kwargs):
    artists = list(seed_artists)
    seen = {getattr(a, "ratingKey") for a in seed_artists if getattr(a, "ratingKey", None)}

    # 1. Find Sonic Similar Artists (BOOSTED)
    for a in seed_artists:
        # BOOST: Fetch 2x the requested limit (min 40) to ensure we have 
        # enough unique artists to survive strict "Max Tracks Per Artist" caps.
        boosted_limit = max(40, sonic_limit * 2)
        
        for s in get_sonic_similar_artists(a, limit=boosted_limit):
            rk = getattr(s, "ratingKey", None)
            if rk and rk not in seen:
                seen.add(rk)
                artists.append(s)

    results = []
    dummy_rejects = Counter()

    # 2. EXTRACT NEW VARIABLES
    exploit_weight = float(kwargs.get('exploit_weight', 0.5))
    recent_days = int(kwargs.get('recent_days', 0))
    recent_weight = float(kwargs.get('recent_weight', 1.0))
    
    # 3. Harvest Tracks
    for artist in artists:
        try:
            valid = 0
            tracks = artist.tracks()

            tracks = smart_sort_candidates(
                tracks, exploit_weight, use_popularity=True,
                recent_days=recent_days, recent_weight=recent_weight
            )

            for track in tracks:
                if track_passes_static_filters(track, plex, set(), exclude_keys, **filter_criteria, reject_reasons=dummy_rejects):
                    results.append(track)
                    valid += 1
                
                # We cap harvest at 25 per artist to save time. 
                # Since your UI cap is likely lower (e.g. 3-4), this is plenty.
                if valid >= 25: break
        except: continue
        
    return results

def expand_via_sonic_tracks(seed_tracks, plex, sonic_limit, exclude_keys, filter_criteria, **kwargs):
    results = []
    seen = set()
    for t in seed_tracks:
        if t.ratingKey: seen.add(t.ratingKey)

    target_total = int(kwargs.get('max_tracks', 50))
    
    # 1. EXTRACT NEW VARIABLES
    exploit_weight = float(kwargs.get('exploit_weight', 0.5))
    recent_days = int(kwargs.get('recent_days', 0))
    recent_weight = float(kwargs.get('recent_weight', 1.0))

    if len(seed_tracks) > 0:
        matches_per_seed = int((target_total / len(seed_tracks)) + 2)
    else:
        matches_per_seed = 10 
    
    limit_per_seed = min(matches_per_seed, sonic_limit)
    log_detail(f"Expanding via Sonic Tracks. Target: {limit_per_seed}/seed.")

    for seed in seed_tracks:
        sims = []
        try:
            rk = seed.ratingKey
            if rk:
                endpoint = f"/library/metadata/{rk}/nearest?context=sonicallySimilar&limit={sonic_limit}"
                sims = list(seed.fetchItems(endpoint))
        except: pass

        if not sims:
            try:
                related = seed.getRelated(hub='sonic', count=sonic_limit)
                sims = [t for t in related if isinstance(t, Track)]
            except: pass

        # 2. APPLY SMART SORT
        sims = smart_sort_candidates(
            sims, exploit_weight, use_popularity=False, 
            recent_days=recent_days, recent_weight=recent_weight
        )

        count = 0
        dummy_rejects = Counter() 
        for track in sims:
            rk = getattr(track, "ratingKey", None)
            if track_passes_static_filters(track, plex, seen, exclude_keys, **filter_criteria, reject_reasons=dummy_rejects):
                results.append(track)
                if rk: seen.add(str(rk))
                count += 1
            
            if count >= limit_per_seed: 
                break
                
    return results

def expand_album_echoes(seed_tracks, plex, exclude_keys, filter_criteria, **kwargs):
    albums = []
    seen = set()
    for t in seed_tracks:
        a = resolve_album(t, plex)
        if a and a.ratingKey not in seen:
            seen.add(a.ratingKey)
            albums.append(a)
            
    if not albums: return []

    max_tracks = int(kwargs.get('max_tracks', 50))
    
    # 1. EXTRACT NEW VARIABLES
    exploit_weight = float(kwargs.get('exploit_weight', 0.5))
    recent_days = int(kwargs.get('recent_days', 0))
    recent_weight = float(kwargs.get('recent_weight', 1.0))
    
    log_detail(f"Deep Dive (Smart): Scanning {len(albums)} albums...")

    album_pools = {} 
    dummy_rejects = Counter()

    for album in albums:
        try:
            tracks = album.tracks()
            
            # 2. APPLY SMART SORT
            tracks = smart_sort_candidates(
                tracks, exploit_weight, use_popularity=True,
                recent_days=recent_days, recent_weight=recent_weight
            )
            
            unplayed = []
            played = []
            
            for t in tracks:
                if any(s.ratingKey == t.ratingKey for s in seed_tracks):
                    continue
                
                if track_passes_static_filters(t, plex, set(), set(), **filter_criteria, reject_reasons=dummy_rejects):
                    if str(t.ratingKey) not in exclude_keys:
                        unplayed.append(t)
                    else:
                        played.append(t)
            
            if unplayed or played:
                album_pools[album.ratingKey] = unplayed + played
            
        except: pass

    results = []
    active_keys = [a.ratingKey for a in albums if a.ratingKey in album_pools]
    if not active_keys: return []

    base_target = int(max_tracks / len(active_keys)) if active_keys else 0
    
    # Pass 1: Fair Share
    for key in active_keys:
        pool = album_pools[key]
        grab = pool[:base_target]
        results.extend(grab)
        album_pools[key] = pool[base_target:]

    # Pass 2: Backfill
    while len(results) < max_tracks:
        survivors = [k for k in active_keys if album_pools[k]]
        if not survivors:
            log_detail("All valid album tracks exhausted!")
            break
            
        needed = max_tracks - len(results)
        per_survivor = int((needed / len(survivors)) + 1)
        
        for key in survivors:
            if len(results) >= max_tracks: break
            pool = album_pools[key]
            grab = pool[:per_survivor]
            results.extend(grab)
            album_pools[key] = pool[per_survivor:]

    return results


# ---------------------------------------------------------------------------
# SONIC JOURNEY & SMOOTHING
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# SONIC JOURNEY & SMOOTHING
# ---------------------------------------------------------------------------

def find_sonic_path(start_track: Track, end_track: Track, plex: PlexServer, max_depth: int = 5, width: int = 20) -> Optional[List[Track]]:
    """
    Attempts to find a 'Six Degrees' sonic path between two tracks using BFS.
    Returns None if no path is found.
    """
    if start_track.ratingKey == end_track.ratingKey:
        return [start_track]
    
    # Queue: List of paths (each path is a list of Tracks)
    queue = [[start_track]]
    visited = {start_track.ratingKey}
    
    target_key = end_track.ratingKey
    max_nodes = 1300 # Safety brake to prevent infinite API calls
    nodes_visited = 0

    log_detail(f"Pathfinding: {start_track.title} -> {end_track.title} (Max Depth {max_depth}, Width {width})")

    while queue:
        path = queue.pop(0)
        current_node = path[-1]
        
        # Stop if path gets too long
        if len(path) > max_depth + 1:
            continue
            
        # Stop if we've burnt too many API calls
        if nodes_visited > max_nodes:
            log_detail("Pathfinding: Max node limit reached.")
            break

        # Get neighbors
        try:
            neighbors = get_sonic_similar_tracks(current_node, limit=width)
            nodes_visited += 1
        except:
            continue

        for neighbor in neighbors:
            if neighbor.ratingKey == target_key:
                return path + [neighbor]
            
            if neighbor.ratingKey not in visited:
                visited.add(neighbor.ratingKey)
                new_path = list(path)
                new_path.append(neighbor)
                queue.append(new_path)
    
    # Path not found
    return None

def inflate_path(path: List[Track], target_count: int, plex: PlexServer) -> List[Track]:
    """
    Takes a skeletal path and 'fattens' it with neighbors to reach target_count.
    """
    if len(path) >= target_count: 
        return path
    
    # Calculate how many neighbors we need per track in the path
    needed = target_count - len(path)
    # Add a buffer (+2) to ensure we hit the target despite duplicates
    per_node = int(needed / len(path)) + 2
    
    inflated = []
    seen = {t.ratingKey for t in path}
    
    for track in path:
        inflated.append(track)
        
        # Flesh out this waypoint with neighbors
        try:
            neighbors = get_sonic_similar_tracks(track, limit=per_node + 5)
            count = 0
            for n in neighbors:
                if n.ratingKey not in seen:
                    inflated.append(n)
                    seen.add(n.ratingKey)
                    count += 1
                if count >= per_node: 
                    break
        except: pass
        
    return inflated

def expand_sonic_journey(seed_tracks, plex, target_count: int = 50, **kwargs) -> List[Track]:
    """
    Connects seeds with a path, then inflates that path to meet target_count.
    """
    if len(seed_tracks) < 2:
        return seed_tracks

    # Divide the target count by the number of "legs" in the journey
    # e.g. 50 tracks, 2 seeds (1 leg) -> target 50 for the A->B trip
    legs = len(seed_tracks) - 1
    per_leg_target = max(5, int(target_count / legs))

    full_journey = []
    
    for i in range(legs):
        start = seed_tracks[i]
        end = seed_tracks[i+1]
        
        # 1. Try Pathfinding
        path = find_sonic_path(start, end, plex, max_depth=4, width=15)
        
        segment = []
        if path:
            # Path found! Check if it's long enough.
            if len(path) < per_leg_target:
                log_detail(f"Path found ({len(path)} tracks). Inflating to ~{per_leg_target}...")
                segment = inflate_path(path, per_leg_target, plex)
            else:
                segment = path
        else:
            # 2. Fallback Bridge (Inflated by default)
            log_warning(f"⚠️ No path {start.title}->{end.title}. Bridging {per_leg_target} tracks.")
            
            # Grab half from A, half from B
            half = int(per_leg_target / 2) + 2
            bridge_a = get_sonic_similar_tracks(start, limit=half)
            bridge_b = get_sonic_similar_tracks(end, limit=half)
            
            segment = [start] + bridge_a + bridge_b + [end]

        # 3. Stitch it together
        if full_journey:
            # If the last track of journey is the same as first of segment, skip first of segment
            if full_journey[-1].ratingKey == segment[0].ratingKey:
                full_journey.extend(segment[1:])
            else:
                full_journey.extend(segment)
        else:
            full_journey.extend(segment)
            
    return full_journey

def smooth_playlist_gradient(tracks: List[Track], plex: PlexServer) -> List[Track]:
    """
    Reorders tracks to create a sonic gradient, with penalties for repetitive artists.
    Starts with a RANDOM track to ensure fresh journeys on every run.
    """
    if len(tracks) < 3: return tracks
    
    log_status(70, "Smoothing playlist gradient (Anti-Clump Mode)...")
    
    pool = list(tracks)
    
    # --- CHANGE: Pick a RANDOM start track instead of the first one ---
    import random
    start_index = random.randint(0, len(pool) - 1)
    ordered = [pool.pop(start_index)]
    # ------------------------------------------------------------------
    
    while pool:
        current = ordered[-1]
        best_next = None
        best_score = -999 # Allow for negative scores
        
        curr_artist = getattr(current, "grandparentTitle", "") or getattr(current, "originalTitle", "")
        
        try:
            # Ask Plex for neighbors
            sims = get_sonic_similar_tracks(current, limit=50)
            sim_keys = [t.ratingKey for t in sims]
            
            for candidate in pool:
                if candidate.ratingKey in sim_keys:
                    idx = sim_keys.index(candidate.ratingKey)
                    score = 100 - idx
                    
                    # Artist Penalty
                    cand_artist = getattr(candidate, "grandparentTitle", "") or getattr(candidate, "originalTitle", "")
                    if curr_artist and cand_artist and curr_artist == cand_artist:
                        score -= 25 

                    if score > best_score:
                        best_score = score
                        best_next = candidate
                        
        except: pass
        
        if best_next:
            ordered.append(best_next)
            pool.remove(best_next)
        else:
            # Dead end: Pick a random next track to jump out of the rut
            fallback = pool.pop(0)
            ordered.append(fallback)
            
    return ordered

# ---------------------------------------------------------------------------
# DATA HELPERS
# ---------------------------------------------------------------------------

def collect_history_seeds(plex, music_section, period, lookback, exclude, use_periods, min_rate, max_play):
    now = datetime.now()
    h_start = now - timedelta(days=lookback)
    ex_start = now - timedelta(days=exclude)
    
    hours = set(period_hours(period, DEFAULT_PERIODS)) if use_periods else set(range(24))

    hist_entries = [e for e in music_section.history(mindate=h_start) if e.viewedAt and e.viewedAt.hour in hours]
    exclude_entries = [e for e in music_section.history(mindate=ex_start)]
    
    excluded_keys = {str(e.ratingKey) for e in exclude_entries}
    
    seeds = []
    for entry in hist_entries:
        if str(entry.ratingKey) in excluded_keys: continue
        try:
            item = plex.fetchItem(entry.ratingKey)
            if not isinstance(item, Track): continue
            if min_rate > 0:
                if (getattr(item, "userRating", None) or 0) < min_rate: continue
            if max_play is not None:
                if (getattr(item, "viewCount", 0) or 0) > max_play: continue
            seeds.append(item)
        except: pass
        
    return seeds, excluded_keys

def collect_seed_tracks_from_keys(plex, keys):
    seeds = []
    for k in keys:
        try:
            item = plex.fetchItem(f"/library/metadata/{k}") if k.isdigit() else plex.fetchItem(k)
            if isinstance(item, Track): seeds.append(item)
        except: pass
    return seeds

def collect_genre_tracks(music_section, plex: PlexServer, genres: List[str], exclude_keys: Set[str], filter_criteria: dict) -> List[Track]:
    """
    Collect genre seeds with Smart Harvest.
    Priority: Search Tracks -> Search Albums.
    Applies filters (Year, Rating, etc.) immediately.
    """
    if not genres: return []
    tracks = []
    # Local seen set to avoid duplicates within the genre list itself
    seen_keys = set()
    dummy_rejects = Counter()

    for g in genres:
        # 1. Try Track Search
        try:
            # Fetch deep to bypass alphabet bias
            res = music_section.search(libtype='track', genre=g, limit=1000)
            if res:
                random.shuffle(res)
                
                count_for_genre = 0
                for t in res:
                    # --- SMART CHECK ---
                    # Verify the track passes all user filters (Year, Rating, etc.)
                    if track_passes_static_filters(t, plex, seen_keys, exclude_keys, **filter_criteria, reject_reasons=dummy_rejects):
                        tracks.append(t)
                        if t.ratingKey: seen_keys.add(t.ratingKey)
                        count_for_genre += 1
                    
                    # Cap at 100 VALID tracks per genre seed to prevent overloading
                    if count_for_genre >= 100: 
                        break
                continue 
        except: pass

        # 2. Fallback to Album Search
        try:
            res = music_section.searchAlbums(genre=g, limit=500)
            if res:
                random.shuffle(res)
                count_for_genre = 0
                for a in res[:50]: # Look at 50 random albums
                    for t in a.tracks():
                        if count_for_genre >= 50: break
                        
                        if track_passes_static_filters(t, plex, seen_keys, exclude_keys, **filter_criteria, reject_reasons=dummy_rejects):
                            tracks.append(t)
                            if t.ratingKey: seen_keys.add(t.ratingKey)
                            count_for_genre += 1
        except: pass

    return tracks

def collect_seed_artists(music_section, names):
    found = []
    for name in names:
        matches = music_section.search(title=name, libtype='artist')
        exact = next((a for a in matches if a.title.lower() == name.lower()), None)
        if not exact:
            norm = name.replace(" ", "").lower()
            exact = next((a for a in matches if a.title.replace(" ", "").lower() == norm), None)
        if exact: found.append(exact)
    return found

def collect_seed_tracks_from_playlists(plex, music, names):
    seeds = []
    for n in names:
        try:
            pl = next((p for p in plex.playlists() if p.title == n), None)
            if pl:
                for item in pl.items():
                    if isinstance(item, Track): seeds.append(item)
        except: pass
    return seeds

def collect_seed_tracks_from_collections(music, names):
    seeds = []
    for n in names:
        try:
            res = music.search(collection=n)
            for r in res:
                if hasattr(r, 'tracks'): seeds.extend(r.tracks())
        except: pass
    return seeds

def convert_preset_to_payload(flat_cfg: dict) -> dict:
    seed_mode_map = {
        "Auto (infer from seeds/history)": "",
        "Deep Dive (Seed Albums)": "album_echoes",
        "History + Seeds (Union)": "history",
        "Genre seeds": "genre",
        "Sonic Artist Mix": "sonic_artist_mix",
        "Sonic Album Mix": "sonic_album_mix",
        "Sonic Tracks Mix": "track_sonic",
        "Sonic Combo (Albums + Artists)": "sonic_combo",
        "Sonic History (Intersection)": "sonic_history",
        "Strict Collection": "strict_collection",
    }
    
    def _list(k): return [s.strip() for s in (flat_cfg.get(k, "") or "").split(",") if s.strip()]
    def _bool(k): return 1 if flat_cfg.get(k, False) else 0
    def _int(k, d=0): return int(flat_cfg.get(k, d))
    def _float(k, d=0.0): return float(flat_cfg.get(k, d))

    return {
        "plex": {
            "url": os.getenv("PLEX_URL") or os.getenv("PLEX_BASEURL"),
            "token": os.getenv("PLEX_TOKEN"),
            "music_library": flat_cfg.get("pc_lib", "Music"),
        },
        "playlist": {
            "custom_title": flat_cfg.get("pc_custom_title"),
            "preset_name": flat_cfg.get("pc_preset_name"),
            "exclude_played_days": _int("pc_exclude_days", 3),
            "history_lookback_days": _int("pc_lookback_days", 30),
            "max_tracks": _int("pc_max_tracks", 50),
            "sonic_similar_limit": _int("pc_sonic_limit", 20),
            "historical_ratio": _float("pc_hist_ratio", 0.3),
            "exploit_weight": _float("pc_explore_exploit", 0.7),
            "use_time_periods": _bool("pc_use_periods"),
            "min_rating": {
                "track": _int("pc_min_track", 7),
                "album": _int("pc_min_album", 0),
                "artist": _int("pc_min_artist", 0),
            },
            "allow_unrated": _bool("pc_allow_unrated"),
            "min_play_count": _int("pc_min_play_count", -1),
            "max_play_count": _int("pc_max_play_count", -1),
            "min_year": _int("pc_min_year", 0),
            "max_year": _int("pc_max_year", 0),
            "min_duration_sec": _int("pc_min_duration", 0),
            "max_duration_sec": _int("pc_max_duration", 0),
            "recently_added_days": _int("pc_recent_days", 0),
            "recently_added_weight": _float("pc_recent_weight", 0.0),
            "max_tracks_per_artist": _int("pc_max_artist", 0),
            "max_tracks_per_album": _int("pc_max_album", 0),
            "history_min_rating": _int("pc_hist_min_rating", 0),
            "history_max_play_count": _int("pc_hist_max_play_count", -1),
            "seed_mode": seed_mode_map.get(flat_cfg.get("pc_seed_mode_label", ""), "history"),
            "seed_fallback_mode": flat_cfg.get("pc_seed_fallback_mode", "history"),
            "new_vs_legacy_slider": 0.5,
            "genre_strict": _bool("pc_genre_strict"),
            "allow_off_genre_fraction": _float("pc_allow_off_genre", 0.2),
            "seed_track_keys": _list("pc_seed_tracks"),
            "seed_artist_names": _list("pc_seed_artists"),
            "seed_playlist_names": _list("pc_seed_playlists"),
            "seed_collection_names": _list("pc_seed_collections"),
            "genre_seeds": _list("pc_seed_genres"),
            "include_collections": _list("pc_include_collections"),
            "exclude_collections": _list("pc_exclude_collections"),
            "exclude_genres": _list("pc_exclude_genres"),
            "deep_dive_target": _int("pc_deep_dive_target", 15),
        }
    }

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:

    start_time = time.time()
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", type=str, help="Name of preset")
    args, _ = parser.parse_known_args()

    raw_json = {}

    if args.preset:
        preset_name = args.preset.replace(".json", "").strip()
        if os.path.exists("/app/Playlist_Presets"):
            base_folder = "/app/Playlist_Presets"
        else:
            base_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Playlist_Presets")
        
        preset_path = os.path.join(base_folder, f"{preset_name}.json")
        log(f"Loading preset from: {preset_path}")
        try:
            with open(preset_path, "r", encoding="utf-8") as f:
                raw_json = json.load(f)
            if "pc_lib" in raw_json and "playlist" not in raw_json:
                log("ℹ️  Converting UI Preset to Script Payload...")
                raw_json = convert_preset_to_payload(raw_json)
        except Exception as e:
            log(f"❌ ERROR: Could not load preset: {e}")
            return 2
    else:
        try:
            if not sys.stdin.isatty():
                raw_text = sys.stdin.read()
                if raw_text: raw_json = json.loads(raw_text)
        except: pass

    if not raw_json:
        log("❌ ERROR: No input.")
        return 2

    cfg = raw_json
    plex_cfg = cfg.get("plex", {})
    pl_cfg = cfg.get("playlist", {})

    url = plex_cfg.get("url") or os.getenv("PLEX_URL")
    token = plex_cfg.get("token") or os.getenv("PLEX_TOKEN")
    lib_name = plex_cfg.get("music_library", "Music")

    if not (url and token):
        log("❌ ERROR: Credentials missing.")
        return 2

    log_status(0, "Starting Playlist Creator...")
    try:
        plex = PlexServer(url, token, timeout=60)
    except Exception as e:
        log(f"❌ ERROR: Could not connect to Plex: {e}")
        return 3

    music = next((s for s in plex.library.sections() if s.title == lib_name), None)
    if not music:
        log(f"❌ ERROR: Library '{lib_name}' not found.")
        return 3

    max_tracks = int(pl_cfg.get("max_tracks", 50))
    seed_mode = (pl_cfg.get("seed_mode") or "history").lower()
    period = get_current_time_period(DEFAULT_PERIODS) if pl_cfg.get("use_time_periods") else "Anytime"
    exploit_weight = float(pl_cfg.get("exploit_weight", 0.7))
    deep_dive_target = int(pl_cfg.get("deep_dive_target", 15))
    
    # ------------------------------------------------------------------
    # Step 1: Collect Seeds
    # ------------------------------------------------------------------
    log_status(10, f"Mode: {seed_mode} | Period: {period}")
    
    # 1. DEFINE FILTERS EARLY (So we can use them for Smart Harvest)    
    filter_criteria = {
        "min_track": int(pl_cfg.get("min_rating", {}).get("track", 0)),
        "min_album": int(pl_cfg.get("min_rating", {}).get("album", 0)),
        "min_artist": int(pl_cfg.get("min_rating", {}).get("artist", 0)),
        "allow_unrated": bool(pl_cfg.get("allow_unrated", True)),
        "min_play_count": int(pl_cfg.get("min_play_count", -1) or -1) if pl_cfg.get("min_play_count")!=-1 else None,
        "max_play_count": int(pl_cfg.get("max_play_count", -1) or -1) if pl_cfg.get("max_play_count")!=-1 else None,
        "min_year": int(pl_cfg.get("min_year", 0)),
        "max_year": int(pl_cfg.get("max_year", 0)),
        "min_duration_sec": int(pl_cfg.get("min_duration_sec", 0)),
        "max_duration_sec": int(pl_cfg.get("max_duration_sec", 0)),
        "include_collections": {str(x).strip() for x in pl_cfg.get("include_collections", []) if str(x).strip()},
        "exclude_collections": {str(x).strip() for x in pl_cfg.get("exclude_collections", [])},
        "exclude_genres": {str(x).strip().lower() for x in pl_cfg.get("exclude_genres", [])}
    }

    # 2. COLLECT HISTORY FIRST (To get 'excluded_keys' for Smart Harvest)
    lookback = int(pl_cfg.get("history_lookback_days", 30))
    exclude_days = int(pl_cfg.get("exclude_played_days", 3))
    hist_min = int(pl_cfg.get("history_min_rating", 0))
    hist_max = int(pl_cfg.get("history_max_play_count", -1))
    if hist_max == -1: hist_max = None
    
    h_seeds, excluded_keys = collect_history_seeds(
        plex, music, period, lookback, exclude_days, 
        pl_cfg.get("use_time_periods"), hist_min, hist_max
    )

    # 3. NOW COLLECT OTHER SEEDS
    seed_tracks = []
    seen_seed_keys = set() 
    
    # keys, playlist names, collection names...
    keys = pl_cfg.get("seed_track_keys", [])
    if keys:
        found = collect_seed_tracks_from_keys(plex, keys)
        for t in found:
            if t.ratingKey not in seen_seed_keys:
                seed_tracks.append(t)
                seen_seed_keys.add(t.ratingKey)
    
    pl_names = pl_cfg.get("seed_playlist_names", [])
    if pl_names:
        pl_seeds = collect_seed_tracks_from_playlists(plex, music, pl_names)
        for t in pl_seeds:
            if t.ratingKey not in seen_seed_keys:
                seed_tracks.append(t)
                seen_seed_keys.add(t.ratingKey)
            
    c_names = pl_cfg.get("seed_collection_names", [])
    if c_names:
        coll_seeds = collect_seed_tracks_from_collections(music, c_names)
        for t in coll_seeds:
            if t.ratingKey not in seen_seed_keys:
                seed_tracks.append(t)
                seen_seed_keys.add(t.ratingKey)

    # SMART ARTIST SEED SELECTION
    a_names = pl_cfg.get("seed_artist_names", [])
    if isinstance(a_names, str): 
        a_names = [x.strip() for x in a_names.split(",") if x.strip()]
    
    seed_artists = collect_seed_artists(music, a_names)
    
    for artist in seed_artists:
        try:
            target_seeds = deep_dive_target if seed_mode == "album_echoes" else 5
            try: all_albums = list(artist.albums())
            except: continue
            if not all_albums: continue
            random.shuffle(all_albums)
            picked_count = 0
            attempts = 0
            album_idx = 0
            while picked_count < target_seeds and attempts < (target_seeds * 4):
                attempts += 1
                current_album = all_albums[album_idx % len(all_albums)]
                album_idx += 1
                t = pick_track_from_album(
                    current_album, plex, exploit_weight, 
                    filter_criteria["min_track"], filter_criteria["min_album"], filter_criteria["min_artist"], 
                    filter_criteria["allow_unrated"], 
                    seen_seed_keys, 
                    None, None, 0, 0, 0, 0, set(), set(), set()
                )
                if t and t.ratingKey not in seen_seed_keys:
                    seed_tracks.append(t)
                    seen_seed_keys.add(t.ratingKey)
                    picked_count += 1
            if picked_count > 0:
                log_detail(f"Selected {picked_count} seeds from {len(all_albums)} albums for: {artist.title}")
            else:
                fallback = artist.tracks()[:3]
                for t in fallback:
                    if t.ratingKey not in seen_seed_keys:
                        seed_tracks.append(t)
                        seen_seed_keys.add(t.ratingKey)
        except Exception as e:
            log_warning(f"Error picking seeds for {artist.title}: {e}")

    # SMART GENRE SEEDS (UPDATED CALL)
    g_seeds = [str(g).strip() for g in pl_cfg.get("genre_seeds", []) if str(g).strip()]
    if g_seeds:
        # Pass exclude_keys and filter_criteria
        g_tracks = collect_genre_tracks(music, plex, g_seeds, excluded_keys, filter_criteria)
        if seed_mode == "genre": 
            seed_tracks.extend(g_tracks)

    if seed_mode == "history": seed_tracks.extend(h_seeds)

    if not seed_tracks and seed_mode not in ["history", "strict_collection"]:
        fallback = pl_cfg.get("seed_fallback_mode", "history")
        log_warning(f"⚠️ No valid seeds found! Falling back to: {fallback.title()}")
        
        if fallback == "history":
            seed_tracks.extend(h_seeds)
        elif fallback == "genre":
            # Default to "Rock" if no genre seeds provided
            g_seeds = [str(g).strip() for g in pl_cfg.get("genre_seeds", []) if str(g).strip()]
            if not g_seeds: g_seeds = ["Rock"]
            fallback_tracks = collect_genre_tracks(music, plex, g_seeds, excluded_keys, filter_criteria)
            seed_tracks.extend(fallback_tracks)

    unique_seeds = []
    seen = set()
    for t in seed_tracks:
        if t.ratingKey not in seen:
            seen.add(t.ratingKey)
            unique_seeds.append(t)
    seed_tracks = unique_seeds
    
    log_detail(f"Total Seed Tracks: {len(seed_tracks)}")

    # ------------------------------------------------------------------
    # Step 2: Expansion
    # ------------------------------------------------------------------
    log_status(35, "Expanding candidates...")
    
    candidates = []
    inc_cols = {str(x).strip() for x in pl_cfg.get("include_collections", []) if str(x).strip()}
    slider = float(pl_cfg.get("new_vs_legacy_slider", 0.5))
    exploit_weight = float(pl_cfg.get("exploit_weight", 0.7))
    recent_days = int(pl_cfg.get("recently_added_days", 0))
    recent_weight = float(pl_cfg.get("recently_added_weight", 1.0))

    if seed_mode == "strict_collection" and inc_cols:
        strict_res = expand_strict_collection(music, inc_cols, slider, max_tracks*4)
        candidates.extend(strict_res)
        
    # Mode: Sonic History (Intersection) with Smart Backfill
    if seed_mode == "sonic_history":
        log_detail(f"Running Sonic History Intersection (History: {len(h_seeds)} items)...")
        sonic_pool = []
        sonic_limit = int(pl_cfg.get("sonic_similar_limit", 20))
        
        if seed_tracks:
            try:
                expanded = expand_via_sonic_albums(
                    seed_tracks, plex, sonic_limit, excluded_keys, filter_criteria,
                    exploit_weight=exploit_weight, recent_days=recent_days, recent_weight=recent_weight
                )
                sonic_pool.extend(expanded)
            except: pass

        if seed_artists:
            try:
                expanded = expand_via_sonic_artists(
                    seed_artists, plex, sonic_limit, excluded_keys, filter_criteria,
                    exploit_weight=exploit_weight, recent_days=recent_days, recent_weight=recent_weight
                )
                sonic_pool.extend(expanded)
            except: pass
            
        history_key_set = {str(t.ratingKey) for t in h_seeds if t.ratingKey}
        intersection = []
        seen_rks = set()

        for t in sonic_pool:
            rk = str(t.ratingKey) if t.ratingKey else None
            if rk and rk in history_key_set and rk not in seen_rks:
                intersection.append(t)
                seen_rks.add(rk)

        for t in seed_tracks:
            rk = str(t.ratingKey) if t.ratingKey else None
            if rk and rk in history_key_set and rk not in seen_rks:
                intersection.append(t)
                seen_rks.add(rk)
        
        log_detail(f"Sonic Pool: {len(sonic_pool)} -> Intersection: {len(intersection)}")
        candidates.extend(intersection)

        needed = max_tracks - len(candidates)
        if needed > 0:
            log_detail(f"Short by {needed} tracks. Backfilling with 'Discovery' tracks...")
            pool_copy = list(sonic_pool)
            random.shuffle(pool_copy) # SHUFFLE FIX
            
            backfill = []
            for t in pool_copy:
                rk = str(t.ratingKey) if t.ratingKey else None
                if rk and rk not in seen_rks:
                    backfill.append(t)
                    seen_rks.add(rk)
                    if len(backfill) >= needed:
                        break
            candidates.extend(backfill)

    # Mode: Track Sonic
    if seed_mode == "track_sonic" and seed_tracks:
        sonic_limit = int(pl_cfg.get("sonic_similar_limit", 20))
        candidates.extend(expand_via_sonic_tracks(
            seed_tracks, plex, sonic_limit, excluded_keys, 
            filter_criteria, max_tracks=max_tracks, 
            exploit_weight=exploit_weight,
            recent_days=recent_days, recent_weight=recent_weight # <--- ADDED
        ))

    # Mode: Deep Dive (Album Echoes)
    elif seed_mode == "album_echoes" and seed_tracks:
        log_detail(f"Expanding Album Echoes...")
        candidates.extend(expand_album_echoes(
            seed_tracks, plex, excluded_keys, 
            filter_criteria, max_tracks=max_tracks, 
            exploit_weight=exploit_weight,
            recent_days=recent_days, recent_weight=recent_weight # <--- ADDED
        ))

    # Mode: Sonic Journey
    elif seed_mode == "sonic_journey" and seed_tracks:
        log_detail(f" embarking on a Sonic Journey between {len(seed_tracks)} waypoints...")
        candidates.extend(expand_sonic_journey(seed_tracks, plex, target_count=max_tracks))

    # Standard Sonic Modes
    elif "sonic" in seed_mode and seed_tracks:
        sonic_limit = int(pl_cfg.get("sonic_similar_limit", 20))
        
        if "album" in seed_mode or "combo" in seed_mode:
            candidates.extend(expand_via_sonic_albums(
                seed_tracks, plex, sonic_limit, excluded_keys, filter_criteria,
                exploit_weight=exploit_weight, recent_days=recent_days, recent_weight=recent_weight
            ))
            
        if "artist" in seed_mode or "combo" in seed_mode:
            candidates.extend(expand_via_sonic_artists(
                seed_artists, plex, sonic_limit, excluded_keys, filter_criteria,
                exploit_weight=exploit_weight, recent_days=recent_days, recent_weight=recent_weight
            ))
            
    if not candidates and seed_mode in ["history", "genre"]:
        candidates = list(seed_tracks)

    # ------------------------------------------------------------------
    # Step 3: Historical Ratio Mix
    # ------------------------------------------------------------------
    historical_ratio = float(pl_cfg.get("historical_ratio", 0.3))
    if seed_mode not in ["strict_collection", "sonic_history", "history"] and historical_ratio > 0 and h_seeds:
        target_hist = int(max_tracks * historical_ratio)
        shuffled_hist = list(h_seeds)
        random.shuffle(shuffled_hist)
        hist_pick = shuffled_hist[:target_hist]
        candidates.extend(hist_pick)
        log_detail(f"Mixed in {len(hist_pick)} tracks from History.")

    # ------------------------------------------------------------------
    # Step 4: Filter & Shape (Track-First Genre)
    # ------------------------------------------------------------------
    log_status(50, f"Filtering {len(candidates)} candidates...")
    
    seen_ids = set()
    seen_fingerprints = set()
    rejects = Counter()
    
    exc_cols = {str(x).strip() for x in pl_cfg.get("exclude_collections", [])}
    exc_genres = {str(x).strip().lower() for x in pl_cfg.get("exclude_genres", [])}
    
    filt_cols = inc_cols
    if seed_mode == "strict_collection": filt_cols = set()
    
    seed_genre_set = {g.lower() for g in g_seeds}
    genre_strict = bool(pl_cfg.get("genre_strict", 0))
    allow_off_genre_fraction = float(pl_cfg.get("allow_off_genre_fraction", 0.2))
    off_limit = int(max_tracks * allow_off_genre_fraction)
    off_genre_count = 0

    max_tracks_per_artist = int(pl_cfg.get("max_tracks_per_artist", 0) or 0)
    max_tracks_per_album = int(pl_cfg.get("max_tracks_per_album", 0) or 0)
    
    valid_candidates = []
    
    # --- PHASE 1: VALIDATION (All Modes) ---
    for t in candidates:
        # 1. Check Technical Filters (Rating, Year, etc.)
        if not track_passes_static_filters(
            t, plex, seen_ids, excluded_keys,
            **filter_criteria, 
            reject_reasons=rejects
        ):
            continue

        # 2. Check Fuzzy Duplicate (Title + Artist)
        try:
            artist_clean = clean_title(t.grandparentTitle or t.originalTitle or "unknown")
            track_clean = clean_title(t.title)
            fingerprint = f"{artist_clean}_{track_clean}"
            
            if fingerprint in seen_fingerprints:
                rejects["fuzzy_duplicate"] += 1
                continue
                
            if t.ratingKey: seen_ids.add(str(t.ratingKey))
            seen_fingerprints.add(fingerprint)
            
        except: pass

        valid_candidates.append(t)

    # --- PHASE 2: RANKING (Skip for Journey) ---
    # Sonic Journey's order IS the logic. Do not scramble it with Smart Sort.
    if seed_mode != "sonic_journey":
        valid_candidates = smart_sort_candidates(
            valid_candidates, 
            exploit_weight, 
            use_popularity=True, 
            recent_days=recent_days, 
            recent_weight=recent_weight
        )

    # --- PHASE 3: SELECTION (Caps) ---
    final_selection = []
    
    artist_counts = defaultdict(int)
    album_counts = defaultdict(int)
    
    for t in valid_candidates:
        if len(final_selection) >= max_tracks:
            break
            
        try:
            artist_name = t.grandparentTitle or "Unknown"
            album_key = t.parentRatingKey or "Unknown"
        except: continue

        # Check Caps
        if max_tracks_per_artist > 0 and artist_counts[artist_name] >= max_tracks_per_artist:
            continue
        if max_tracks_per_album > 0 and album_counts[album_key] >= max_tracks_per_album:
            continue

        # Check Genre Strictness
        if seed_genre_set:
            candidate_genres = get_track_genres_with_fallback(t)
            on_genre = bool(candidate_genres.intersection(seed_genre_set))
            
            if genre_strict and not on_genre:
                if off_genre_count >= off_limit:
                    continue
                else:
                    off_genre_count += 1

        # Add to Selection
        final_selection.append(t)
        artist_counts[artist_name] += 1
        album_counts[album_key] += 1

    # --- PHASE 4: SMOOTHING (Skip for Journey) ---
    # Journey is already smoothed (A -> B). Random anchor smoothing would break the chain.
    if seed_mode != "sonic_journey" and bool(pl_cfg.get("sonic_smoothing", False)):
        log_detail(f"Smoothing final selection of {len(final_selection)} tracks...")
        final_tracks = smooth_playlist_gradient(final_selection, plex)
    else:
        final_tracks = final_selection

    log_detail(f"Final Playlist Size: {len(final_tracks)}")
    if not final_tracks:
        log("❌ ERROR: 0 tracks remaining after filters.")
        return 5

    # ------------------------------------------------------------------
    # Step 5: Publish to Plex
    # ------------------------------------------------------------------
    log_status(90, "Publishing playlist...")
    
    cust_title = pl_cfg.get("custom_title")
    if cust_title:
        title = cust_title
    else:
        date_str = datetime.now().strftime("%y-%m-%d")
        title = f"Playlist Creator • {seed_mode.title()} ({date_str})"

    desc = f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}. Mode: {seed_mode}. Tracks: {len(final_tracks)}."

    try:
        playlist = next((p for p in plex.playlists() if p.title == title), None)
        if playlist:
            playlist.removeItems(playlist.items())
            playlist.addItems(final_tracks)
            log(f"🔄 Updated existing playlist: {title}")
        else:
            playlist = plex.createPlaylist(title, items=final_tracks)
            log(f"✨ Created new playlist: {title}")

        playlist.edit(summary=desc)

        thumb_file = f"thumb_{playlist.ratingKey}.png"
        create_playlist_thumbnail(title, thumb_file)
        playlist.uploadPoster(filepath=thumb_file)
        if os.path.exists(thumb_file): os.remove(thumb_file)
        
    except Exception as e:
        log(f"❌ ERROR Publishing: {e}")
        return 5

    log_status(100, "Done!")
    return 0

if __name__ == "__main__":
    sys.exit(main())