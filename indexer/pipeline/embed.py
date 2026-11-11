"""Stage 4: Embed. One vector per chunk, from the pinned model only.

nomic-embed-text is trained with task prefixes: documents are embedded as
``search_document: <text>`` and questions as ``search_query: <question>``. Using the
wrong prefix, or none, silently lowers retrieval quality, so both prefixes and the
text normalisation live here as constants, and the API's query embedding must use
QUERY_PREFIX with the same ``normalise`` (contract for phase P6).

Batching (EMBED_BATCH_SIZE texts per request) bounds the request size and the memory
held at once: the pipeline embeds and stores one batch before embedding the next.

Failures:
- timeouts, connection errors, HTTP 408, 429 and 5xx are retried EMBED_RETRIES times
  with exponential backoff (Ollama on a laptop CPU can be slow to load the model);
- any other HTTP 4xx is not retried: the request itself is wrong (model not pulled, input
  longer than the model's context with truncation off). The message says what to do;
- a response with the wrong number of vectors, a different dimension, an empty,
  non-finite or all-zero vector is rejected: storing it would poison the index.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from .config import IndexerSettings

DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "      # for the API (P6); defined here so both stay identical
BACKOFF_SECONDS = 1.0
_sleep: Callable[[float], None] = time.sleep   # replaced in tests


class EmbeddingError(RuntimeError):
    """The embedding provider failed or returned unusable vectors."""


def normalise(text: str) -> str:
    """The exact preprocessing applied to every text before embedding: Unicode NFKC,
    tabs and runs of spaces collapsed, blank lines removed, ends trimmed. Newlines
    inside the text are kept (table rows stay rows)."""
    text = unicodedata.normalize("NFKC", text)
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def document_input(section: str, text: str) -> str:
    """What is embedded for one chunk: the prefix, its heading, then its text. The
    heading gives a short clause ("4.19 The request should be made ...") its topic."""
    return f"{DOCUMENT_PREFIX}{normalise(section)}\n{normalise(text)}"


def query_input(question: str) -> str:
    return f"{QUERY_PREFIX}{normalise(question)}"


def _validate(vectors: Any, expected: int, dimension: int | None) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != expected:
        got = len(vectors) if isinstance(vectors, list) else type(vectors).__name__
        raise EmbeddingError(f"expected {expected} embeddings, got {got}")
    checked: list[list[float]] = []
    for i, vector in enumerate(vectors):
        if not isinstance(vector, list) or not vector:
            raise EmbeddingError(f"embedding {i} is empty or not a list")
        values = [float(v) for v in vector]
        if dimension is not None and len(values) != dimension:
            raise EmbeddingError(f"embedding {i} has dimension {len(values)}, expected "
                                 f"{dimension}: vectors from different models cannot be mixed")
        if not all(math.isfinite(v) for v in values):
            raise EmbeddingError(f"embedding {i} contains NaN or infinity")
        if not any(values):
            raise EmbeddingError(f"embedding {i} is all zeros (no direction for cosine)")
        dimension = len(values)
        checked.append(values)
    return checked


def _client_error(response: httpx.Response, model: str) -> EmbeddingError:
    try:
        detail = str(response.json().get("error", response.text))
    except ValueError:
        detail = response.text
    detail = detail[:300]
    if response.status_code == 404 or "not found" in detail.lower():
        return EmbeddingError(f"embedding model {model!r} is not available on the Ollama server "
                              f"({detail}). Run: ollama pull {model}")
    return EmbeddingError(f"Ollama rejected the embedding request: HTTP "
                          f"{response.status_code}: {detail}")


def _embed_batch(http: httpx.Client, settings: IndexerSettings, batch: Sequence[str],
                 dimension: int | None) -> list[list[float]]:
    url = settings.ollama_url.rstrip("/") + "/api/embed"
    # truncate=False: an over-long chunk is an error, never silently cut.
    payload = {"model": settings.embed_model, "input": list(batch), "truncate": False}
    last = "no attempt made"
    for attempt in range(settings.embed_retries + 1):
        try:
            response = http.post(url, json=payload)
        except httpx.TransportError as exc:          # includes every timeout
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise EmbeddingError(f"Ollama returned invalid JSON: {exc}") from exc
                return _validate(body.get("embeddings"), len(batch), dimension)
            # 408 and 429 are "try again later", not "this request is wrong"
            if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                raise _client_error(response, settings.embed_model)
            last = f"HTTP {response.status_code}: {response.text[:200]}"
        if attempt < settings.embed_retries:
            _sleep(BACKOFF_SECONDS * 2 ** attempt)
    raise EmbeddingError(f"embedding failed after {settings.embed_retries + 1} attempts at "
                         f"{url}: {last}")


def http_client(settings: IndexerSettings) -> httpx.Client:
    """The HTTP client for Ollama: EMBED_TIMEOUT_SECONDS for reading (a CPU-only host
    may need a minute to load the model), a short connect timeout to fail fast."""
    return httpx.Client(timeout=httpx.Timeout(settings.embed_timeout_seconds, connect=10.0))


def embed(texts: list[str], settings: IndexerSettings,
          client: httpx.Client | None = None) -> list[list[float]]:
    """Embed with settings.embed_model and nothing else.

    With Ollama: POST {ollama_url}/api/embed with {"model": ..., "input": [texts]}
    returns {"embeddings": [[...], ...]}. Requests are batched; every vector in the
    result has the same dimension, or this raises EmbeddingError."""
    if not texts:
        return []
    http = client or http_client(settings)
    try:
        vectors: list[list[float]] = []
        dimension: int | None = None
        size = settings.embed_batch_size
        for start in range(0, len(texts), size):
            batch = _embed_batch(http, settings, texts[start:start + size], dimension)
            dimension = len(batch[0])
            vectors += batch
        return vectors
    finally:
        if client is None:
            http.close()
