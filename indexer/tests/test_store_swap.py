"""Safe re-indexing (ADR 0001): the live index is only ever replaced by a complete one."""

from __future__ import annotations

import pytest
from fakes import FakeClient

from pipeline import run as run_module
from pipeline import store as store_module
from pipeline.config import get_settings
from pipeline.dummy import write_dummy_index
from pipeline.models import Chunk
from pipeline.store import (
    IndexBuild,
    IndexBuildError,
    activate,
    pointer_name,
    rollback,
    store,
)


def _chunks(n: int, prefix: str = "c") -> list[Chunk]:
    return [Chunk(id=f"{prefix}{i}", text=f"text {i}", source="doc.md", section=f"{i}. S",
                  page=1, doc_version="2026", superseded=False) for i in range(n)]


def _vectors(n: int, dim: int = 4) -> list[list[float]]:
    return [[1.0] + [0.0] * (dim - 1) for _ in range(n)]


def test_first_build_activates_and_records_the_pin() -> None:
    client = FakeClient()
    settings = get_settings()
    assert store(_chunks(5), _vectors(5, dim=8), settings, client=client) == 5
    ptr = client.pointer()
    active = client.active()
    assert active.count() == 5
    assert active.metadata["embed_model"] == settings.embed_model
    assert active.metadata["dimension"] == 8
    assert active.metadata["index_version"] == ptr["index_version"]
    assert active.configuration == {"hnsw": {"space": "cosine"}}
    assert "previous" not in ptr
    assert "hnsw:space" not in ptr


def test_failed_rebuild_leaves_the_live_index_serving() -> None:
    client = FakeClient()
    settings = get_settings()
    store(_chunks(3, "old"), _vectors(3), settings, client=client)
    live_before = client.pointer()["active"]

    client.fail_next_create = True            # the next build crashes on its first write
    with pytest.raises(ConnectionError):
        store(_chunks(4, "new"), _vectors(4), settings, client=client)

    assert client.pointer()["active"] == live_before   # pointer untouched
    assert client.active().count() == 3                # old index intact


def test_count_mismatch_refuses_activation() -> None:
    client = FakeClient()
    build = IndexBuild(client, get_settings(), embed_model="m")
    dup = _chunks(2)
    dup[1].id = dup[0].id                     # Chroma silently keeps only one row
    build.add(dup, _vectors(2))
    with pytest.raises(IndexBuildError, match="duplicate chunk ids"):
        activate(build)
    assert pointer_name("rahbar") not in client.collections


def test_empty_index_is_never_activated() -> None:
    client = FakeClient()
    with pytest.raises(IndexBuildError, match="empty"):
        store([], [], get_settings(), client=client)


def test_vectors_must_match_chunks_and_share_one_dimension() -> None:
    build = IndexBuild(FakeClient(), get_settings(), embed_model="m")
    with pytest.raises(IndexBuildError, match="chunks but"):
        build.add(_chunks(2), _vectors(1))
    with pytest.raises(IndexBuildError, match="dimensions"):
        build.add(_chunks(2), [[1.0, 0.0], [1.0, 0.0, 0.0]])
    build.add(_chunks(1, "a"), _vectors(1, dim=4))
    with pytest.raises(IndexBuildError, match="changed mid-build"):
        build.add(_chunks(1, "b"), _vectors(1, dim=5))


def test_writes_are_batched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_module, "WRITE_BATCH", 10)
    client = FakeClient()
    build = IndexBuild(client, get_settings(), embed_model="m")
    build.add(_chunks(25), _vectors(25))
    assert build.collection.add_calls == 3    # 10 + 10 + 5


def test_gc_keeps_only_active_and_previous() -> None:
    client = FakeClient()
    settings = get_settings()
    for i in range(4):
        store(_chunks(2, f"r{i}"), _vectors(2), settings, client=client)
    versions = client.versions()
    assert len(versions) == 2
    ptr = client.pointer()
    assert set(versions) == {ptr["active"], ptr["previous"]}


def test_rollback_swaps_active_and_previous_and_can_be_undone() -> None:
    client = FakeClient()
    settings = get_settings()
    store(_chunks(2, "first"), _vectors(2), settings, client=client)
    first = client.pointer()["active"]
    store(_chunks(3, "second"), _vectors(3), settings, client=client)
    second = client.pointer()["active"]

    result = rollback(client, settings)
    assert result.active == first and client.pointer()["active"] == first
    assert client.pointer()["previous"] == second
    assert client.active().count() == 2

    rollback(client, settings)                # undo the rollback
    assert client.pointer()["active"] == second


def test_rollback_without_previous_fails_cleanly() -> None:
    client = FakeClient()
    store(_chunks(1), _vectors(1), get_settings(), client=client)
    with pytest.raises(IndexBuildError, match="no previous index"):
        rollback(client, get_settings())


def test_dummy_rerun_keeps_serving_and_never_deletes_first() -> None:
    client = FakeClient()
    settings = get_settings()
    write_dummy_index(settings, client)
    first = client.pointer()["active"]
    write_dummy_index(settings, client)
    assert client.pointer()["previous"] == first
    assert first in client.collections


def test_cli_rollback_and_refusal_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    monkeypatch.setattr(store_module, "get_client", lambda settings: client)
    assert run_module.main(["--dummy"]) == 0
    assert run_module.main(["--rollback"]) == 1      # only one index: refused, exit 1
    assert run_module.main(["--dummy"]) == 0
    assert run_module.main(["--rollback"]) == 0
