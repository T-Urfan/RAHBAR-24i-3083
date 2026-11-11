"""The API only reads the index; the indexer Job is the only writer (section 4.2)."""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest
from fakes import FakeChroma

from app.config import Settings
from app.retrieval.chroma import ChromaRetriever

APP = Path(__file__).resolve().parents[1] / "app"
WRITES = re.compile(
    r"\b(create_collection|get_or_create_collection|delete_collection)\b"
    r"|\.(add|upsert|update|delete|modify)\s*\(")


@pytest.mark.parametrize("method", ["create_collection", "delete_collection",
                                    "get_or_create_collection", "reset"])
def test_client_write_methods_are_blocked(method: str) -> None:
    chroma = FakeChroma()
    retriever = ChromaRetriever(Settings(), client_factory=lambda s: chroma)
    client = retriever._get_client()
    with pytest.raises(PermissionError, match="read-only"):
        getattr(client, method)


@pytest.mark.parametrize("method", ["add", "upsert", "update", "delete", "modify"])
def test_collection_write_methods_are_blocked(method: str) -> None:
    chroma = FakeChroma()
    chroma.publish()
    collection = ChromaRetriever(Settings(), client_factory=lambda s: chroma).active_collection()
    with pytest.raises(PermissionError, match="read-only"):
        getattr(collection, method)


def _code(path: Path) -> str:
    """Source without comments or strings, so documentation that names a write
    method is not mistaken for a call to it."""
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            out.append(tok.string)
    return " ".join(out).replace(" . ", ".").replace(" (", "(")


def test_no_write_call_anywhere_in_the_api_source() -> None:
    offenders = [str(p.relative_to(APP)) for p in APP.rglob("*.py") if WRITES.search(_code(p))]
    assert offenders == [], f"Chroma write calls found in the API: {offenders}"
