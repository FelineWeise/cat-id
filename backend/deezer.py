import asyncio
import logging

import httpx
from backend.http_policy import aget_json_with_policy

DEEZER_SEARCH = "https://api.deezer.com/search"
DEEZER_TRACK = "https://api.deezer.com/track"
_sem = asyncio.Semaphore(8)
_EMPTY = {"preview": None, "bpm": None, "isrc": None}
logger = logging.getLogger(__name__)


async def fetch_track_info(client: httpx.AsyncClient, artist: str, track_name: str) -> dict:
    """Search Deezer for a track and return preview URL + BPM.

    Returns dict with keys: preview (str|None), bpm (float|None), isrc (str|None).
    """
    async with _sem:
        try:
            query = f'artist:"{artist}" track:"{track_name}"'
            search_payload = await aget_json_with_policy(
                client,
                DEEZER_SEARCH,
                params={"q": query},
                timeout=5,
                attempts=3,
            )
            items = search_payload.get("data", [])
            if not items:
                return _EMPTY

            preview = items[0].get("preview") or None
            track_id = items[0].get("id")
            bpm = None
            isrc = None
            if track_id:
                detail = await aget_json_with_policy(
                    client,
                    f"{DEEZER_TRACK}/{track_id}",
                    params={},
                    timeout=5,
                    attempts=3,
                )
                bpm = detail.get("bpm") or None
                isrc_raw = detail.get("isrc")
                isrc = str(isrc_raw).strip().upper() if isrc_raw else None
                if bpm is not None and bpm > 0:
                    bpm = float(bpm)
                else:
                    bpm = None

            return {"preview": preview, "bpm": bpm, "isrc": isrc}
        except Exception:
            logger.warning("Deezer lookup failed for '%s - %s'", artist, track_name, exc_info=True)
            return _EMPTY
