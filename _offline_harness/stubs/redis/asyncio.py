"""Offline stub for ``redis.asyncio``."""

from __future__ import annotations

from typing import Any

from . import _OfflineRedis, RedisError

STUB_SDK = True

Redis = _OfflineRedis


def from_url(*args: Any, **kwargs: Any) -> _OfflineRedis:
    return _OfflineRedis(*args, **kwargs)


__all__ = ["Redis", "RedisError", "from_url"]
