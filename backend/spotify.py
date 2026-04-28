import logging
import math
import re
import time
from functools import lru_cache
from difflib import SequenceMatcher
from typing import Literal

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from backend.config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
from backend.models import (
    AUDIO_DIMENSION_KEYS,
    AudioFeatures,
    AudioWeights,
    TrackInfo,
)

logger = logging.getLogger(__name__)

TRACK_URL_PATTERN = re.compile(
    r"(?:https?://)?open\.spotify\.com/track/([a-zA-Z0-9]+)"
)
TRACK_URI_PATTERN = re.compile(r"spotify:track:([a-zA-Z0-9]+)")

TARGET_THRESHOLD = 0.3
SPOTIFY_RATE_LIMIT_COOLDOWN_SECONDS = 180
_spotify_mapping_throttled_until = {"app": 0.0, "user": 0.0}
_spotify_feature_throttled_until = 0.0
MAPPING_RESULT_LIMIT = 5
MAPPING_MIN_SCORE = 0.65
MappingSource = Literal["app", "user"]


def _set_mapping_throttled(
    retry_after_seconds: int | None = None,
    source: MappingSource = "app",
) -> None:
    cooldown = retry_after_seconds or SPOTIFY_RATE_LIMIT_COOLDOWN_SECONDS
    _spotify_mapping_throttled_until[source] = max(
        _spotify_mapping_throttled_until[source],
        time.monotonic() + max(1, cooldown),
    )


def _set_feature_throttled(retry_after_seconds: int | None = None) -> None:
    global _spotify_feature_throttled_until
    cooldown = retry_after_seconds or SPOTIFY_RATE_LIMIT_COOLDOWN_SECONDS
    _spotify_feature_throttled_until = max(
        _spotify_feature_throttled_until,
        time.monotonic() + max(1, cooldown),
    )


def spotify_mapping_allowed(source: MappingSource = "app") -> bool:
    return time.monotonic() >= _spotify_mapping_throttled_until[source]


def spotify_feature_calls_allowed() -> bool:
    return time.monotonic() >= _spotify_feature_throttled_until


def _handle_spotify_rate_limit(
    exc: spotipy.SpotifyException,
    target: str,
    source: MappingSource = "app",
) -> bool:
    if exc.http_status != 429:
        return False
    retry_after = None
    try:
        retry_after = int(exc.headers.get("Retry-After", "0")) if exc.headers else None
    except Exception:
        retry_after = None
    if target == "mapping":
        _set_mapping_throttled(retry_after, source=source)
    else:
        _set_feature_throttled(retry_after)
    logger.warning(
        "Spotify API throttled for %s calls (source=%s); entering cooldown.",
        target,
        source,
    )
    return True


@lru_cache(maxsize=1)
def get_spotify_client() -> spotipy.Spotify:
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise ValueError(
            "Spotify credentials not configured. "
            "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in your .env file."
        )
    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
    )
    return spotipy.Spotify(
        auth_manager=auth_manager,
        retries=0,
        status_retries=0,
        backoff_factor=0,
    )


def extract_track_id(url_or_uri: str) -> str:
    """Extract the Spotify track ID from a URL or URI."""
    url_or_uri = url_or_uri.strip()

    match = TRACK_URL_PATTERN.search(url_or_uri)
    if match:
        return match.group(1)

    match = TRACK_URI_PATTERN.search(url_or_uri)
    if match:
        return match.group(1)

    if re.match(r"^[a-zA-Z0-9]{22}$", url_or_uri):
        return url_or_uri

    raise ValueError(
        f"Could not extract a Spotify track ID from: {url_or_uri}"
    )


def _track_to_info(t: dict) -> TrackInfo:
    album_images = t.get("album", {}).get("images", [])
    return TrackInfo(
        name=t["name"],
        artists=[a["name"] for a in t["artists"]],
        album=t["album"]["name"],
        album_art=album_images[0]["url"] if album_images else None,
        preview_url=t.get("preview_url"),
        spotify_url=t["external_urls"]["spotify"],
        spotify_id=t["id"],
    )


def get_track_info(url_or_uri: str) -> TrackInfo:
    """Fetch basic track metadata from Spotify."""
    sp = get_spotify_client()
    track_id = extract_track_id(url_or_uri)
    return _track_to_info(sp.track(track_id))


def resolve_spotify_track_with_source(
    artist: str,
    track_name: str,
    isrc: str | None = None,
    *,
    user_sp: spotipy.Spotify | None = None,
    spotify_id_hint: str | None = None,
) -> tuple[TrackInfo | None, str | None]:
    """Resolve a catalog track to Spotify and report source."""
    artist_norm = artist.strip().lower()
    track_norm = track_name.strip().lower()
    isrc_norm = isrc.strip().upper() if isrc else ""
    hint_id = spotify_id_hint.strip() if spotify_id_hint else ""

    if user_sp is not None:
        track = _resolve_with_client(
            user_sp,
            source="user",
            artist=artist_norm,
            track_name=track_norm,
            isrc=isrc_norm,
            spotify_id_hint=hint_id,
            market="from_token",
        )
        if track is not None:
            return track, "user_token_search"

    if hint_id:
        hinted = _fetch_track_by_id_cached(hint_id)
        if hinted is not None:
            return hinted, "spotify_id_hint"

    app_track = _resolve_with_cached_app(artist_norm, track_norm, isrc_norm)
    if app_track is not None:
        if isrc_norm and _search_track_by_isrc_cached(isrc_norm) is not None:
            return app_track, "app_isrc_search"
        return app_track, "app_text_search"
    return None, None


def resolve_spotify_track(artist: str, track_name: str, isrc: str | None = None) -> TrackInfo | None:
    """Backward-compatible resolver that returns only TrackInfo."""
    track, _ = resolve_spotify_track_with_source(artist, track_name, isrc)
    return track


def search_track(artist: str, track_name: str, isrc: str | None = None) -> TrackInfo | None:
    """Backward-compatible alias for :func:`resolve_spotify_track`."""
    return resolve_spotify_track(artist, track_name, isrc)


@lru_cache(maxsize=1024)
def _search_track_by_isrc_cached(isrc: str) -> TrackInfo | None:
    if not isrc or not spotify_mapping_allowed("app"):
        return None

    sp = get_spotify_client()
    items = _run_search_with_retry(sp, query=f"isrc:{isrc}", limit=1, source="app")
    if not items:
        return None
    return _track_to_info(items[0])


@lru_cache(maxsize=1024)
def _search_track_cached(artist: str, track_name: str) -> TrackInfo | None:
    if not spotify_mapping_allowed("app"):
        return None

    sp = get_spotify_client()
    return _search_track_uncached(
        sp,
        source="app",
        artist=artist,
        track_name=track_name,
    )


def _resolve_with_cached_app(artist: str, track_name: str, isrc: str) -> TrackInfo | None:
    if isrc:
        by_isrc = _search_track_by_isrc_cached(isrc)
        if by_isrc is not None:
            return by_isrc
    return _search_track_cached(artist, track_name)


def _resolve_with_client(
    sp: spotipy.Spotify,
    *,
    source: MappingSource,
    artist: str,
    track_name: str,
    isrc: str,
    spotify_id_hint: str,
    market: str | None,
) -> TrackInfo | None:
    if not spotify_mapping_allowed(source):
        return None
    if spotify_id_hint:
        hinted = _fetch_track_by_id(sp, spotify_id_hint, source=source)
        if hinted is not None:
            return hinted
    if isrc:
        by_isrc = _search_track_by_isrc(sp, isrc, source=source, market=market)
        if by_isrc is not None:
            return by_isrc
    return _search_track_uncached(
        sp,
        source=source,
        artist=artist,
        track_name=track_name,
        market=market,
    )


def _search_track_by_isrc(
    sp: spotipy.Spotify,
    isrc: str,
    *,
    source: MappingSource,
    market: str | None = None,
) -> TrackInfo | None:
    if not isrc or not spotify_mapping_allowed(source):
        return None
    items = _run_search_with_retry(
        sp,
        query=f"isrc:{isrc}",
        limit=1,
        source=source,
        market=market,
    )
    if not items:
        return None
    return _track_to_info(items[0])


def _search_track_uncached(
    sp: spotipy.Spotify,
    *,
    source: MappingSource,
    artist: str,
    track_name: str,
    market: str | None = None,
) -> TrackInfo | None:
    if not spotify_mapping_allowed(source):
        return None

    normalized_title = _normalize_track_title(track_name)

    passes: list[tuple[str, int]] = [
        (f"artist:{artist} track:{track_name}", 1),
        (f"artist:{artist} track:{normalized_title}", 3),
        (f"{artist} {normalized_title}", MAPPING_RESULT_LIMIT),
    ]

    best_track: TrackInfo | None = None
    best_score = 0.0
    for query, limit in passes:
        items = _run_search_with_retry(
            sp,
            query=query,
            limit=limit,
            source=source,
            market=market,
        )
        if not items:
            continue
        candidate, score = _pick_best_mapping_candidate(items, artist, track_name)
        if candidate and score > best_score:
            best_track = candidate
            best_score = score
        if best_track and best_score >= 0.9:
            break

    if best_track and best_score >= MAPPING_MIN_SCORE:
        return best_track
    return None


def _run_search_with_retry(
    sp: spotipy.Spotify,
    query: str,
    limit: int,
    *,
    source: MappingSource,
    market: str | None = None,
) -> list[dict]:
    if not spotify_mapping_allowed(source):
        return []

    attempts = 2
    for attempt in range(attempts):
        try:
            if market:
                results = sp.search(q=query, type="track", limit=limit, market=market)
            else:
                results = sp.search(q=query, type="track", limit=limit)
            return results.get("tracks", {}).get("items", [])
        except spotipy.SpotifyException as exc:
            if _handle_spotify_rate_limit(exc, target="mapping", source=source):
                if attempt == attempts - 1:
                    return []
                time.sleep(0.25 * (attempt + 1))
                continue
            raise
    return []


@lru_cache(maxsize=2048)
def _fetch_track_by_id_cached(track_id: str) -> TrackInfo | None:
    if not track_id or not spotify_mapping_allowed("app"):
        return None
    sp = get_spotify_client()
    return _fetch_track_by_id(sp, track_id, source="app")


def _fetch_track_by_id(
    sp: spotipy.Spotify,
    track_id: str,
    *,
    source: MappingSource,
) -> TrackInfo | None:
    if not track_id or not spotify_mapping_allowed(source):
        return None
    try:
        return _track_to_info(sp.track(track_id))
    except spotipy.SpotifyException as exc:
        if _handle_spotify_rate_limit(exc, target="mapping", source=source):
            return None
        return None


def _normalize_track_title(value: str) -> str:
    normalized = value.lower()
    normalized = re.sub(r"\([^)]*\)", " ", normalized)
    normalized = re.sub(r"\[[^\]]*\]", " ", normalized)
    normalized = re.sub(r"\b(feat|ft|remix|edit|version)\b.*$", " ", normalized)
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or value.lower().strip()


def _text_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize_track_title(a), _normalize_track_title(b)).ratio()


def _pick_best_mapping_candidate(
    items: list[dict],
    artist: str,
    track_name: str,
) -> tuple[TrackInfo | None, float]:
    best_item: dict | None = None
    best_score = 0.0
    for item in items:
        item_artist_names = [a.get("name", "").lower() for a in item.get("artists", [])]
        artist_match = max((_text_similarity(artist, n) for n in item_artist_names), default=0.0)
        title_match = _text_similarity(track_name, item.get("name", ""))
        score = (artist_match * 0.55) + (title_match * 0.45)
        if score > best_score:
            best_score = score
            best_item = item

    if best_item is None:
        return None, 0.0
    return _track_to_info(best_item), best_score


# ---------------------------------------------------------------------------
# Audio features + recommendations
# ---------------------------------------------------------------------------

def _parse_audio_features(raw: dict | None) -> AudioFeatures | None:
    if not raw or raw.get("error"):
        return None
    return AudioFeatures(
        tempo=raw.get("tempo"),
        energy=raw.get("energy"),
        valence=raw.get("valence"),
        danceability=raw.get("danceability"),
        acousticness=raw.get("acousticness"),
        instrumentalness=raw.get("instrumentalness"),
    )


def get_audio_features(track_ids: list[str]) -> dict[str, AudioFeatures | None]:
    """Batch-fetch audio features. Returns {track_id: AudioFeatures | None}.

    Raises ValueError with a clear message if the endpoint returns 403,
    which typically means the Spotify app lacks extended quota access.
    """
    sp = get_spotify_client()
    if not spotify_feature_calls_allowed():
        return {track_id: None for track_id in track_ids}
    result: dict[str, AudioFeatures | None] = {}
    for i in range(0, len(track_ids), 100):
        batch = track_ids[i : i + 100]
        try:
            features_list = sp.audio_features(batch)
        except spotipy.SpotifyException as exc:
            if _handle_spotify_rate_limit(exc, target="feature"):
                for tid in batch:
                    result[tid] = None
                continue
            if exc.http_status == 403:
                raise ValueError(
                    "Your Spotify app does not have access to the audio-features endpoint. "
                    "This usually means the app needs extended quota mode. "
                    "Check your app settings at https://developer.spotify.com/dashboard"
                ) from exc
            raise
        if features_list is None:
            for tid in batch:
                result[tid] = None
            continue
        for raw in features_list:
            if raw is None:
                continue
            result[raw["id"]] = _parse_audio_features(raw)
    return result


def _normalize_tempo(tempo: float | None) -> float | None:
    """Map BPM to 0-1 range (50–200 BPM → 0–1, clamped)."""
    if tempo is None:
        return None
    return max(0.0, min(1.0, (tempo - 50) / 150))


def _denormalize_tempo(norm: float) -> float:
    return norm * 150 + 50


def build_recommendation_targets(
    seed_features: AudioFeatures,
    weights: AudioWeights,
) -> dict[str, float]:
    """Build target_* kwargs for sp.recommendations().

    Only dimensions whose weight exceeds TARGET_THRESHOLD are included.
    """
    targets: dict[str, float] = {}
    for key in AUDIO_DIMENSION_KEYS:
        w = getattr(weights, key)
        val = getattr(seed_features, key)
        if w > TARGET_THRESHOLD and val is not None:
            targets[f"target_{key}"] = val
    return targets


def get_recommendations(
    seed_track_id: str,
    targets: dict[str, float],
    limit: int = 20,
) -> list[TrackInfo]:
    """Fetch Spotify recommendations seeded by a track with audio feature targets."""
    sp = get_spotify_client()
    if not spotify_feature_calls_allowed():
        return []
    try:
        resp = sp.recommendations(
            seed_tracks=[seed_track_id],
            limit=limit,
            **targets,
        )
    except spotipy.SpotifyException as exc:
        if _handle_spotify_rate_limit(exc, target="feature"):
            return []
        if exc.http_status == 403:
            raise ValueError(
                "Your Spotify app does not have access to the recommendations endpoint. "
                "Check your app settings at https://developer.spotify.com/dashboard"
            ) from exc
        raise
    return [_track_to_info(t) for t in resp.get("tracks", [])]


def compute_similarity(
    seed: AudioFeatures,
    candidate: AudioFeatures,
    weights: AudioWeights,
) -> float:
    """Weighted similarity score (1.0 = identical, 0.0 = maximally different).

    Uses normalized Euclidean distance across active dimensions. Tempo is
    normalized to 0-1 before comparison so it's on the same scale as the
    other 0-1 features.
    """
    total_weight = 0.0
    weighted_sq_diff = 0.0

    for key in AUDIO_DIMENSION_KEYS:
        w = getattr(weights, key)
        if w == 0:
            continue
        s_val = getattr(seed, key)
        c_val = getattr(candidate, key)
        if s_val is None or c_val is None:
            continue

        if key == "tempo":
            s_val = _normalize_tempo(s_val)
            c_val = _normalize_tempo(c_val)

        total_weight += w
        weighted_sq_diff += w * (s_val - c_val) ** 2

    if total_weight == 0:
        return 0.0
    distance = math.sqrt(weighted_sq_diff / total_weight)
    return max(0.0, 1.0 - distance)
