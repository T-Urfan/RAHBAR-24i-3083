"""The whole indexing run on the CI fixture corpus, with a fake Chroma client and fake
(hash) embeddings: offline, deterministic. Real embeddings are used for evaluation only."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pdfgen
import pytest
from fakes import FakeClient

from pipeline import run as run_module
from pipeline import store as store_module
from pipeline.chunk import chunk
from pipeline.clean import clean
from pipeline.config import get_settings
from pipeline.dummy import fake_vector
from pipeline.embed import DOCUMENT_PREFIX, EmbeddingError, document_input
from pipeline.extract import extract
from pipeline.run import discover, run_pipeline
from pipeline.store import IndexBuildError

FIXTURES = Path(__file__).resolve().parents[2] / "eval" / "fixtures"
REQUIRED = {"source", "section", "page", "doc_version", "superseded"}


class FakeEmbedder:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(texts)
        return [fake_vector(t, dim=32) for t in texts]


def _settings(corpus: Path, **overrides: object):  # type: ignore[no-untyped-def]
    values: dict[str, object] = {"corpus_dir": str(corpus), "embed_model": "nomic-embed-text",
                                 "chunk_size": 60, "chunk_overlap": 10, "embed_batch_size": 4}
    return replace(get_settings(), **{**values, **overrides})


def _corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    shutil.copytree(FIXTURES, corpus)
    return corpus


def test_fixture_corpus_is_indexed_and_activated(tmp_path: Path) -> None:
    client, embedder = FakeClient(), FakeEmbedder()
    count = run_pipeline(_settings(_corpus(tmp_path)), client=client, embedder=embedder)
    active = client.active()
    assert count == active.count() > 0
    meta = active.metadata
    assert meta["embed_model"] == "nomic-embed-text" and meta["dimension"] == 32
    assert (meta["chunk_size"], meta["chunk_overlap"], meta["chunk_unit"]) == (60, 10, "words")
    assert meta["documents"] == 3 and meta["failed_documents"] == 0
    assert 1.0 <= meta["amplification"] < 1.5
    rows = list(active.rows.values())
    assert all(REQUIRED <= set(m) for _, _, m in rows)
    sources = {m["source"]: m["superseded"] for _, _, m in rows}
    assert sources == {"academic-rules-2023.md": True, "academic-rules-2026.md": False,
                       "fees-2026.md": False}


def test_embedding_is_streamed_in_batches_with_the_document_prefix(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    settings = _settings(corpus, embed_batch_size=2)
    embedder = FakeEmbedder()
    run_pipeline(settings, client=FakeClient(), embedder=embedder)
    # expected: each document's chunks in batches of 2; a batch never mixes documents
    expected: list[int] = []
    for path, version, superseded in discover(str(corpus)):
        n = len(chunk(clean(extract(str(path), version, superseded, settings)),
                      settings.chunk_size, settings.chunk_overlap))
        expected += [2] * (n // 2) + ([n % 2] if n % 2 else [])
    assert [len(b) for b in embedder.batches] == expected
    assert all(t.startswith(DOCUMENT_PREFIX) for b in embedder.batches for t in b)


def test_every_stored_vector_is_the_embedding_of_its_own_chunk(tmp_path: Path) -> None:
    client = FakeClient()
    run_pipeline(_settings(_corpus(tmp_path), embed_batch_size=3), client=client,
                 embedder=FakeEmbedder())
    rows = client.active().rows
    assert rows
    for document, vector, meta in rows.values():
        assert vector == fake_vector(document_input(str(meta["section"]), document), dim=32)


def test_re_indexing_is_repeatable_and_keeps_the_previous_index(tmp_path: Path) -> None:
    client = FakeClient()
    settings = _settings(_corpus(tmp_path))
    run_pipeline(settings, client=client, embedder=FakeEmbedder())
    first_name, first_ids = client.pointer()["active"], set(client.active().rows)
    run_pipeline(settings, client=client, embedder=FakeEmbedder())
    assert client.pointer()["previous"] == first_name
    assert set(client.active().rows) == first_ids          # stable chunk ids


def test_a_bad_document_blocks_activation_and_the_live_index_keeps_serving(
        tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    client = FakeClient()
    run_pipeline(_settings(corpus), client=client, embedder=FakeEmbedder())
    live = client.pointer()["active"]
    (corpus / "broken.pdf").write_bytes(b"%PDF-1.4 truncated download")

    with pytest.raises(IndexBuildError, match="1 document\\(s\\) failed \\(broken.pdf\\)"):
        run_pipeline(_settings(corpus), client=client, embedder=FakeEmbedder())
    assert client.pointer()["active"] == live
    assert client.versions() == [live]                  # the unfinished build was discarded


def test_partial_index_is_allowed_only_when_asked(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    (corpus / "broken.pdf").write_bytes(b"%PDF-1.4 truncated download")
    client = FakeClient()
    run_pipeline(_settings(corpus, allow_partial_index=True), client=client,
                 embedder=FakeEmbedder())
    meta = client.active().metadata
    assert meta["documents"] == 3 and meta["failed_documents"] == 1


def test_scanned_file_without_ocr_counts_as_a_failed_document(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    pdfgen.scanned_pdf(corpus / "scan.pdf")
    with pytest.raises(IndexBuildError, match="scan.pdf"):
        run_pipeline(_settings(corpus, ocr_enabled=False), client=FakeClient(),
                     embedder=FakeEmbedder())


def test_provider_failure_mid_run_aborts_and_discards(tmp_path: Path) -> None:
    client = FakeClient()
    calls = []

    def failing(texts: list[str]) -> list[list[float]]:
        calls.append(1)
        if len(calls) == 3:
            raise EmbeddingError("Ollama went away")
        return [fake_vector(t, dim=32) for t in texts]

    with pytest.raises(EmbeddingError):
        run_pipeline(_settings(_corpus(tmp_path)), client=client, embedder=failing)
    assert client.versions() == [] and "rahbar-active" not in client.collections


def test_discover_prefers_the_fetch_index(tmp_path: Path) -> None:
    corpus = tmp_path / "raw"
    corpus.mkdir()
    (corpus / "old.pdf").write_bytes(b"x")
    (corpus / "_index.json").write_text(json.dumps(
        [{"file": "old.pdf", "doc_version": "2023-08", "superseded": True}]), encoding="utf-8")
    assert discover(str(corpus)) == [((corpus / "old.pdf").resolve(), "2023-08", True)]


def test_discover_reads_string_booleans_and_refuses_paths_outside_the_corpus(
        tmp_path: Path) -> None:
    corpus = tmp_path / "raw"
    corpus.mkdir()
    (corpus / "_index.json").write_text(json.dumps(
        [{"file": "new.pdf", "doc_version": "2026", "superseded": "false"}]), encoding="utf-8")
    assert discover(str(corpus))[0][2] is False             # "false" is not True
    (corpus / "_index.json").write_text(json.dumps([{"file": "../outside.pdf"}]),
                                        encoding="utf-8")
    with pytest.raises(SystemExit, match="outside"):
        discover(str(corpus))


def test_sigterm_becomes_exit_code_143() -> None:
    import signal

    with pytest.raises(SystemExit) as stopped:
        run_module._terminate(signal.SIGTERM, None)
    assert stopped.value.code == 143


def test_main_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = _corpus(tmp_path)
    client = FakeClient()
    monkeypatch.setattr(store_module, "get_client", lambda settings: client)
    monkeypatch.setenv("CORPUS_DIR", str(corpus))
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:1")     # nothing listens here
    monkeypatch.setenv("EMBED_RETRIES", "0")
    assert run_module.main([]) == 1                            # embedding failed, logged
    assert "rahbar-active" not in client.collections
    monkeypatch.setenv("CHUNK_OVERLAP", "999")
    assert run_module.main([]) == 1                            # invalid configuration
