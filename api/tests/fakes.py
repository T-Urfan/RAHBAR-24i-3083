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


class QueryableCollection(FakeCollection):
    """A collection that answers query() from canned hits, recording each call."""

    def __init__(self, name: str, hits: list[dict], metadata: dict | None = None) -> None:
        super().__init__(name, metadata, rows=len(hits))
        self.hits = hits
        self.calls: list[dict] = []

    def query(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        where = kwargs.get("where")
        contains = (kwargs.get("where_document") or {}).get("$contains")
        hits = [h for h in self.hits
                if (not where or all(h["meta"].get(k) == v for k, v in where.items()))
                and (contains is None or contains in h["text"])]
        hits = hits[: kwargs["n_results"]]
        return {"ids": [[h["id"] for h in hits]], "documents": [[h["text"] for h in hits]],
                "metadatas": [[h["meta"] for h in hits]],
                "distances": [[h["distance"] for h in hits]]}


class FakeEmbedder:
    def __init__(self, vector: list[float] | None = None, error: Exception | None = None):
        self.vector = vector or [1.0, 0.0, 0.0]
        self.error = error
        self.calls: list[str] = []

    def embed(self, question: str) -> list[float]:
        self.calls.append(question)
        if self.error:
            raise self.error
        return list(self.vector)


class MemoryRedis:
    """Just enough of redis-py for the semantic cache: strings, hashes, lists, TTLs
    recorded (not enforced), scan, unlink and pipelines."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.ttl: dict[str, int] = {}
        self.broken = False

    def _check(self) -> None:
        if self.broken:
            raise ConnectionError("simulated: Redis down")

    def ping(self) -> bool:
        self._check()
        return True

    def get(self, key: str) -> Any:
        self._check()
        return self.data.get(key)

    def set(self, key: str, value: Any) -> None:
        self._check()
        self.data[key] = value.encode() if isinstance(value, str) else value

    def scan_iter(self, match: str = "*", count: int = 0) -> Any:
        import fnmatch
        self._check()
        return [k.encode() for k in list(self.data) if fnmatch.fnmatch(k, match)]

    def unlink(self, *keys: Any) -> int:
        self._check()
        n = 0
        for k in keys:
            n += self.data.pop(k.decode() if isinstance(k, bytes) else k, None) is not None
        return n

    def lrange(self, key: str, start: int, end: int) -> list[bytes]:
        self._check()
        return list(self.data.get(key, []))[start:end + 1]

    def lpush(self, key: str, value: Any) -> None:
        self.data.setdefault(key, []).insert(0, value.encode() if isinstance(value, str)
                                             else value)

    def ltrim(self, key: str, start: int, end: int) -> None:
        self.data[key] = self.data.get(key, [])[start:end + 1]

    def expire(self, key: str, seconds: int) -> None:
        self.ttl[key] = seconds

    def hset(self, key: str, mapping: dict) -> None:
        self.data[key] = {k: (v.encode() if isinstance(v, str) else v)
                          for k, v in mapping.items()}

    def hmget(self, key: str, *fields: str) -> list[Any]:
        row = self.data.get(key, {})
        return [row.get(f) for f in fields]

    def pipeline(self) -> MemoryPipeline:
        self._check()
        return MemoryPipeline(self)


class MemoryPipeline:
    def __init__(self, redis: MemoryRedis) -> None:
        self.redis = redis
        self.ops: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        def queue(*args: Any, **kwargs: Any) -> None:
            self.ops.append((name, args, kwargs))
        return queue

    def execute(self) -> list[Any]:
        self.redis._check()
        return [getattr(self.redis, n)(*a, **k) for n, a, k in self.ops]
