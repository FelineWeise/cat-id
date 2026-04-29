import logging
from typing import Any

import httpx

from backend.config import RAPIDAPI_KEY, RAPIDAPI_SOUNDNET_HOST

logger = logging.getLogger(__name__)

_CACHE: dict[str, dict[str, Any]] = {}


def _cache_key(artist: str, title: str) -> str:
    return f"{artist.strip().lower()}::{title.strip().lower()}"


def _normalize_payload(raw: dict[str, Any]) -> dict[str, Any]:
    numeric_keys = (
        "tempo",
        "energy",
        "danceability",
        "happiness",
        "acousticness",
        "instrumentalness",
        "liveness",
        "speechiness",
        "loudness",
        "duration",
        "popularity",
    )
    normalized: dict[str, Any] = {}
    for key in numeric_keys:
        value = raw.get(key)
        if isinstance(value, (int, float)):
            normalized[key] = float(value)
    for key in ("key", "mode", "camelot"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    return normalized


async def fetch_analysis_metrics(
    *,
    artist: str,
    title: str,
    spotify_id: str | None,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    cache_key = _cache_key(artist, title)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    if not RAPIDAPI_KEY:
        return {}

    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_SOUNDNET_HOST,
    }
    payload: dict[str, Any] = {}
    try:
        if spotify_id:
            resp = await client.get(
                f"https://{RAPIDAPI_SOUNDNET_HOST}/pktx/spotify/{spotify_id}",
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()
        else:
            resp = await client.get(
                f"https://{RAPIDAPI_SOUNDNET_HOST}/pktx/analysis",
                headers=headers,
                params={"song": title, "artist": artist},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        logger.warning("SoundNet analysis fetch failed for %s - %s: %s", artist, title, exc)
        return {}

    normalized = _normalize_payload(payload if isinstance(payload, dict) else {})
    _CACHE[cache_key] = normalized
    return normalized
