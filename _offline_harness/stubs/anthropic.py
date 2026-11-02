"""Offline stub for the ``anthropic`` SDK (dependency injection, NOT a re-implementation).

Purpose
-------
The offline harness must import the project's *real* modules.  Those modules do
``from anthropic import AsyncAnthropic`` at import time, so a stub module that
satisfies the import contract is injected ahead of the project root on
``sys.path``.

The stub intentionally has two behaviours, switched by :func:`set_mode`:

* ``"fail"`` (default, used for every metric) -- every ``messages.create()``
  raises ``RuntimeError("offline: LLM disabled ...")``.  This is the honest
  simulation of "no API key / no network" and makes the project's own
  degradation branches run.
* ``"static"`` -- returns a deterministic, schema-compatible response object.
  It exists only so the harness can prove the switch is real and reversible
  (see the ``llm_stub_control`` governance check); it is never used to produce
  an accuracy number.

No network I/O is performed anywhere in this stub.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

STUB_SDK = True

_OFFLINE_ERROR = "offline: LLM disabled (no API key / no network) enabled by offline harness"

#: mode -> "fail" | "static"
_mode: str = os.getenv("ECHOMIND_OFFLINE_LLM_MODE", "fail").strip().lower() or "fail"
if _mode not in ("fail", "static"):
    _mode = "fail"

#: list of call records, kept for provenance ("LLM was really attempted N times")
_calls: List[Dict[str, Any]] = []


class OfflineLLMUnavailable(RuntimeError):
    """Raised whenever the LLM path is exercised while the harness is offline."""


def set_mode(mode: str) -> str:
    """Switch the stub behaviour; returns the effective mode."""
    global _mode
    normalized = (mode or "").strip().lower()
    if normalized not in ("fail", "static"):
        raise ValueError(f"unknown stub mode: {mode!r}")
    _mode = normalized
    return _mode


def get_mode() -> str:
    return _mode


def reset_counters() -> None:
    _calls.clear()


def snapshot() -> Dict[str, Any]:
    """Counters proving the LLM path was reached and refused."""
    errors = sum(1 for call in _calls if call.get("error"))
    return {
        "mode": _mode,
        "calls": len(_calls),
        "errors": errors,
        "offline_error": _OFFLINE_ERROR,
        "error_type": "RuntimeError",
        "last_error": _calls[-1].get("error") if _calls else None,
    }


def _static_text() -> str:
    return '{"intent": "other", "confidence": 0.0, "reasoning": "offline stub static response"}'


class _Messages:
    """Minimal stand-in for ``AsyncAnthropic().messages``."""

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        record: Dict[str, Any] = {
            "model": kwargs.get("model"),
            "max_tokens": kwargs.get("max_tokens"),
            "temperature": kwargs.get("temperature"),
            "error": None,
        }
        if _mode == "fail":
            record["error"] = _OFFLINE_ERROR
            _calls.append(record)
            raise OfflineLLMUnavailable(_OFFLINE_ERROR)
        _calls.append(record)
        # schema-compatible response for the *stub contract self-test* only
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=_static_text())],
            stop_reason="end_turn",
        )


class AsyncAnthropic:
    """Import-compatible client placeholder.  Holds no credentials, sends nothing."""

    STUB_SDK = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.api_key = kwargs.get("api_key")
        self.base_url = kwargs.get("base_url")
        self._init_kwargs = dict(kwargs)
        self.messages = _Messages()
        # NOTE: deliberately no ``embeddings`` attribute -> real
        # ``IntentRecognizer._embed_text`` falls back to its local n-gram vector,
        # which is exactly the code path the harness measures.


class Anthropic(AsyncAnthropic):
    """Sync client alias (unused by the measured modules)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)


class _Unsupported:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise OfflineLLMUnavailable("offline: anthropic streaming/other resources disabled")


def __getattr__(name: str) -> Any:  # pragma: no cover - defensive import compatibility
    if name.isupper():
        return _Unsupported
    raise AttributeError(f"anthropic stub has no attribute {name!r}")
