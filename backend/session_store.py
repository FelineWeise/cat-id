import json
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class SessionStore:
    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        raise NotImplementedError

    def get(self, key: str) -> dict | None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


@dataclass
class _MemoryItem:
    value: dict
    expires_at: float


class MemorySessionStore(SessionStore):
    backend_key = "memory"

    def __init__(self) -> None:
        self._items: dict[str, _MemoryItem] = {}

    def _cleanup(self) -> None:
        now = time.monotonic()
        expired = [k for k, v in self._items.items() if v.expires_at <= now]
        for key in expired:
            self._items.pop(key, None)

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        self._cleanup()
        self._items[key] = _MemoryItem(value=value, expires_at=time.monotonic() + ttl_seconds)

    def get(self, key: str) -> dict | None:
        self._cleanup()
        item = self._items.get(key)
        if item is None:
            return None
        return item.value

    def delete(self, key: str) -> None:
        self._items.pop(key, None)


class RedisSessionStore(SessionStore):
    backend_key = "redis"

    def __init__(self, redis_url: str, key_prefix: str = "catid:sess:") -> None:
        import redis

        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._prefix = key_prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        payload = json.dumps(value)
        self._redis.set(self._k(key), payload, ex=ttl_seconds)

    def get(self, key: str) -> dict | None:
        raw = self._redis.get(self._k(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON payload in Redis session for key %s", key)
            return None

    def delete(self, key: str) -> None:
        self._redis.delete(self._k(key))


def build_session_store(backend: str, redis_url: str) -> SessionStore:
    if backend == "redis":
        try:
            return RedisSessionStore(redis_url)
        except Exception as exc:
            logger.warning("Redis session store unavailable, falling back to memory: %s", exc)
    return MemorySessionStore()
