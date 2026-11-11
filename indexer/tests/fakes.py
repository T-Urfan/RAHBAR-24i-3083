"""An in-memory stand-in for the Chroma HTTP client, close enough to the real one
(chromadb-client 1.0.12) for the behaviours store.py relies on:

- create_collection raises if the name exists; get_collection raises if it does not
- modify(metadata=...) replaces the whole metadata dict and rejects "hnsw:space"
- add() with an id that already exists is ignored, so count() can disagree with
  what the caller thinks it wrote (store.activate must catch that)
"""

from __future__ import annotations

from typing import Any


class FakeCollection:
    def __init__(self, name: str, metadata: dict | None = None,
                 configuration: dict | None = None) -> None:
        self.name = name
        self.metadata = dict(metadata) if metadata else None
        self.configuration = configuration or {}
        self.rows: dict[str, tuple[str, list[float], dict]] = {}
        self.fail_on_add_call: int | None = None   # simulate a crash on the Nth add()
        self.add_calls = 0

    def add(self, ids, documents, embeddings, metadatas) -> None:
        self.add_calls += 1
        if self.fail_on_add_call is not None and self.add_calls >= self.fail_on_add_call:
            raise ConnectionError("simulated: Chroma went away mid-write")
        for i, d, e, m in zip(ids, documents, embeddings, metadatas):
            self.rows.setdefault(i, (d, e, m))

    def count(self) -> int:
        return len(self.rows)

    def modify(self, name: str | None = None, metadata: dict | None = None) -> None:
        if metadata is not None:
            if "hnsw:space" in metadata:
                raise ValueError("Changing the distance function of a collection once it "
                                 "is created is not supported currently.")
            self.metadata = dict(metadata)


class FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}
        self.fail_next_create = False

    def create_collection(self, name: str, metadata: dict | None = None,
                          configuration: dict | None = None,
                          embedding_function: Any = "default") -> FakeCollection:
        assert embedding_function is None, "store.py must pass embedding_function=None"
        if name in self.collections:
            raise ValueError(f"Collection {name} already exists")
        col = FakeCollection(name, metadata, configuration)
        if self.fail_next_create:
            col.fail_on_add_call = 1
            self.fail_next_create = False
        self.collections[name] = col
        return col

    def get_collection(self, name: str, embedding_function: Any = "default") -> FakeCollection:
        assert embedding_function is None, "store.py must pass embedding_function=None"
        if name not in self.collections:
            raise ValueError(f"Collection {name} does not exist.")
        return self.collections[name]

    def get_or_create_collection(self, name: str, metadata: dict | None = None,
                                 embedding_function: Any = "default") -> FakeCollection:
        assert embedding_function is None, "store.py must pass embedding_function=None"
        if name not in self.collections:
            self.collections[name] = FakeCollection(name, metadata)
        return self.collections[name]

    def delete_collection(self, name: str) -> None:
        if name not in self.collections:
            raise ValueError("no such collection")
        del self.collections[name]

    def list_collections(self) -> list[FakeCollection]:
        return list(self.collections.values())

    # helpers for assertions
    def pointer(self, base: str = "rahbar") -> dict:
        return self.collections[f"{base}-active"].metadata or {}

    def active(self, base: str = "rahbar") -> FakeCollection:
        return self.collections[self.pointer(base)["active"]]

    def versions(self, base: str = "rahbar") -> list[str]:
        return sorted(n for n in self.collections if n.startswith(f"{base}-v"))
