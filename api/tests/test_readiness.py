"""/readyz: 200 only when every dependency is fit to serve (section 4.3)."""

from __future__ import annotations

import logging

import pytest
from fakes import Clock, FakeChroma, FakeRedis
from fastapi.testclient import TestClient

from app.config import Settings
from app.retrieval.chroma import ChromaRetriever
from app.retrieval.stub import StubRetriever
from app.services.readiness import ReadinessChecker


def _checker(chroma: FakeChroma, redis: FakeRedis | None = None, clock: Clock | None = None,
             embed_model: str = "nomic-embed-text") -> ReadinessChecker:
    settings = Settings(retriever="chroma", embed_model=embed_model, redis_host="redis")
    retriever = ChromaRetriever(settings, client_factory=lambda s: chroma)
    return ReadinessChecker(settings, retriever, redis if redis is not None else FakeRedis(),
                            clock=clock or Clock())


def test_ready_when_everything_is_healthy() -> None:
    chroma = FakeChroma()
    chroma.publish(rows=42)
    ok, body = _checker(chroma).check()
    assert ok, body
    assert body["status"] == "ready"
    assert body["checks"]["collection"] == "rahbar-v1: 42 chunks"
    assert body["checks"]["embed_model"].startswith("ok:")
    assert body["checks"]["redis"] == "ok"


def test_chroma_down_names_chroma() -> None:
    chroma = FakeChroma()
    chroma.down = True
    ok, body = _checker(chroma).check()
    assert not ok and body["failed"] == ["chroma"]


def test_no_index_yet_names_collection_missing() -> None:
    ok, body = _checker(FakeChroma()).check()
    assert not ok and body["failed"] == ["collection_missing"]
    assert "run the indexer Job" in body["checks"]["collection"]


def test_empty_collection_is_not_ready() -> None:
    chroma = FakeChroma()
    chroma.publish(rows=0)
    ok, body = _checker(chroma).check()
    assert not ok and "collection_empty" in body["failed"]


def test_embedding_pin_mismatch_is_not_ready_and_logs_the_conflict(
        caplog: pytest.LogCaptureFixture) -> None:
    chroma = FakeChroma()
    chroma.publish(embed_model="dummy-768", dimension=768)
    with caplog.at_level(logging.WARNING, logger="rahbar.readiness"):
        ok, body = _checker(chroma, embed_model="nomic-embed-text").check()
    assert not ok and body["failed"] == ["embed_model_mismatch"]
    message = body["checks"]["embed_model"]
    assert "dummy-768" in message and "nomic-embed-text" in message and "768" in message
    assert any(r.message == "not ready" for r in caplog.records)


def test_redis_down_names_redis() -> None:
    chroma = FakeChroma()
    chroma.publish()
    ok, body = _checker(chroma, redis=FakeRedis(up=False)).check()
    assert not ok and body["failed"] == ["redis"]


def test_every_failure_is_reported_not_just_the_first() -> None:
    chroma = FakeChroma()
    chroma.publish(rows=0, embed_model="other-model")
    ok, body = _checker(chroma, redis=FakeRedis(up=False)).check()
    assert body["failed"] == ["collection_empty", "embed_model_mismatch", "redis"]


def test_results_are_cached_for_the_ttl() -> None:
    chroma = FakeChroma()
    chroma.publish()
    clock = Clock()
    checker = _checker(chroma, clock=clock)
    checker.check()
    checker.check()
    assert chroma.heartbeats == 1           # second probe served from cache
    clock.now += 10
    checker.check()
    assert chroma.heartbeats == 2


def test_recovers_when_chroma_comes_back() -> None:
    chroma = FakeChroma()
    chroma.down = True
    clock = Clock()
    checker = _checker(chroma, clock=clock)
    assert checker.check()[0] is False
    chroma.down = False
    chroma.publish()
    clock.now += 10
    assert checker.check()[0] is True


def test_stub_retriever_skips_chroma_and_unset_redis_is_skipped() -> None:
    settings = Settings()
    ok, body = ReadinessChecker(settings, StubRetriever(), None).check()
    assert ok
    assert body["checks"]["chroma"].startswith("skipped")
    assert body["checks"]["redis"].startswith("skipped")


def test_readyz_route_returns_503_with_the_failed_dependency(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVER", "stub")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        chroma = FakeChroma()
        client.app.state.readiness = _checker(chroma)   # chroma up, but no index yet
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["failed"] == ["collection_missing"]
