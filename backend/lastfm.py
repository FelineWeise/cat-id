import asyncio
import logging
import time

import httpx

from backend.config import LASTFM_API_KEY

logger = logging.getLogger(__name__)

LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
_sem = asyncio.Semaphore(4)


def _check_key():
    if not LASTFM_API_KEY:
        raise ValueError(
            "Last.fm API key not configured. "
            "Set LASTFM_API_KEY in your .env file."
        )


def _lastfm_get(params: dict, timeout: int = 15) -> dict:
    params.setdefault("api_key", LASTFM_API_KEY)
    params.setdefault("format", "json")
    params.setdefault("autocorrect", 1)
    for attempt in range(2):
        try:
            resp = httpx.get(LASTFM_BASE, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.TransportError:
            if attempt == 0:
                time.sleep(0.5)
                continue
            raise


async def _lastfm_aget(client: httpx.AsyncClient, params: dict, timeout: int = 10) -> dict:
    params.setdefault("api_key", LASTFM_API_KEY)
    params.setdefault("format", "json")
    params.setdefault("autocorrect", 1)
    for attempt in range(2):
        try:
            resp = await client.get(LASTFM_BASE, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.TransportError:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            raise


def _parse_tags(data: dict, limit: int = 15) -> list[str]:
    """Extract tag names from a Last.fm toptags response."""
    if "error" in data:
        logger.warning("Last.fm tag error: %s", data.get("message", "unknown"))
        return []

    toptags = data.get("toptags", {})
    raw = toptags.get("tag", [])

    if isinstance(raw, dict):
        raw = [raw]

    tags = []
    for t in raw[:limit]:
        name = t.get("name", "").strip().lower()
        if name:
            tags.append(name)
    return tags


def _extract_image(images: list[dict]) -> str | None:
    for img in reversed(images):
        if img.get("#text"):
            return img["#text"]
    return None


def _parse_track_list(raw_tracks: list[dict]) -> list[dict]:
    """Normalize a list of Last.fm track objects into our standard dict format."""
    results = []
    for t in raw_tracks:
        artist_info = t.get("artist")
        artist_name = (
            artist_info.get("name", "") if isinstance(artist_info, dict) else str(artist_info or "")
        )
        results.append({
            "name": t.get("name", ""),
            "artist": artist_name,
            "match": float(t.get("match", 0)),
            "image": _extract_image(t.get("image", [])),
            "url": t.get("url", ""),
        })
    return results


# ---------------------------------------------------------------------------
# Similar tracks (with artist-level fallback)
# ---------------------------------------------------------------------------

def get_similar_tracks(artist: str, track: str, limit: int = 50) -> list[dict]:
    """Find similar tracks via Last.fm.

    Tries track.getSimilar first. When the track is too obscure and returns
    nothing, falls back to artist.getSimilar + top tracks from each similar
    artist to build a candidate pool.
    """
    _check_key()
    data = _lastfm_get({"method": "track.getsimilar", "artist": artist, "track": track, "limit": limit})

    if "error" not in data:
        results = _parse_track_list(data.get("similartracks", {}).get("track", []))
        if results:
            return results

    logger.info("track.getSimilar empty for '%s - %s', falling back to artist similarity", artist, track)
    return _similar_via_artists(artist, limit)


def _similar_via_artists(artist: str, limit: int) -> list[dict]:
    """Build a candidate pool from similar artists' top tracks."""
    try:
        data = _lastfm_get({"method": "artist.getsimilar", "artist": artist, "limit": 20})
    except Exception:
        logger.warning("artist.getSimilar failed for '%s'", artist, exc_info=True)
        return []

    similar_artists = data.get("similarartists", {}).get("artist", [])
    if not similar_artists:
        return []

    tracks_per = max(2, limit // len(similar_artists))
    results: list[dict] = []

    for art in similar_artists:
        art_name = art.get("name", "")
        art_match = float(art.get("match", 0))
        try:
            td = _lastfm_get({"method": "artist.gettoptracks", "artist": art_name, "limit": tracks_per}, timeout=10)
            for t in td.get("toptracks", {}).get("track", [])[:tracks_per]:
                results.append({
                    "name": t.get("name", ""),
                    "artist": art_name,
                    "match": art_match,
                    "image": _extract_image(t.get("image", [])),
                    "url": t.get("url", ""),
                })
        except Exception:
            continue
        if len(results) >= limit:
            break

    return results[:limit]


# ---------------------------------------------------------------------------
# Tags (with artist-level fallback)
# ---------------------------------------------------------------------------

def get_track_tags(artist: str, track: str, limit: int = 20) -> list[str]:
    """Fetch top tags for a track. Falls back to artist tags when the track
    is too obscure for Last.fm to have track-level tags."""
    _check_key()
    try:
        data = _lastfm_get({"method": "track.gettoptags", "artist": artist, "track": track})
        tags = _parse_tags(data, limit)
        if tags:
            logger.info("Track tags for '%s - %s': %s", artist, track, tags[:5])
            return tags
    except Exception:
        logger.warning("Failed to fetch track tags for '%s - %s'", artist, track, exc_info=True)

    logger.info("No track tags for '%s - %s', trying artist tags", artist, track)
    return _get_artist_tags(artist, limit)


def _get_artist_tags(artist: str, limit: int = 15) -> list[str]:
    try:
        data = _lastfm_get({"method": "artist.gettoptags", "artist": artist})
        tags = _parse_tags(data, limit)
        if tags:
            logger.info("Artist tags for '%s': %s", artist, tags[:5])
        return tags
    except Exception:
        logger.warning("Failed to fetch artist tags for '%s'", artist, exc_info=True)
        return []


async def fetch_track_tags(client: httpx.AsyncClient, artist: str, track: str) -> list[str]:
    """Async tag fetch for bulk enrichment. Falls back to artist tags."""
    async with _sem:
        try:
            data = await _lastfm_aget(client, {"method": "track.gettoptags", "artist": artist, "track": track})
            tags = _parse_tags(data)
            if tags:
                await asyncio.sleep(0.05)
                return tags
        except Exception:
            logger.warning("Failed to fetch tags for '%s - %s'", artist, track, exc_info=True)

        try:
            data = await _lastfm_aget(client, {"method": "artist.gettoptags", "artist": artist})
            tags = _parse_tags(data)
            await asyncio.sleep(0.05)
            return tags
        except Exception:
            logger.warning("Failed to fetch artist tags for '%s'", artist, exc_info=True)
            return []
