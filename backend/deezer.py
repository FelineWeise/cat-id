import asyncio

import httpx

DEEZER_SEARCH = "https://api.deezer.com/search"
DEEZER_TRACK = "https://api.deezer.com/track"
_sem = asyncio.Semaphore(8)
_EMPTY = {"preview": None, "bpm": None}


async def fetch_track_info(client: httpx.AsyncClient, artist: str, track_name: str) -> dict:
    """Search Deezer for a track and return preview URL + BPM.

    Returns dict with keys: preview (str|None), bpm (float|None).
    """
    async with _sem:
        for attempt in range(2):
            try:
                query = f'artist:"{artist}" track:"{track_name}"'
                resp = await client.get(DEEZER_SEARCH, params={"q": query}, timeout=5)
                resp.raise_for_status()
                items = resp.json().get("data", [])
                if not items:
                    return _EMPTY

                preview = items[0].get("preview") or None
                track_id = items[0].get("id")
                bpm = None

                if track_id:
                    detail = await client.get(f"{DEEZER_TRACK}/{track_id}", timeout=5)
                    detail.raise_for_status()
                    bpm = detail.json().get("bpm") or None
                    if bpm is not None and bpm > 0:
                        bpm = float(bpm)
                    else:
                        bpm = None

                return {"preview": preview, "bpm": bpm}
            except httpx.TransportError:
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                return _EMPTY
            except Exception:
                return _EMPTY
