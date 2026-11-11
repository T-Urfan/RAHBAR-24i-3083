"""The real retriever (sections 3.2 and 4.4).

Three rules shape this file:

1. Lazy connection. chromadb's HttpClient contacts the server inside its
   constructor and raises if Chroma is down. Building it at API startup would turn
   "Chroma is not up yet" into a crashed API. So the client is built on first use,
   and thrown away after a connection error so the next call can retry. While
   Chroma is unreachable /healthz still answers and /readyz says why it is not ready.

2. Read-only by construction. The API only ever reads the index (section 4.2). The
   client is wrapped in a proxy that exposes read methods only; calling add, upsert,
   delete, modify or create_collection raises PermissionError. A rule in a README
   can be broken; this cannot.

3. Follow the pointer. The indexer writes each index into a new collection and then
   repoints ``<base>-active`` at it (indexer/pipeline/store.py). The API reads the
   pointer to find the live collection, caching the answer for a few seconds.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..schemas import Passage
from ..services.embedding import QueryEmbedder

# Must match indexer/pipeline/store.py (checked by api/tests/test_index_contract.py).
POINTER_SUFFIX = "-active"


def pointer_name(base: str) -> str:
    return f"{base}{POINTER_SUFFIX}"


class ReadOnlyCollection:
    """A Chroma collection with only its read side visible."""

    ALLOWED = frozenset({"name", "id", "metadata", "count", "query", "get", "peek"})

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, item: str) -> Any:
        if item not in self.ALLOWED:
            raise PermissionError(
                f"the API is read-only for Chroma: collection.{item} is not allowed "
                "(only the indexer Job writes the index)")
        return getattr(self._inner, item)


class ReadOnlyClient:
    """A Chroma client with only its read side visible."""

    ALLOWED = frozenset({"heartbeat", "get_collection", "list_collections"})

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, item: str) -> Any:
        if item not in self.ALLOWED:
            raise PermissionError(
                f"the API is read-only for Chroma: client.{item} is not allowed "
                "(only the indexer Job writes the index)")
        return getattr(self._inner, item)

    def get_collection(self, name: str) -> ReadOnlyCollection:
        # embedding_function=None: we embed questions ourselves with the pinned model,
        # and the thin client must not try to load Chroma's default ONNX model.
        return ReadOnlyCollection(self._inner.get_collection(name=name, embedding_function=None))


def _http_client(settings: Settings) -> Any:
    import chromadb  # the thin client, from chromadb-client in requirements.txt
    from chromadb.config import Settings as ChromaSettings

    return chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port,
                               settings=ChromaSettings(anonymized_telemetry=False))


@dataclass(frozen=True)
class IndexStatus:
    """What readiness needs to know about the live index."""

    reachable: bool
    active: str | None = None          # name of the live collection, from the pointer
    count: int = 0
    embed_model: str | None = None     # read from the live collection's own metadata
    dimension: int | None = None
    index_version: str | None = None
    error: str | None = None


class ChromaRetriever:
    name = "chroma"

    def __init__(self, settings: Settings,
                 client_factory: Callable[[Settings], Any] = _http_client,
                 clock: Callable[[], float] = time.monotonic,
                 embedder: Any | None = None) -> None:
        self.settings = settings
        # Builds an httpx client but opens no connection.
        self.embedder = embedder if embedder is not None else QueryEmbedder(settings)
        self._factory = client_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._client: ReadOnlyClient | None = None
        self._active: tuple[str, float] | None = None   # (collection name, resolved at)

    # -- connection -------------------------------------------------------------------
    def _get_client(self) -> ReadOnlyClient:
        with self._lock:
            if self._client is None:
                self._client = ReadOnlyClient(self._factory(self.settings))
            return self._client

    def _reset(self) -> None:
        with self._lock:
            self._client = None
            self._active = None

    # -- pointer ----------------------------------------------------------------------
    def _resolve_active(self, client: ReadOnlyClient, force: bool = False) -> str:
        now = self._clock()
        with self._lock:
            cached = self._active
        if not force and cached and now - cached[1] < self.settings.pointer_ttl_seconds:
            return cached[0]
        pointer = client.get_collection(pointer_name(self.settings.collection))
        active = (pointer.metadata or {}).get("active")
        if not active:
            raise LookupError(f"pointer {pointer_name(self.settings.collection)} names no index")
        with self._lock:
            self._active = (str(active), now)
        return str(active)

    def active_collection(self, force: bool = False) -> ReadOnlyCollection:
        """The live collection. Re-resolves the pointer once if the cached name has
        gone (the indexer garbage-collected it after a newer index went live)."""
        client = self._get_client()
        try:
            return client.get_collection(self._resolve_active(client, force=force))
        except Exception:
            if force:
                raise
            return client.get_collection(self._resolve_active(client, force=True))

    def generation(self) -> str:
        """The live collection's name: it changes on every re-index, so the semantic
        cache keys on it (services/cache.py)."""
        return self._resolve_active(self._get_client())

    # -- readiness --------------------------------------------------------------------
    def describe(self) -> IndexStatus:
        """Never raises: readiness turns the answer into 200 or 503."""
        try:
            client = self._get_client()
            client.heartbeat()
        except Exception as exc:
            self._reset()          # the next call builds a fresh client
            return IndexStatus(reachable=False, error=f"{type(exc).__name__}: {exc}")
        try:
            collection = self.active_collection(force=True)
            meta = dict(collection.metadata or {})
            dimension = meta.get("dimension")
            return IndexStatus(
                reachable=True,
                active=str(collection.name),
                count=int(collection.count()),
                embed_model=str(meta["embed_model"]) if "embed_model" in meta else None,
                dimension=int(dimension) if dimension is not None else None,
                index_version=str(meta.get("index_version")) if "index_version" in meta
                else None,
            )
        except Exception as exc:
            return IndexStatus(reachable=True,
                               error=f"no active index ({type(exc).__name__}: {exc}); "
                                     "run the indexer Job")

    # -- query ------------------------------------------------------------------------
    def retrieve(self, question: str, k: int, *, include_superseded: bool = False,
                 vector: list[float] | None = None) -> list[Passage]:
        """Embed the question with the pinned model (unless the caller already did),
        query the live collection for the top k by cosine distance, and exclude
        superseded documents unless the question asks about an older version."""
        if vector is None:
            vector = self.embedder.embed(question)
        where = None if include_superseded else {"superseded": False}
        result = self.active_collection().query(
            query_embeddings=[vector], n_results=k, where=where,
            include=["documents", "metadatas", "distances"])
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        passages = []
        for chunk_id, text, meta, distance in zip(ids, docs, metas, dists):
            meta = meta or {}
            page = meta.get("page")
            passages.append(Passage(
                chunk_id=str(chunk_id), text=str(text or ""),
                source=str(meta.get("source", "")), section=str(meta.get("section", "")),
                page=int(page) if isinstance(page, int | float) else None,
                # the collection uses cosine distance (indexer/pipeline/store.py)
                score=round(1.0 - float(distance), 6),
                doc_version=str(meta["doc_version"]) if "doc_version" in meta else None,
                superseded=bool(meta.get("superseded", False)),
            ))
        return passages
