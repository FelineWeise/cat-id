"""Resolve external provider links via Odesli/Song.link."""

from __future__ import annotations

import re
import time
from functools import lru_cache

import httpx

from backend.http_policy import aget_json_with_policy

ODESLI_LINKS_BY_SONG = "https://api.song.link/v1-alpha.1/links"
ODESLI_TIMEOUT = 6
ODESLI_ATTEMPTS = 2
ODESLI_COOLDOWN_SECONDS = 120
_odesli_throttled_until = 0.0

PROVIDER_WHITELIST = {
    "youtube",
    "youtubeMusic",
    "deezer",
    "appleMusic",
    "soundcloud",
    "tidal",
}

PROVIDER_ALIAS = {
    "youtubeMusic": "youtube_music",
    "appleMusic": "apple_music",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+", re.I)


def _normalize_part(value: str | None) -> str:
    if not value:
        return ""
    cleaned = _NON_ALNUM.sub(" ", value).strip().lower()
    return re.sub(r"\s+", " ", cleaned)


def build_external_lookup_key(artist: str, title: str, isrc: str | None) -> str:
    artist_n = _normalize_part(artist)
    title_n = _normalize_part(title)
    isrc_n = (isrc or "").strip().upper()
    return f"{artist_n}|{title_n}|{isrc_n}"


def _normalize_provider(provider: str) -> str | None:
    if provider not in PROVIDER_WHITELIST:
        return None
    return PROVIDER_ALIAS.get(provider, provider)


def _extract_provider_links(payload: dict) -> dict[str, str]:
    links_by_platform = payload.get("linksByPlatform")
    if not isinstance(links_by_platform, dict):
        return {}

    resolved: dict[str, str] = {}
    for provider, body in links_by_platform.items():
        normalized = _normalize_provider(provider)
        if not normalized:
            continue
        if not isinstance(body, dict):
            continue
        url = body.get("url")
        if isinstance(url, str) and url.strip():
            resolved[normalized] = url.strip()
    return resolved


def _choose_primary_provider(links: dict[str, str]) -> str | None:
    for candidate in ("soundcloud", "youtube_music", "youtube", "deezer", "apple_music", "tidal"):
        if candidate in links:
            return candidate
    return next(iter(links.keys()), None)


@lru_cache(maxsize=4096)
def _cached_empty(_key: str) -> tuple[dict[str, str], str | None]:
    return {}, None


async def resolve_external_links(
    client: httpx.AsyncClient,
    *,
    artist: str,
    title: str,
    isrc: str | None = None,
    spotify_id: str | None = None,
    deezer_url: str | None = None,
) -> tuple[dict[str, str], str | None]:
    """
    Resolve cross-provider links from Odesli.

    Returns (provider_links, primary_provider). Never raises.
    """
    key = build_external_lookup_key(artist, title, isrc)
    global _odesli_throttled_until
    try:
        if time.monotonic() < _odesli_throttled_until:
            return _cached_empty(key)
        params: dict[str, str] = {}
        if spotify_id and spotify_id.strip():
            params = {
                "platform": "spotify",
                "type": "song",
                "id": spotify_id.strip(),
            }
        elif deezer_url and deezer_url.strip():
            params = {"url": deezer_url.strip()}
        else:
            return _cached_empty(key)

        payload = await aget_json_with_policy(
            client,
            ODESLI_LINKS_BY_SONG,
            params=params,
            timeout=ODESLI_TIMEOUT,
            attempts=ODESLI_ATTEMPTS,
        )
        if not isinstance(payload, dict):
            return _cached_empty(key)
        links = _extract_provider_links(payload)
        if not links:
            return _cached_empty(key)
        return links, _choose_primary_provider(links)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            _odesli_throttled_until = max(
                _odesli_throttled_until,
                time.monotonic() + ODESLI_COOLDOWN_SECONDS,
            )
        return _cached_empty(key)
    except Exception:
        return _cached_empty(key)
