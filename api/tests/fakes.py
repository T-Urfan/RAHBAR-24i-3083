"""Offline stand-ins for Chroma and Redis. No network, no model."""

from __future__ import annotations

from typing import Any


class FakeCollection:
    def __init__(self, name: str, metadata: dict | None = None, rows: int = 0) -> None:
        self.name = name
        self.metadata = metadata
        self.rows = rows

    def count(self) -> int:
        return self.rows

    # write methods exist on the real object; the API must never reach them
    def add(self, **_: Any) -> None:
        raise AssertionError("API wrote to Chroma")

    def modify(self, **_: Any) -> None:
        raise AssertionError("API wrote to Chroma")


class FakeChroma:
    """Mimics chromadb's client: heartbeat, get_collection, and write methods that
    the read-only proxy must hide."""

    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}
        self.down = False
        self.heartbeats = 0

    def heartbeat(self) -> int:
        self.heartbeats += 1
        if self.down:
            raise ConnectionError("simulated: Chroma unreachable")
        return 1

    def get_collection(self, name: str, embedding_function: Any = "default") -> FakeCollection:
        assert embedding_function is None, "the API must pass embedding_function=None"
        if self.down:
            raise ConnectionError("simulated: Chroma unreachable")
        if name not in self.collections:
            raise ValueError(f"Collection {name} does not exist.")
        return self.collections[name]

    def create_collection(self, **_: Any) -> None:
        raise AssertionError("API wrote to Chroma")

    def delete_collection(self, *_: Any, **__: Any) -> None:
        raise AssertionError("API wrote to Chroma")

    # test helper: what the indexer would have produced
    def publish(self, active: str = "rahbar-v1", rows: int = 3,
                embed_model: str = "nomic-embed-text", dimension: int = 768,
                base: str = "rahbar") -> None:
        self.collections[active] = FakeCollection(
            active, {"embed_model": embed_model, "dimension": dimension,
                     "index_version": active.split("-v")[-1]}, rows)
        self.collections[f"{base}-active"] = FakeCollection(
            f"{base}-active", {"active": active, "embed_model": embed_model})


class FakeRedis:
    def __init__(self, up: bool = True) -> None:
        self.up = up

    def ping(self) -> bool:
        if not self.up:
            raise ConnectionError("simulated: Redis unreachable")
        return True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now
