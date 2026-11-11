"""Stage 5: Store. The only code in the whole system that writes to Chroma.

Safe re-indexing (ADR 0001). The live index is never deleted or modified in place:

1. Every run writes into a brand-new collection, ``<base>-v<index_version>``.
2. Only when that collection holds every chunk does ``activate`` repoint a tiny
   pointer collection, ``<base>-active``, whose metadata names the live collection.
   The API resolves the pointer and reads whatever it names.
3. Old versions are garbage-collected afterwards, keeping the previous one so that
   ``python -m pipeline.run --rollback`` can switch back in one step.

If a run dies half-way, the pointer still names the old, complete collection, so the
API keeps serving it. The pointer is the only thing ever modified, and it is replaced
with a complete metadata dict each time (Chroma's ``modify`` overwrites metadata).

The API resolves the same names: keep POINTER_SUFFIX in step with
api/app/retrieval/chroma.py (api/tests/test_index_contract.py checks it).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import IndexerSettings
from .log import log
from .models import Chunk

POINTER_SUFFIX = "-active"
VERSION_INFIX = "-v"
WRITE_BATCH = 64            # rows per collection.add call: bounds request size and memory
DISTANCE = "cosine"         # the API converts distance to similarity as 1 - distance


class IndexBuildError(RuntimeError):
    """The new collection is not fit to serve; the pointer was not touched."""


def pointer_name(base: str) -> str:
    return f"{base}{POINTER_SUFFIX}"


def version_name(base: str, index_version: str) -> str:
    return f"{base}{VERSION_INFIX}{index_version}"


def new_index_version(now: datetime | None = None) -> str:
    """UTC timestamp plus a random suffix: sortable by time, valid in a Chroma collection
    name, and unique even for two runs in the same clock tick (Windows clocks are coarse
    enough for that to happen, and a CronJob run can overlap a manual one)."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dt%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_client(settings: IndexerSettings) -> Any:
    import chromadb  # the thin client, from chromadb-client in requirements.txt
    from chromadb.config import Settings as ChromaSettings

    return chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port,
                               settings=ChromaSettings(anonymized_telemetry=False))


class IndexBuild:
    """A new, not-yet-active collection. Created on the first ``add``, when the
    vector dimension is known, so the pin (model + dimension) is recorded from
    the vectors actually written rather than from a guess."""

    def __init__(self, client: Any, settings: IndexerSettings, embed_model: str,
                 extra_metadata: dict[str, str | int | float | bool] | None = None) -> None:
        self.client = client
        self.settings = settings
        self.embed_model = embed_model
        self.index_version = new_index_version()
        self.name = version_name(settings.collection, self.index_version)
        self.extra_metadata = dict(extra_metadata or {})
        self.collection: Any = None
        self.dimension: int | None = None
        self.written = 0

    def _create(self, dimension: int) -> None:
        metadata: dict[str, str | int | float | bool] = {
            **self.extra_metadata,
            "embed_model": self.embed_model,      # section 4.4: the pin, read by the API
            "dimension": dimension,
            "index_version": self.index_version,
            "built_at": _utc_iso(),
        }
        self.collection = self.client.create_collection(
            name=self.name,
            metadata=metadata,
            configuration={"hnsw": {"space": DISTANCE}},
            embedding_function=None,   # we always supply our own vectors
        )
        self.dimension = dimension
        log("collection created", collection=self.name, embed_model=self.embed_model,
            dimension=dimension)

    def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        """Write one batch of chunks with their vectors. Returns rows written so far."""
        if len(chunks) != len(vectors):
            raise IndexBuildError(f"{len(chunks)} chunks but {len(vectors)} vectors")
        if not chunks:
            return self.written
        dims = {len(v) for v in vectors}
        if len(dims) != 1 or 0 in dims:
            raise IndexBuildError(f"inconsistent or empty vector dimensions: {sorted(dims)}")
        dimension = dims.pop()
        if self.collection is None:
            self._create(dimension)
        elif dimension != self.dimension:
            raise IndexBuildError(
                f"vector dimension changed mid-build: {self.dimension} then {dimension}")
        for start in range(0, len(chunks), WRITE_BATCH):
            part = chunks[start:start + WRITE_BATCH]
            self.collection.add(
                ids=[c.id for c in part],
                documents=[c.text for c in part],
                embeddings=vectors[start:start + WRITE_BATCH],
                metadatas=[c.metadata() for c in part],
            )
        self.written += len(chunks)
        return self.written


@dataclass(frozen=True)
class Activation:
    active: str
    previous: str | None
    index_version: str
    count: int
    removed: tuple[str, ...]


def activate(build: IndexBuild) -> Activation:
    """Verify the new collection, then repoint the API at it. Nothing the API can
    see changes until the single ``modify`` call on the pointer."""
    if build.collection is None or build.written == 0:
        raise IndexBuildError("refusing to activate an empty index")
    count = int(build.collection.count())
    if count != build.written:
        raise IndexBuildError(
            f"collection {build.name} holds {count} rows but {build.written} were written "
            "(duplicate chunk ids?)")

    base = build.settings.collection
    pointer = build.client.get_or_create_collection(
        name=pointer_name(base), embedding_function=None)
    previous = (pointer.metadata or {}).get("active")
    metadata: dict[str, str | int | float | bool] = {
        "active": build.name,
        "index_version": build.index_version,
        "embed_model": build.embed_model,
        "dimension": int(build.dimension or 0),
        "count": count,
        "activated_at": _utc_iso(),
    }
    if previous and previous != build.name:
        metadata["previous"] = str(previous)    # Chroma metadata cannot hold None
    pointer.modify(metadata=metadata)
    log("index activated", active=build.name, previous=previous, count=count,
        embed_model=build.embed_model, dimension=build.dimension)

    keep = {build.name} | ({str(previous)} if previous else set())
    removed = gc(build.client, base, keep)
    return Activation(active=build.name, previous=str(previous) if previous else None,
                      index_version=build.index_version, count=count, removed=removed)


def _names(collections: Any) -> list[str]:
    # chromadb 1.x returns Collection objects; older clients returned names
    return [c if isinstance(c, str) else c.name for c in collections]


def gc(client: Any, base: str, keep: set[str]) -> tuple[str, ...]:
    """Delete old versions. Runs after activation, so a failure here never affects
    what the API serves; it is logged and the Job still succeeds."""
    prefix = f"{base}{VERSION_INFIX}"
    removed = []
    for name in _names(client.list_collections()):
        if name.startswith(prefix) and name not in keep:
            try:
                client.delete_collection(name)
                removed.append(name)
            except Exception as exc:  # never fail the Job over housekeeping
                log("gc could not delete collection", collection=name, error=str(exc))
    if removed:
        log("gc removed old collections", removed=removed)
    return tuple(removed)


def rollback(client: Any, settings: IndexerSettings) -> Activation:
    """Swap the pointer back to the previous collection (and keep the current one as
    the new 'previous', so a rollback can itself be undone)."""
    pointer = client.get_collection(name=pointer_name(settings.collection),
                                    embedding_function=None)
    meta = dict(pointer.metadata or {})
    current, previous = meta.get("active"), meta.get("previous")
    if not previous:
        raise IndexBuildError("no previous index recorded; nothing to roll back to")
    target = client.get_collection(name=str(previous), embedding_function=None)
    count = int(target.count())
    if count == 0:
        raise IndexBuildError(f"previous index {previous} is empty; refusing to roll back")
    tmeta = dict(target.metadata or {})
    metadata: dict[str, str | int | float | bool] = {
        "active": str(previous),
        "index_version": str(tmeta.get("index_version", "unknown")),
        "embed_model": str(tmeta.get("embed_model", "unknown")),
        "dimension": int(tmeta.get("dimension", 0)),
        "count": count,
        "activated_at": _utc_iso(),
    }
    if current:
        metadata["previous"] = str(current)
    pointer.modify(metadata=metadata)
    log("index rolled back", active=previous, previous=current, count=count)
    return Activation(active=str(previous), previous=str(current) if current else None,
                      index_version=str(metadata["index_version"]), count=count, removed=())


def store(chunks: list[Chunk], vectors: list[list[float]], settings: IndexerSettings,
          client: Any = None) -> int:
    """Write every chunk with its vector and metadata into a new collection, record the
    embedding model and dimension in its metadata (section 4.4), then activate it.
    Returns the number of chunks now being served."""
    build = IndexBuild(client if client is not None else get_client(settings), settings,
                       embed_model=settings.embed_model)
    build.add(chunks, vectors)
    return activate(build).count
