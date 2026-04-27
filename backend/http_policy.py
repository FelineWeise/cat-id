import asyncio
import random
import time

import httpx


RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _retry_delay(attempt: int, retry_after: float | None = None) -> float:
    if retry_after is not None and retry_after > 0:
        return retry_after
    base = 0.25 * (2 ** attempt)
    jitter = random.uniform(0.0, 0.15)
    return min(2.0, base + jitter)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def get_json_with_policy(
    url: str,
    params: dict,
    timeout: float,
    attempts: int = 3,
) -> dict:
    for attempt in range(attempts):
        try:
            resp = httpx.get(url, params=params, timeout=timeout)
            if resp.status_code in RETRYABLE_STATUSES and attempt < attempts - 1:
                time_to_wait = _retry_delay(attempt, _retry_after_seconds(resp))
                time.sleep(time_to_wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
            time.sleep(_retry_delay(attempt))
    return {}


async def aget_json_with_policy(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    timeout: float,
    attempts: int = 3,
) -> dict:
    for attempt in range(attempts):
        try:
            resp = await client.get(url, params=params, timeout=timeout)
            if resp.status_code in RETRYABLE_STATUSES and attempt < attempts - 1:
                await asyncio.sleep(_retry_delay(attempt, _retry_after_seconds(resp)))
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(_retry_delay(attempt))
    return {}
