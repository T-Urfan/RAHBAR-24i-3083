"""ChromaRetriever.retrieve, the query embedder, and the version filter."""

from __future__ import annotations

import json

import httpx
import pytest
from fakes import FakeChroma, FakeEmbedder, QueryableCollection

from app.config import Settings
from app.retrieval.chroma import ChromaRetriever
from app.services.embedding import QueryEmbedder, QueryEmbeddingError, cosine, query_input
from app.services.versioning import wants_older_version

HITS = [
    {"id": "old-1", "text": "CGPA 1.75 avoids warning.", "distance": 0.20,
     "meta": {"source": "rules-2023.pdf", "section": "Warning", "page": 7,
              "doc_version": "2023-08", "superseded": True}},
    {"id": "new-1", "text": "CGPA 2.00 avoids warning.", "distance": 0.25,
     "meta": {"source": "rules-2026.pdf", "section": "5.4 Warning", "page": 24,
              "doc_version": "2026-06", "superseded": False}},
    {"id": "new-2", "text": "Probation follows.", "distance": 0.40,
     "meta": {"source": "rules-2026.pdf", "section": "5.5 Probation", "page": 25,
              "doc_version": "2026-06", "superseded": False}},
]


def retriever() -> tuple[ChromaRetriever, QueryableCollection, FakeEmbedder]:
    chroma = FakeChroma()
    chroma.publish(active="rahbar-v1")
    coll = QueryableCollection("rahbar-v1", HITS, chroma.collections["rahbar-v1"].metadata)
    chroma.collections["rahbar-v1"] = coll  # type: ignore[assignment]
    embedder = FakeEmbedder([0.5, 0.5, 0.0])
    return ChromaRetriever(Settings(), client_factory=lambda s: chroma,
                           embedder=embedder), coll, embedder


def test_retrieve_excludes_superseded_and_converts_distance_to_similarity() -> None:
    r, coll, embedder = retriever()
    passages = r.retrieve("What CGPA avoids warning?", k=5)
    assert [p.chunk_id for p in passages] == ["new-1", "new-2"]
    assert coll.calls[0]["where"] == {"superseded": False}
    assert coll.calls[0]["query_embeddings"] == [[0.5, 0.5, 0.0]]
    assert passages[0].score == pytest.approx(0.75)
    assert passages[0].page == 24 and passages[0].section == "5.4 Warning"
    assert passages[0].doc_version == "2026-06" and passages[0].superseded is False
    assert embedder.calls == ["What CGPA avoids warning?"]


def test_retrieve_with_older_versions_has_no_filter() -> None:
    r, coll, _ = retriever()
    passages = r.retrieve("q", k=5, include_superseded=True)
    assert coll.calls[0]["where"] is None
    assert passages[0].superseded is True


def test_a_supplied_vector_is_not_embedded_again() -> None:
    r, coll, embedder = retriever()
    r.retrieve("q", k=2, vector=[1.0, 0.0, 0.0])
    assert embedder.calls == [] and coll.calls[0]["n_results"] == 2


def test_generation_is_the_live_collection_name() -> None:
    r, _, _ = retriever()
    assert r.generation() == "rahbar-v1"


def embedder_with(handler) -> tuple[QueryEmbedder, list[httpx.Request]]:  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)
    client = httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(wrapped))
    return QueryEmbedder(Settings(embed_model="nomic-embed-text"), client), seen


def test_query_embedding_uses_the_pinned_model_and_the_query_prefix() -> None:
    e, seen = embedder_with(lambda r: httpx.Response(200, json={"embeddings": [[0.1, 0.2]]}))
    assert e.embed("  What   is clause 1.21? ") == [0.1, 0.2]
    body = json.loads(seen[0].content)
    assert body["model"] == "nomic-embed-text"
    assert body["input"] == ["search_query: What is clause 1.21?"]


def test_query_embedding_retries_a_5xx_once() -> None:
    replies = [httpx.Response(503), httpx.Response(200, json={"embeddings": [[1.0]]})]
    e, seen = embedder_with(lambda r: replies.pop(0))
    assert e.embed("q?") == [1.0] and len(seen) == 2


@pytest.mark.parametrize("reply", [
    httpx.Response(404, text="model not found"),
    httpx.Response(200, json={"embeddings": []}),
    httpx.Response(200, json={"embeddings": [[0.0, 0.0]]}),
])
def test_unusable_embedding_responses_raise(reply: httpx.Response) -> None:
    e, _ = embedder_with(lambda r: reply)
    with pytest.raises(QueryEmbeddingError):
        e.embed("q?")


def test_unreachable_embedder_raises_after_one_retry() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)
    e, seen = embedder_with(down)
    with pytest.raises(QueryEmbeddingError, match="unreachable"):
        e.embed("q?")
    assert len(seen) == 2


def test_query_input_and_cosine() -> None:
    assert query_input("ﬁnal  exam\n\n") == "search_query: final exam"
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0], [1.0, 0.0]) == -1.0


@pytest.mark.parametrize("question, older", [
    ("What did the 2023 rules say about attendance?", True),
    ("What was the old attendance policy?", True),
    ("What was the previous CGPA threshold?", True),
    ("Under the superseded handbook, what was the grade cap?", True),
    ("What has changed in the attendance rule?", True),
    ("What CGPA puts a student on academic warning?", False),
    ("Does withdrawal need prior approval?", False),
    ("Can I change my section after the first week?", False),
    ("What is the fee for the Fall 2026 semester?", False),
])
def test_version_intent(question: str, older: bool) -> None:
    assert wants_older_version(question) is older
