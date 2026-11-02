"""Offline stub for ``chromadb``.

The measured modules do not touch the vector store; this stub only keeps
``import chromadb`` (and the small surface used by
``mcp/knowledge_base.py`` / ``memory/conversation_memory.py``) importable.
Any real operation raises.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

STUB_SDK = True


class _OfflineVectorStore(RuntimeError):
    pass


class Settings(SimpleNamespace):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class Collection:
    def __init__(self, name: str = "offline") -> None:
        self.name = name

    def count(self) -> int:
        return 0

    def add(self, *args: Any, **kwargs: Any) -> None:
        raise _OfflineVectorStore("offline stub: chromadb disabled")

    def upsert(self, *args: Any, **kwargs: Any) -> None:
        raise _OfflineVectorStore("offline stub: chromadb disabled")

    def query(self, *args: Any, **kwargs: Any) -> Dict[str, List[Any]]:
        raise _OfflineVectorStore("offline stub: chromadb disabled")

    def get(self, *args: Any, **kwargs: Any) -> Dict[str, List[Any]]:
        return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}

    def delete(self, *args: Any, **kwargs: Any) -> None:
        return None


class Client:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._collections: Dict[str, Collection] = {}

    def heartbeat(self) -> int:
        return 0

    def get_or_create_collection(self, name: str, **kwargs: Any) -> Collection:
        return self._collections.setdefault(name, Collection(name))

    def get_collection(self, name: str, **kwargs: Any) -> Collection:
        if name not in self._collections:
            raise ValueError(f"Collection {name} does not exist")
        return self._collections[name]

    def delete_collection(self, name: str, **kwargs: Any) -> None:
        self._collections.pop(name, None)

    def reset(self) -> None:
        self._collections.clear()


class PersistentClient(Client):
    def __init__(self, path: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(path=path, **kwargs)


class HttpClient(Client):
    def __init__(self, host: str = "localhost", port: int = 8000, **kwargs: Any) -> None:
        super().__init__(host=host, port=port, **kwargs)


class EphemeralClient(Client):
    pass


def __getattr__(name: str) -> Any:  # pragma: no cover
    raise AttributeError(f"chromadb stub has no attribute {name!r}")
