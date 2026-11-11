"""Query embedding (section 3.2, step 3): the same model and preprocessing as the indexer.

nomic-embed-text is trained with task prefixes. The indexer embeds chunks as
``search_document: ...`` and questions must be embedded as ``search_query: ...``,
after exactly the same ``normalise``. The API and the indexer are separate images, so
these lines are duplicated from indexer/pipeline/embed.py; api/tests/test_index_contract.py
fails if the two copies ever drift.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata

import httpx

from ..config import Settings

QUERY_PREFIX = "search_query: "
RETRIES = 1                      # one retry: a question waits, so fail fast after that


class QueryEmbeddingError(RuntimeError):
    """The embedding service failed; the request falls back instead of erroring."""


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def query_input(question: str) -> str:
    return f"{QUERY_PREFIX}{normalise(question)}"


class QueryEmbedder:
    """POST {OLLAMA_URL}/api/embed with the pinned EMBED_MODEL."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.model = settings.embed_model
        self._client = client or httpx.Client(
            base_url=settings.ollama_url,
            timeout=httpx.Timeout(settings.embed_timeout_seconds, connect=5.0))

    def embed(self, question: str) -> list[float]:
        payload = {"model": self.model, "input": [query_input(question)], "truncate": True}
        last: Exception | None = None
        for attempt in range(RETRIES + 1):
            try:
                response = self._client.post("/api/embed", json=payload)
                if response.status_code >= 500 and attempt < RETRIES:
                    last = QueryEmbeddingError(f"HTTP {response.status_code}")
                    time.sleep(0.2)
                    continue
                response.raise_for_status()
                vectors = response.json().get("embeddings")
                return _validate(vectors)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                if attempt < RETRIES:
                    time.sleep(0.2)
                    continue
            except httpx.HTTPStatusError as exc:
                raise QueryEmbeddingError(
                    f"embedding model {self.model} failed: HTTP {exc.response.status_code} "
                    f"{exc.response.text[:200]}") from exc
        raise QueryEmbeddingError(f"embedding service unreachable: {type(last).__name__}: {last}")


def _validate(vectors: object) -> list[float]:
    if not isinstance(vectors, list) or len(vectors) != 1 or not isinstance(vectors[0], list):
        raise QueryEmbeddingError("embedding response did not hold exactly one vector")
    values = [float(v) for v in vectors[0]]
    if not values or not all(math.isfinite(v) for v in values) or not any(values):
        raise QueryEmbeddingError("embedding vector is empty, non-finite or all zeros")
    return values


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else -1.0
