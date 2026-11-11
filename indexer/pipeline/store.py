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

import re
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


_last_micros = 0


def new_index_version(now: datetime | None = None) -> str:
    """'<UTC timestamp to the microsecond>-<6 random hex>'. Versions sort in creation
    order (gc relies on that): the timestamp strictly increases within a process even
    when the clock does not tick between two calls (Windows clocks are coarse). The
    random suffix keeps two processes (a CronJob run and a manual run) from colliding."""
    global _last_micros
    moment = now or datetime.now(timezone.utc)
    micros = max(int(moment.timestamp() * 1_000_000), _last_micros + 1)
    _last_micros = micros
    exact = datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)
    return f"{exact.strftime('%Y%m%dt%H%M%S')}{micros % 1_000_000:06d}-{secrets.token_hex(3)}"


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
        self.metadata: dict[str, str | int | float | bool] = {}
        self.dimension: int | None = None
        self.written = 0
        self._ids: set[str] = set()
        self.create_attempted = False
        # Set the moment the pointer may name this collection. From then on it is (or
        # may be) live, and discard() must never delete it.
        self.activated = False

    def _create(self, dimension: int) -> None:
        self.create_attempted = True
        self.metadata = {
            **self.extra_metadata,
            "embed_model": self.embed_model,      # section 4.4: the pin, read by the API
            "dimension": dimension,
            "index_version": self.index_version,
            "built_at": _utc_iso(),
        }
        self.collection = self.client.create_collection(
            name=self.name,
            metadata=self.metadata,
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
        # Checked here, not left to Chroma: the real client rejects duplicates inside one
        # add() call but its behaviour across calls is version-dependent, and a silently
        # dropped row would mean a chunk that can never be retrieved.
        batch_ids = [c.id for c in chunks]
        duplicates = sorted({i for i in batch_ids if batch_ids.count(i) > 1} |
                            (set(batch_ids) & self._ids))
        if duplicates:
            raise IndexBuildError(f"duplicate chunk ids: {duplicates[:5]}")
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
        self._ids.update(batch_ids)
        self.written += len(chunks)
        return self.written

    def finalize(self, stats: dict[str, str | int | float | bool]) -> None:
        """Record run statistics (amplification, document count, ...) in the new
        collection's metadata before it goes live. The pin fields cannot be overridden.
        Safe because this collection is not active yet and its metadata holds no
        hnsw:space (the distance lives in its configuration)."""
        if self.collection is None:
            raise IndexBuildError("nothing was written; there is no collection to finalize")
        protected = {"embed_model", "dimension", "index_version", "built_at"}
        self.metadata = {**self.metadata,
                         **{k: v for k, v in stats.items() if k not in protected}}
        self.collection.modify(metadata=self.metadata)

    def discard(self) -> None:
        """Delete this unfinished collection after a failed run. Best effort: the next
        successful run's gc removes it anyway.

        Never after activation started: if the pointer update reached the server but the
        reply was lost, or anything after it failed, this collection may be live, and
        deleting it would leave the API pointing at nothing. Deletes by name, so a
        collection whose create call timed out after the server made it is cleaned too."""
        if self.activated or not self.create_attempted:
            return
        try:
            self.client.delete_collection(self.name)
            log("discarded unfinished collection", collection=self.name)
        except Exception as exc:
            log("could not discard unfinished collection", collection=self.name, error=str(exc))
        self.collection = None


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
    build.activated = True                      # before modify: see IndexBuild.discard
    pointer.modify(metadata=metadata)
    log("index activated", active=build.name, previous=previous, count=count,
        embed_model=build.embed_model, dimension=build.dimension)

    keep = {build.name} | ({str(previous)} if previous else set())
    try:
        removed = gc(build.client, base, keep, older_than=build.index_version)
    except Exception as exc:   # housekeeping after activation must never fail the run
        log("gc failed; old collections are left for the next run", error=str(exc))
        removed = ()
    return Activation(active=build.name, previous=str(previous) if previous else None,
                      index_version=build.index_version, count=count, removed=removed)


def _names(collections: Any) -> list[str]:
    # chromadb 1.x returns Collection objects; older clients returned names
    return [c if isinstance(c, str) else c.name for c in collections]


def gc(client: Any, base: str, keep: set[str], older_than: str | None = None) -> tuple[str, ...]:
    """Delete old versions. Runs after activation, so a failure here never affects
    what the API serves; it is logged and the Job still succeeds.

    Only versions older than `older_than` are deleted: a newer version may be another
    run's build still in progress (a manual re-index overlapping the CronJob), and
    deleting it would crash that run. Versions sort by their UTC timestamp.

    Names must match this base's version pattern exactly: a plain prefix test on
    "rahbar-v" would also delete another index called "rahbar-v2"."""
    pattern = re.compile(
        rf"{re.escape(base)}{VERSION_INFIX}(?P<version>\d{{8}}t\d{{12}}-[0-9a-f]{{6}})")
    removed = []
    for name in _names(client.list_collections()):
        match = pattern.fullmatch(name)
        if not match or name in keep:
            continue
        if older_than is None or match.group("version") < older_than:
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
    try:
        pointer = client.get_collection(name=pointer_name(settings.collection),
                                        embedding_function=None)
    except Exception as exc:
        raise IndexBuildError(f"no active index to roll back from ({exc})") from exc
    meta = dict(pointer.metadata or {})
    current, previous = meta.get("active"), meta.get("previous")
    if not previous:
        raise IndexBuildError("no previous index recorded; nothing to roll back to")
    try:
        target = client.get_collection(name=str(previous), embedding_function=None)
    except Exception as exc:
        raise IndexBuildError(f"previous index {previous} no longer exists ({exc})") from exc
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
