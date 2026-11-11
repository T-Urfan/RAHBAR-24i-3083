"""Indexer configuration: same parsing rules as the API, plus indexer-only checks."""

from __future__ import annotations

import pytest

from pipeline.config import ConfigError, get_settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CHUNK_SIZE", "CHUNK_OVERLAP", "EMBED_BATCH_SIZE", "OCR_ENABLED",
                 "CHUNK_STRATEGY", "ALLOW_PARTIAL_INDEX"):
        monkeypatch.delenv(name, raising=False)
    s = get_settings()
    assert (s.chunk_size, s.chunk_overlap, s.embed_batch_size) == (400, 50, 16)
    assert s.ocr_enabled is True and s.chunk_strategy == "structure"
    assert s.allow_partial_index is False


def test_values_and_booleans(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHUNK_SIZE", " 300 ")
    monkeypatch.setenv("OCR_ENABLED", "false")
    monkeypatch.setenv("ALLOW_PARTIAL_INDEX", "YES")
    monkeypatch.setenv("CHUNK_STRATEGY", "Fixed")
    monkeypatch.setenv("EMBED_BATCH_SIZE", "")
    s = get_settings()
    assert (s.chunk_size, s.ocr_enabled, s.allow_partial_index, s.chunk_strategy,
            s.embed_batch_size) == (300, False, True, "fixed", 16)


@pytest.mark.parametrize(("name", "value", "match"), [
    ("CHUNK_SIZE", "big", "not an integer"),
    ("CHUNK_SIZE", "5", "at least 20"),
    ("CHUNK_OVERLAP", "400", "must be smaller than CHUNK_SIZE"),
    ("OCR_ENABLED", "maybe", "not a boolean"),
    ("CHUNK_STRATEGY", "semantic", "structure or fixed"),
    ("EMBED_TIMEOUT_SECONDS", "soon", "not a number"),
    ("EMBED_BATCH_SIZE", "0", "at least 1"),
    ("CHROMA_COLLECTION", "a-name-that-is-far-too-long-for-chroma", "3-33 characters"),
    ("CHROMA_COLLECTION", "bad name", "3-33 characters"),
    ("CHROMA_COLLECTION", "-x-", "3-33 characters"),
])
def test_bad_values_name_the_variable(monkeypatch: pytest.MonkeyPatch, name: str, value: str,
                                      match: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=match):
        get_settings()
