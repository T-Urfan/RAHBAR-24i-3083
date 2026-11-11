"""Stage 4, embedding: batching, prefixes, validation and failure handling.
Ollama is replaced by httpx.MockTransport: no network, no model."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from pipeline import embed as embed_module
from pipeline.config import get_settings
from pipeline.embed import (
    DOCUMENT_PREFIX,
    QUERY_PREFIX,
    EmbeddingError,
    document_input,
    embed,
    normalise,
    query_input,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(embed_module, "_sleep", slept.append)
    return slept


def _settings(**overrides: object):  # type: ignore[no-untyped-def]
    return replace(get_settings(), ollama_url="http://ollama.test:11434",
                   embed_model="nomic-embed-text", **overrides)


def _ok(dim: int = 4):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[1.0] + [0.5] * (dim - 1)
                                                        for _ in body["input"]]})
    return handler


def _client(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_texts_are_sent_in_batches_with_the_pinned_model() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert request.url == "http://ollama.test:11434/api/embed"
        return _ok(8)(request)

    vectors = embed([f"t{i}" for i in range(37)], _settings(embed_batch_size=16),
                    client=_client(handler))
    assert len(vectors) == 37 and all(len(v) == 8 for v in vectors)
    assert [len(r["input"]) for r in requests] == [16, 16, 5]
    assert all(r["model"] == "nomic-embed-text" and r["truncate"] is False for r in requests)


def test_empty_input_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert embed([], _settings(), client=_client(handler)) == []


def test_document_and_query_prefixes_and_normalisation() -> None:
    assert DOCUMENT_PREFIX == "search_document: " and QUERY_PREFIX == "search_query: "
    assert document_input("Fee  Refund\tPolicy", "| A | 1 |\n\n| B | 2 |  ") == \
        "search_document: Fee Refund Policy\n| A | 1 |\n| B | 2 |"
    assert query_input("  What is the  ﬁne? ") == "search_query: What is the fine?"
    assert normalise("a b") == "a b"


def test_dimension_must_stay_the_same_across_batches() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _ok(4 if len(calls) == 1 else 5)(request)

    with pytest.raises(EmbeddingError, match="dimension 5, expected 4"):
        embed(["a", "b", "c"], _settings(embed_batch_size=2), client=_client(handler))


@pytest.mark.parametrize(("payload", "message"), [
    ({"embeddings": [[1.0, 0.0]]}, "expected 2 embeddings, got 1"),
    ({"embeddings": [[1.0, 0.0], []]}, "empty"),
    ({"embeddings": [[1.0, 0.0], [1.0]]}, "dimension 1, expected 2"),
    ({"embeddings": [[1.0, 0.0], [float("nan"), 1.0]]}, "NaN"),
    ({"embeddings": [[1.0, 0.0], [0.0, 0.0]]}, "all zeros"),
    ({"something": "else"}, "expected 2 embeddings"),
])
def test_unusable_responses_are_rejected(payload: dict, message: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).replace("NaN", "NaN"))

    with pytest.raises(EmbeddingError, match=message):
        embed(["a", "b"], _settings(), client=_client(handler))


def test_transient_failures_are_retried_with_backoff(no_sleep: list[float]) -> None:
    responses = iter(["timeout", 503, "ok"])

    def handler(request: httpx.Request) -> httpx.Response:
        step = next(responses)
        if step == "timeout":
            raise httpx.ReadTimeout("model still loading", request=request)
        if step == 503:
            return httpx.Response(503, text="busy")
        return _ok()(request)

    assert len(embed(["a"], _settings(embed_retries=3), client=_client(handler))) == 1
    assert no_sleep == [1.0, 2.0]


def test_retries_are_bounded(no_sleep: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(EmbeddingError, match="after 3 attempts.*ConnectError"):
        embed(["a"], _settings(embed_retries=2), client=_client(handler))
    assert no_sleep == [1.0, 2.0]


def test_missing_model_is_not_retried_and_says_what_to_do(no_sleep: list[float]) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, json={"error": 'model "nomic-embed-text" not found, '
                                                  "try pulling it first"})

    with pytest.raises(EmbeddingError, match="ollama pull nomic-embed-text"):
        embed(["a"], _settings(), client=_client(handler))
    assert calls == [1] and no_sleep == []


def test_other_client_errors_are_reported_without_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "input length exceeds the context length"})

    with pytest.raises(EmbeddingError, match="HTTP 400.*context length"):
        embed(["a"], _settings(), client=_client(handler))


@pytest.mark.parametrize("status", [408, 429])
def test_rate_limit_and_request_timeout_are_retried(status: int, no_sleep: list[float]) -> None:
    responses = iter([status, "ok"])

    def handler(request: httpx.Request) -> httpx.Response:
        step = next(responses)
        return httpx.Response(step, text="slow down") if step != "ok" else _ok()(request)

    assert len(embed(["a"], _settings(), client=_client(handler))) == 1
    assert no_sleep == [1.0]


def test_http_client_uses_the_configured_timeout() -> None:
    from pipeline.embed import http_client

    with http_client(_settings(embed_timeout_seconds=95.0)) as client:
        assert client.timeout.read == 95.0 and client.timeout.connect == 10.0


def test_vectors_come_back_in_input_order_across_batches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        return httpx.Response(200, json={"embeddings": [[float(t[1:]) + 1, 1.0] for t in texts]})

    vectors = embed([f"t{i}" for i in range(7)], _settings(embed_batch_size=3),
                    client=_client(handler))
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]


def test_invalid_json_is_an_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>proxy error</html>")

    with pytest.raises(EmbeddingError, match="invalid JSON"):
        embed(["a"], _settings(), client=_client(handler))
