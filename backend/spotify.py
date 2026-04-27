import logging
import math
import re
from functools import lru_cache

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
    return spotipy.Spotify(auth_manager=auth_manager)


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


def search_track(artist: str, track_name: str) -> TrackInfo | None:
    """Search Spotify for a track by artist + name. Returns metadata or None."""
    return _search_track_cached(artist.strip().lower(), track_name.strip().lower())


@lru_cache(maxsize=1024)
def _search_track_cached(artist: str, track_name: str) -> TrackInfo | None:
    sp = get_spotify_client()
    query = f"artist:{artist} track:{track_name}"
    results = sp.search(q=query, type="track", limit=1)
    items = results.get("tracks", {}).get("items", [])
    if not items:
        return None
    return _track_to_info(items[0])


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
    result: dict[str, AudioFeatures | None] = {}
    for i in range(0, len(track_ids), 100):
        batch = track_ids[i : i + 100]
        try:
            features_list = sp.audio_features(batch)
        except spotipy.SpotifyException as exc:
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
    try:
        resp = sp.recommendations(
            seed_tracks=[seed_track_id],
            limit=limit,
            **targets,
        )
    except spotipy.SpotifyException as exc:
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
