"""Offline stub for ``prometheus_client``.

Import-compatible, metrics are recorded in memory only.  ``start_http_server``
raises because the harness must never open a listener.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

#: name -> list of (labels, op, value)
RECORDS: List[Tuple[str, Dict[str, Any], str, float]] = []


class _Child:
    __slots__ = ("_metric", "_labels")

    def __init__(self, metric: "_Metric", labels: Dict[str, Any]) -> None:
        self._metric = metric
        self._labels = labels

    def inc(self, amount: float = 1.0) -> None:
        RECORDS.append((self._metric.name, dict(self._labels), "inc", float(amount)))

    def dec(self, amount: float = 1.0) -> None:
        RECORDS.append((self._metric.name, dict(self._labels), "dec", float(amount)))

    def set(self, value: float) -> None:
        RECORDS.append((self._metric.name, dict(self._labels), "set", float(value)))

    def observe(self, value: float) -> None:
        RECORDS.append((self._metric.name, dict(self._labels), "observe", float(value)))

    def time(self) -> "_Timer":
        return _Timer(self)

    def labels(self, **kwargs: Any) -> "_Child":
        merged = dict(self._labels)
        merged.update(kwargs)
        return _Child(self._metric, merged)


class _Timer:
    def __init__(self, child: _Child) -> None:
        self._child = child

    def __enter__(self) -> "_Timer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._child.observe(0.0)


class _Metric:
    def __init__(self, name: str, documentation: str = "", labelnames: Any = (), *args: Any, **kwargs: Any) -> None:
        self.name = name
        self.documentation = documentation
        self.labelnames = tuple(labelnames or ())
        self._labels: Dict[str, Any] = {}
        for label in self.labelnames:
            self._labels[label] = ""

    def labels(self, *args: Any, **kwargs: Any) -> _Child:
        merged = dict(self._labels)
        for index, value in enumerate(args):
            if index < len(self.labelnames):
                merged[self.labelnames[index]] = value
        merged.update(kwargs)
        return _Child(self, merged)

    def inc(self, amount: float = 1.0) -> None:
        _Child(self, {}).inc(amount)

    def dec(self, amount: float = 1.0) -> None:
        _Child(self, {}).dec(amount)

    def set(self, value: float) -> None:
        _Child(self, {}).set(value)

    def observe(self, value: float) -> None:
        _Child(self, {}).observe(value)


class Counter(_Metric):
    pass


class Gauge(_Metric):
    pass


class Histogram(_Metric):
    pass


class Summary(_Metric):
    pass


class Info(_Metric):
    pass


class CollectorRegistry:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._metrics: List[_Metric] = []

    def register(self, metric: _Metric) -> None:
        self._metrics.append(metric)


REGISTRY = CollectorRegistry()


def start_http_server(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("offline stub: prometheus HTTP server disabled")


def generate_latest(*args: Any, **kwargs: Any) -> bytes:
    return b""


def values(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return {}
