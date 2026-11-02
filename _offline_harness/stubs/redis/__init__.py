"""Offline stub for the ``redis`` package.

Only import compatibility is required; the measured modules never talk to
Redis.  Every command raises.
"""

from __future__ import annotations

from typing import Any

STUB_SDK = True


class RedisError(RuntimeError):
    pass


class _OfflineRedis:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._init_kwargs = dict(kwargs)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisError("offline stub: redis disabled")

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisError("offline stub: redis disabled")

    async def ping(self) -> bool:
        raise RedisError("offline stub: redis disabled")

    async def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


Redis = _OfflineRedis


def from_url(*args: Any, **kwargs: Any) -> _OfflineRedis:
    return _OfflineRedis(*args, **kwargs)
