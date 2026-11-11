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


def test_duplicate_chunk_ids_are_rejected_within_and_across_batches(
        monkeypatch: pytest.MonkeyPatch) -> None:
    build = IndexBuild(FakeClient(), get_settings(), embed_model="m")
    dup = _chunks(2)
    dup[1].id = dup[0].id
    with pytest.raises(IndexBuildError, match="duplicate chunk ids"):
        build.add(dup, _vectors(2))
    build.add(_chunks(2, "x"), _vectors(2))
    with pytest.raises(IndexBuildError, match=r"duplicate chunk ids: \['x1'\]"):
        build.add([_chunks(2, "x")[1]], _vectors(1))     # same id in a later batch


def test_rows_lost_by_the_server_refuse_activation() -> None:
    client = FakeClient()
    build = IndexBuild(client, get_settings(), embed_model="m")
    build.add(_chunks(3, "a"), _vectors(3))
    build.collection.lose_rows = 1           # the server will silently drop one row
    build.add(_chunks(1, "b"), _vectors(1))  # we sent 4 rows in total, it kept 3
    with pytest.raises(IndexBuildError, match="holds 3 rows but 4 were written"):
        activate(build)
    assert pointer_name("rahbar") not in client.collections


def test_every_stored_vector_belongs_to_its_own_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_module, "WRITE_BATCH", 3)    # several add() calls
    client = FakeClient()
    chunks = _chunks(10)
    vectors = [[float(i + 1), 0.5, 0.25, 0.125] for i in range(10)]   # all different
    store(chunks, vectors, get_settings(), client=client)
    rows = client.active().rows
    for i, c in enumerate(chunks):
        document, vector, meta = rows[c.id]
        assert vector == vectors[i] and document == c.text and meta["section"] == c.section


def test_gc_never_deletes_a_newer_build_still_in_progress() -> None:
    client = FakeClient()
    settings = get_settings()
    store(_chunks(1, "a"), _vectors(1), settings, client=client)
    other = client.create_collection(name="rahbar-v99991231t235959999999-ffffff",  # another run
                                     metadata={"embed_model": "m"}, embedding_function=None)
    store(_chunks(1, "b"), _vectors(1), settings, client=client)
    store(_chunks(1, "c"), _vectors(1), settings, client=client)
    assert other.name in client.collections
    assert len(client.versions()) == 3          # active, previous, and the newer build


def test_gc_failure_is_logged_and_never_fails_activation(
        monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    settings = get_settings()
    for prefix in "abc":
        store(_chunks(1, prefix), _vectors(1), settings, client=client)

    def refuse(name: str) -> None:
        raise ConnectionError("simulated: delete timed out")

    monkeypatch.setattr(client, "delete_collection", refuse)
    assert store(_chunks(1, "d"), _vectors(1), settings, client=client) == 1
    assert client.pointer()["active"] == client.versions()[-1]


def test_failure_after_the_pointer_moved_never_deletes_the_live_collection(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Review finding: gc's list_collections failing after activation used to reach
    the run's cleanup, which deleted the collection the pointer had just named."""
    client = FakeClient()
    settings = get_settings()
    store(_chunks(1, "a"), _vectors(1), settings, client=client)

    def blip() -> list:
        raise ConnectionError("simulated: Chroma blip during gc")

    monkeypatch.setattr(client, "list_collections", blip)
    assert store(_chunks(2, "b"), _vectors(2), settings, client=client) == 2
    assert client.active().count() == 2                  # the pointer names a real collection


def test_pointer_update_whose_reply_is_lost_does_not_delete_the_new_index() -> None:
    client = FakeClient()
    settings = get_settings()
    build = IndexBuild(client, settings, embed_model="m")
    build.add(_chunks(2), _vectors(2))
    pointer = client.get_or_create_collection(name="rahbar-active", embedding_function=None)
    real_modify = pointer.modify

    def applied_then_timeout(**kwargs: object) -> None:
        real_modify(**kwargs)                             # the server applied it...
        raise TimeoutError("...but the reply was lost")

    pointer.modify = applied_then_timeout                 # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        activate(build)
    build.discard()                                       # what run_pipeline does next
    assert client.pointer()["active"] == build.name
    assert build.name in client.collections               # still there: it may be live


def test_discard_cleans_a_collection_whose_create_reply_was_lost() -> None:
    client = FakeClient()
    build = IndexBuild(client, get_settings(), embed_model="m")
    real_create = client.create_collection

    def created_then_timeout(**kwargs: object) -> None:
        real_create(**kwargs)                             # type: ignore[arg-type]
        raise TimeoutError("create reply lost")

    client.create_collection = created_then_timeout       # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        build.add(_chunks(1), _vectors(1))
    build.discard()
    assert build.name not in client.collections


def test_gc_only_touches_this_base_exact_version_names() -> None:
    client = FakeClient()
    for name in ("rahbar-v2-active", "rahbar-v2-v20200101t000000000000-aaaaaa",
                 "rahbar-v-notaversion"):
        client.create_collection(name=name, metadata={"x": 1}, embedding_function=None)
    for prefix in "abc":
        store(_chunks(1, prefix), _vectors(1), get_settings(), client=client)
    for name in ("rahbar-v2-active", "rahbar-v2-v20200101t000000000000-aaaaaa",
                 "rahbar-v-notaversion"):
        assert name in client.collections


def test_versions_sort_in_creation_order_even_within_one_clock_tick() -> None:
    from datetime import datetime, timezone

    from pipeline.store import new_index_version
    same = datetime(2026, 11, 11, tzinfo=timezone.utc)
    versions = [new_index_version(same) for _ in range(5)]
    assert versions == sorted(versions) and len(set(versions)) == 5


def test_rollback_when_the_previous_index_is_gone_is_a_clean_refusal() -> None:
    client = FakeClient()
    settings = get_settings()
    store(_chunks(1, "a"), _vectors(1), settings, client=client)
    store(_chunks(1, "b"), _vectors(1), settings, client=client)
    del client.collections[client.pointer()["previous"]]
    with pytest.raises(IndexBuildError, match="no longer exists"):
        rollback(client, settings)
    with pytest.raises(IndexBuildError, match="no active index"):
        rollback(FakeClient(), settings)


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
