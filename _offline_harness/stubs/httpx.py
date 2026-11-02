"""Offline stub for ``httpx``.

Only the import surface needed by ``monitor/performance_monitor.py`` is
provided.  Every network operation raises; the harness never calls it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class HTTPError(Exception):
    pass


class ConnectError(HTTPError):
    pass


class TimeoutException(HTTPError):
    pass


class RequestError(HTTPError):
    pass


class Response:
    def __init__(self, status_code: int = 0, text: str = "", json_data: Any = None) -> None:
        self.status_code = status_code
        self.text = text
        self._json = json_data
        self.headers = {}

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"offline stub: HTTP {self.status_code}")


class _NoNetworkClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectError("offline stub: network disabled")

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectError("offline stub: network disabled")

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectError("offline stub: network disabled")

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> "_NoNetworkClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class AsyncClient(_NoNetworkClient):
    pass


class Client(_NoNetworkClient):
    pass


class Limits(SimpleNamespace):
    pass


class Timeout(SimpleNamespace):
    pass
