"""Free metadata hints when Spotify text/ISRC mapping fails (MusicBrainz)."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MUSICBRAINZ_RECORDING_SEARCH = "https://musicbrainz.org/ws/2/recording"
MUSICBRAINZ_ISRC = "https://musicbrainz.org/ws/2/isrc"
# MusicBrainz requires a descriptive User-Agent with contact URL.
MB_USER_AGENT = "cat-id/2.1 (+https://github.com/FelineWeise/cat-id)"

_NON_ALNUM = re.compile(r"[^a-z0-9\s]", re.I)


def _sanitize_mb_query_part(value: str) -> str:
    cleaned = _NON_ALNUM.sub(" ", value).strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _primary_artist_name(recording: dict[str, Any]) -> str | None:
    credit = recording.get("artist-credit")
    if not credit or not isinstance(credit, list):
        return None
    for entry in credit:
        if isinstance(entry, dict) and entry.get("name"):
            return str(entry["name"]).strip()
    return None


def _first_isrc_from_recording(recording: dict[str, Any]) -> str | None:
    raw = recording.get("isrcs")
    if not raw:
        return None
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, str) and first.strip():
            return first.strip().upper()
    return None


def _parse_hints_from_recordings(recordings: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None]:
    if not recordings:
        return None, None, None
    rec = recordings[0]
    title = rec.get("title")
    title_s = str(title).strip() if title else None
    artist = _primary_artist_name(rec)
    isrc = _first_isrc_from_recording(rec)
    return isrc, artist, title_s


async def fetch_musicbrainz_hints(
    client: httpx.AsyncClient,
    artist: str,
    track_name: str,
    known_isrc: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Return (isrc, artist, title) hints to retry Spotify resolution, or Nones if unavailable."""
    headers = {"User-Agent": MB_USER_AGENT, "Accept": "application/json"}
    try:
        if known_isrc and known_isrc.strip():
            isrc_clean = known_isrc.strip().upper()
            url = f"{MUSICBRAINZ_ISRC}/{isrc_clean}"
            params: dict[str, str] = {"fmt": "json", "inc": "artist-credits"}
            resp = await client.get(url, params=params, headers=headers, timeout=6.0)
            if resp.status_code != 200:
                return None, None, None
            data = resp.json()
            recordings = data.get("recordings") or []
            isrc, artist, title = _parse_hints_from_recordings(recordings)
            return isrc or isrc_clean, artist, title

        a = _sanitize_mb_query_part(artist)
        t = _sanitize_mb_query_part(track_name)
        if len(a) < 2 or len(t) < 2:
            return None, None, None
        query = f'artist:"{a}" AND recording:"{t}"'
        # Search requests may not support all inc= values; title/artist are enough to retry Spotify.
        params = {"query": query, "fmt": "json", "limit": 5}
        resp = await client.get(MUSICBRAINZ_RECORDING_SEARCH, params=params, headers=headers, timeout=6.0)
        if resp.status_code != 200:
            return None, None, None
        data = resp.json()
        recordings = data.get("recordings") or []
        return _parse_hints_from_recordings(recordings)
    except Exception:
        logger.debug("MusicBrainz hint lookup failed for '%s - %s'", artist, track_name, exc_info=True)
        return None, None, None
