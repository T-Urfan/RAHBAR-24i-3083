"""Chroma being down must make the API not ready, never crash it (section 4.3)."""

from __future__ import annotations

import pytest
from fakes import Clock, FakeChroma
from fastapi.testclient import TestClient

from app.config import Settings
from app.retrieval.chroma import ChromaRetriever


def test_api_starts_and_stays_alive_with_chroma_unreachable(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # A real chromadb HttpClient against a closed local port: the constructor itself
    # raises "Could not connect to a Chroma server". Before the lazy client, this
    # crashed the whole API at startup.
    monkeypatch.setenv("RETRIEVER", "chroma")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("CHROMA_HOST", "127.0.0.1")
    monkeypatch.setenv("CHROMA_PORT", "1")
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["failed"] == ["chroma"]


def test_client_is_not_built_until_first_use() -> None:
    built = []
    ChromaRetriever(Settings(), client_factory=lambda s: built.append(1) or FakeChroma())
    assert built == []


def test_failed_connection_is_retried_on_the_next_call() -> None:
    attempts = []
    chroma = FakeChroma()
    chroma.publish()

    def factory(settings: Settings) -> FakeChroma:
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("Could not connect to a Chroma server. Are you sure it is running?")
        return chroma

    retriever = ChromaRetriever(Settings(), client_factory=factory)
    assert retriever.describe().reachable is False
    status = retriever.describe()
    assert status.reachable and status.active == "rahbar-v1" and status.count == 3


def test_pointer_is_cached_then_followed_to_a_new_index() -> None:
    chroma = FakeChroma()
    chroma.publish(active="rahbar-v1")
    clock = Clock()
    retriever = ChromaRetriever(Settings(pointer_ttl_seconds=10), client_factory=lambda s: chroma,
                                clock=clock)
    assert retriever.active_collection().name == "rahbar-v1"

    chroma.publish(active="rahbar-v2", rows=7)         # indexer activated a new index
    assert retriever.active_collection().name == "rahbar-v1"   # still within the TTL
    clock.now += 11
    assert retriever.active_collection().name == "rahbar-v2"


def test_garbage_collected_collection_triggers_one_re_resolve() -> None:
    chroma = FakeChroma()
    chroma.publish(active="rahbar-v1")
    retriever = ChromaRetriever(Settings(pointer_ttl_seconds=3600),
                                client_factory=lambda s: chroma)
    retriever.active_collection()
    chroma.publish(active="rahbar-v2")
    del chroma.collections["rahbar-v1"]                # the indexer's gc removed it
    assert retriever.active_collection().name == "rahbar-v2"
