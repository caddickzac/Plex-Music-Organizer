#!/usr/bin/env python3
"""
Playlist Creator — Unified Version (v3.2)
Compatible with: Unraid (Docker) AND Windows/Mac (Laptop)

Usage:
  1. Unraid Scheduler: python playlist_creator.py --preset "Mix Name"
  2. Streamlit/Laptop: Pipe JSON to stdin
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
# THUMBNAIL GENERATOR (Universal Path Fix)
# ---------------------------------------------------------------------------

def create_playlist_thumbnail(title, output_path="thumb.png"):
    size = 1000
    img = Image.new('RGB', (size, size), color='black')
    draw = ImageDraw.Draw(img)
    
    # --- UNIVERSAL FONT LOADER ---
    # Tries Linux (Unraid) paths first, then falls back to Windows/Generic
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
                title_font = ImageFont.truetype(fpath, 80)
                date_font = ImageFont.truetype(fpath, 50)
                break
            except:
                continue

    # 2. Try Windows/Standard Paths
    if title_font is None:
        try:
            # "arial.ttf" usually works on Windows if installed
            title_font = ImageFont.truetype("arial.ttf", 80)
            date_font = ImageFont.truetype("arial.ttf", 50)
        except OSError:
            # 3. Last Resort: Pillow Default (Tiny, but works)
            log_warning("Could not load custom fonts. Using default.")
            title_font = ImageFont.load_default()
            date_font = ImageFont.load_default()

    # Draw Title
    margin = 40
    wrapped_title = textwrap.fill(title, width=15)
    draw.multiline_text((size - margin, margin), wrapped_title, 
                        font=title_font, fill="white", 
                        align="right", anchor="ra", spacing=10)

    # Draw Date
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
        # Track rating
        if min_track > 0:
            tr = getattr(track, "userRating", None)
            if tr is None and not allow_unrated: return False
            if tr is not None and tr < min_track: return False

        # Album rating
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

        # Artist rating
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
    
    # Collections
    for x in getattr(album, "collections", []):
        name = getattr(x, 'tag', str(x)).strip()
        if name: c.add(name)
        
    # Genres
    for x in getattr(album, "genres", []):
        name = getattr(x, 'tag', str(x)).strip().lower()
        if name: g.add(name)
    return c, g

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
    
    if str(rk) in cand_seen:
        reject_reasons["duplicate"] += 1
        return False
    cand_seen.add(str(rk))

    if not passes_min_ratings(track, plex, min_track, min_album, min_artist, allow_unrated):
        reject_reasons["min_ratings"] += 1
        return False

    if not passes_playcount(track, min_play_count, max_play_count):
        reject_reasons["play_count"] += 1
        return False

    # Duration
    dur_ms = getattr(track, "duration", 0)
    if dur_ms:
        ds = int(dur_ms // 1000)
        if min_duration_sec and ds < min_duration_sec:
            reject_reasons["duration"] += 1
            return False
        if max_duration_sec and ds > max_duration_sec:
            reject_reasons["duration"] += 1
            return False

    # Album Checks
    album = resolve_album(track, plex)
    
    # Year
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

    # Collections/Genres
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
# EXPANSION STRATEGIES
# ---------------------------------------------------------------------------

def expand_strict_collection(music_section, collection_names, slider_val, limit):
    all_possible_tracks = []
    for col_name in collection_names:
        try:
            collection = music_section.collection(col_name)
            items = collection.items()
            log_detail(f"Collection '{col_name}' found {len(items)} items.")
            for item in items:
                if item.type == 'artist':
                    for album in item.albums(): all_possible_tracks.extend(album.tracks())
                elif item.type == 'album':
                    all_possible_tracks.extend(item.tracks())
                elif item.type == 'track':
                    all_possible_tracks.append(item)
        except Exception as e:
            log_warning(f"Fetch failed for collection '{col_name}': {e}")

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

def expand_via_sonic_albums(seed_tracks, plex, sonic_limit, exclude_keys, **kwargs):
    # 1. Unique seed albums
    albums = []
    seen = set()
    for t in seed_tracks:
        a = resolve_album(t, plex)
        if a:
            rk = getattr(a, "ratingKey", None)
            if rk and rk not in seen:
                seen.add(rk)
                albums.append(a)

    # 2. Similar albums
    expanded_albums = list(albums)
    for album in albums:
        for s in get_sonic_similar_albums(album, limit=sonic_limit):
            rk = getattr(s, "ratingKey", None)
            if rk and rk not in seen:
                seen.add(rk)
                expanded_albums.append(s)

    # 3. Harvest tracks
    results = []
    for album in expanded_albums:
        try:
            count = 0
            for t in album.tracks():
                rk = getattr(t, "ratingKey", None)
                if rk and str(rk) not in exclude_keys:
                    results.append(t)
                    count += 1
                if count >= 6: break
        except: continue
    return results

def expand_via_sonic_artists(seed_artists, plex, sonic_limit, exclude_keys, **kwargs):
    artists = list(seed_artists)
    seen = {getattr(a, "ratingKey") for a in seed_artists if getattr(a, "ratingKey", None)}

    for a in seed_artists:
        for s in get_sonic_similar_artists(a, limit=sonic_limit):
            rk = getattr(s, "ratingKey", None)
            if rk and rk not in seen:
                seen.add(rk)
                artists.append(s)

    results = []
    for artist in artists:
        try:
            valid = 0
            for track in artist.tracks():
                if str(track.ratingKey) not in exclude_keys:
                    results.append(track)
                    valid += 1
                if valid >= 25: break
        except: continue
    return results

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
            
            # Optional History Filters
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

def collect_genre_tracks(music_section, genres):
    if not genres: return []
    tracks = []
    for g in genres:
        try:
            for album in music_section.searchAlbums(genre=g):
                tracks.extend(album.tracks())
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

def convert_preset_to_payload(flat_cfg: dict) -> dict:
    """Converts flat UI preset to nested script payload."""
    seed_mode_map = {
        "Auto (infer from seeds/history)": "",
        "History only": "history",
        "Genre seeds": "genre",
        "Sonic Album Mix": "sonic_album_mix",
        "Sonic Artist Mix": "sonic_artist_mix",
        "Sonic Combo (Albums + Artists)": "sonic_combo",
        "Album Echoes (seed albums only)": "album_echoes",
        "Sonic Tracks (track-level similarity)": "track_sonic",
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
        }
    }

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    start_time = time.time()
    
    # --- 1. ARGUMENT HANDLING (Universal Preset Path) ---
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", type=str, help="Name of preset (e.g., 'Afro-Cuban')")
    args, _ = parser.parse_known_args()

    raw_json = {}

    if args.preset:
        preset_name = args.preset.replace(".json", "").strip()
        
        # --- UNIVERSAL PATH DETECTION ---
        # If /app exists, we are in Docker (Unraid). Else, look locally (Laptop).
        if os.path.exists("/app/Playlist_Presets"):
            base_folder = "/app/Playlist_Presets"
        else:
            base_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Playlist_Presets")
        
        preset_path = os.path.join(base_folder, f"{preset_name}.json")
        
        log(f"Loading preset from: {preset_path}")
        try:
            with open(preset_path, "r", encoding="utf-8") as f:
                raw_json = json.load(f)
            
            # Convert Flat UI preset to Script Payload if needed
            if "pc_lib" in raw_json and "playlist" not in raw_json:
                log("ℹ️  Converting UI Preset to Script Payload...")
                raw_json = convert_preset_to_payload(raw_json)
                
        except Exception as e:
            log(f"❌ ERROR: Could not load preset: {e}")
            return 2
    else:
        # Try stdin (Streamlit or Pipe)
        try:
            if not sys.stdin.isatty():
                raw_text = sys.stdin.read()
                if raw_text: raw_json = json.loads(raw_text)
        except: pass

    if not raw_json:
        log("❌ ERROR: No input. Use --preset 'Name' or pipe JSON.")
        return 2

    cfg = raw_json
    plex_cfg = cfg.get("plex", {})
    pl_cfg = cfg.get("playlist", {})

    url = plex_cfg.get("url") or os.getenv("PLEX_URL")
    token = plex_cfg.get("token") or os.getenv("PLEX_TOKEN")
    lib_name = plex_cfg.get("music_library", "Music")

    if not (url and token):
        log("❌ ERROR: Credentials missing. Set PLEX_URL/PLEX_TOKEN env vars or config.")
        return 2

    # --- SETUP & LOGGING ---
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

    # Config Vars
    max_tracks = int(pl_cfg.get("max_tracks", 50))
    seed_mode = (pl_cfg.get("seed_mode") or "history").lower()
    period = get_current_time_period(DEFAULT_PERIODS) if pl_cfg.get("use_time_periods") else "Anytime"
    
    # ------------------------------------------------------------------
    # Step 1: Collect Seeds
    # ------------------------------------------------------------------
    log_status(10, f"Mode: {seed_mode} | Period: {period}")
    
    seed_tracks = []
    
    # Track Keys
    seed_tracks.extend(collect_seed_tracks_from_keys(plex, pl_cfg.get("seed_track_keys", [])))
    
    # Playlists
    pl_names = pl_cfg.get("seed_playlist_names", [])
    if pl_names:
        pl_map = {p.title: p for p in plex.playlists()}
        for n in pl_names:
            if n in pl_map: seed_tracks.extend(pl_map[n].items())
            
    # Collections (Seed)
    c_names = pl_cfg.get("seed_collection_names", [])
    if c_names:
        for c in c_names:
            try:
                res = music.search(collection=c)
                for r in res:
                    if hasattr(r, 'tracks'): seed_tracks.extend(r.tracks())
            except: pass

    # Artists
    a_names = pl_cfg.get("seed_artist_names", [])
    # Handle CSV string vs List
    if isinstance(a_names, str): 
        a_names = [x.strip() for x in a_names.split(",") if x.strip()]
    
    seed_artists = collect_seed_artists(music, a_names)
    for a in seed_artists:
        try: seed_tracks.extend(a.tracks()[:5])
        except: pass

    # Genre Seeds
    g_seeds = [str(g).strip() for g in pl_cfg.get("genre_seeds", []) if str(g).strip()]
    g_tracks = collect_genre_tracks(music, g_seeds)
    if seed_mode == "genre": seed_tracks.extend(g_tracks)

    # History Seeds
    lookback = int(pl_cfg.get("history_lookback_days", 30))
    exclude_days = int(pl_cfg.get("exclude_played_days", 3))
    hist_min = int(pl_cfg.get("history_min_rating", 0))
    hist_max = int(pl_cfg.get("history_max_play_count", -1))
    if hist_max == -1: hist_max = None
    
    h_seeds, excluded_keys = collect_history_seeds(
        plex, music, period, lookback, exclude_days, 
        pl_cfg.get("use_time_periods"), hist_min, hist_max
    )
    
    if seed_mode == "history": seed_tracks.extend(h_seeds)

    # Dedupe Seeds
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

    # Mode: Strict Collection
    if seed_mode == "strict_collection" and inc_cols:
        strict_res = expand_strict_collection(music, inc_cols, slider, max_tracks*4)
        candidates.extend(strict_res)
        
    # Mode: Sonic
    sonic_limit = int(pl_cfg.get("sonic_similar_limit", 20))
    if "sonic" in seed_mode and seed_tracks:
        # Albums
        if "album" in seed_mode or "combo" in seed_mode:
            candidates.extend(expand_via_sonic_albums(seed_tracks, plex, sonic_limit, excluded_keys))
        # Artists
        if "artist" in seed_mode or "combo" in seed_mode:
            candidates.extend(expand_via_sonic_artists(seed_artists, plex, sonic_limit, excluded_keys))
            
    # Fallback / Direct
    if not candidates and seed_mode in ["history", "genre"]:
        candidates = list(seed_tracks)

    # ------------------------------------------------------------------
    # Step 3: Filter & Shape
    # ------------------------------------------------------------------
    log_status(50, f"Filtering {len(candidates)} candidates...")
    
    filtered = []
    seen_ids = set()
    rejects = Counter()
    
    exc_cols = {str(x).strip() for x in pl_cfg.get("exclude_collections", [])}
    exc_genres = {str(x).strip().lower() for x in pl_cfg.get("exclude_genres", [])}
    
    # Strict Mode Bypass
    # If strict_collection, we ignore 'include_collections' during filtering 
    # because the expansion step already handled it.
    filt_cols = inc_cols
    if seed_mode == "strict_collection": filt_cols = set()

    # Common Filters
    min_t = int(pl_cfg.get("min_rating", {}).get("track", 0))
    min_al = int(pl_cfg.get("min_rating", {}).get("album", 0))
    min_ar = int(pl_cfg.get("min_rating", {}).get("artist", 0))
    allow_u = bool(pl_cfg.get("allow_unrated", True))
    
    for t in candidates:
        if track_passes_static_filters(
            t, plex, seen_ids, excluded_keys,
            min_t, min_al, min_ar, allow_u,
            int(pl_cfg.get("min_play_count", -1) or -1) if pl_cfg.get("min_play_count")!=-1 else None,
            int(pl_cfg.get("max_play_count", -1) or -1) if pl_cfg.get("max_play_count")!=-1 else None,
            int(pl_cfg.get("min_year", 0)), int(pl_cfg.get("max_year", 0)),
            int(pl_cfg.get("min_duration_sec", 0)), int(pl_cfg.get("max_duration_sec", 0)),
            filt_cols, exc_cols, exc_genres, rejects
        ):
            filtered.append(t)

    # Shuffle & Slice
    final_tracks = filtered
    if len(final_tracks) > max_tracks:
        random.shuffle(final_tracks)
        final_tracks = final_tracks[:max_tracks]

    log_detail(f"Final Playlist Size: {len(final_tracks)}")
    if not final_tracks:
        log("❌ ERROR: 0 tracks remaining after filters.")
        return 5

    # ------------------------------------------------------------------
    # Step 4: Publish to Plex
    # ------------------------------------------------------------------
    log_status(90, "Publishing playlist...")
    
    # Title
    cust_title = pl_cfg.get("custom_title")
    if cust_title:
        title = cust_title
    else:
        date_str = datetime.now().strftime("%y-%m-%d")
        title = f"Playlist Creator • {seed_mode.title()} ({date_str})"

    # Description
    desc = f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}. Mode: {seed_mode}. Tracks: {len(final_tracks)}."

    try:
        # Create/Update
        playlist = next((p for p in plex.playlists() if p.title == title), None)
        if playlist:
            playlist.removeItems(playlist.items())
            playlist.addItems(final_tracks)
            log(f"🔄 Updated existing playlist: {title}")
        else:
            playlist = plex.createPlaylist(title, items=final_tracks)
            log(f"✨ Created new playlist: {title}")

        # Metadata
        playlist.edit(summary=desc)

        # Thumbnail
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