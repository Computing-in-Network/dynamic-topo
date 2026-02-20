from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class InMemoryRedis:
    """Minimal Redis-like storage used in tests and local fallback."""

    hashes: Dict[str, Dict[str, str]] = field(default_factory=dict)
    streams: Dict[str, List[dict]] = field(default_factory=dict)

    def hset(self, key: str, mapping: Dict[str, str]) -> int:
        current = self.hashes.setdefault(key, {})
        before = len(current)
        current.update(mapping)
        return len(current) - before

    def xadd(self, key: str, fields: Dict[str, str], id: str = "*") -> str:  # noqa: A002
        stream = self.streams.setdefault(key, [])
        stream.append(fields)
        return str(len(stream)) if id == "*" else id


def create_redis_client(url: str = "redis://localhost:6379/0"):
    """Return a Redis client if available, otherwise a local in-memory fallback."""
    try:
        import redis

        client = redis.Redis.from_url(url, decode_responses=True)
        client.ping()
        return client
    except Exception:
        return InMemoryRedis()
