"""The API and the indexer are separate images, so they cannot import each other.
They must still agree on how the live index is named. This test reads the indexer's
constant straight from its source file."""

from __future__ import annotations

import ast
from pathlib import Path

from app.retrieval.chroma import POINTER_SUFFIX, pointer_name

STORE = Path(__file__).resolve().parents[2] / "indexer" / "pipeline" / "store.py"


def _indexer_constant(name: str) -> str:
    for node in ast.parse(STORE.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            assert isinstance(node.value, ast.Constant)
            return str(node.value.value)
    raise AssertionError(f"{name} not found in {STORE}")


def test_api_and_indexer_agree_on_the_pointer_name() -> None:
    assert POINTER_SUFFIX == _indexer_constant("POINTER_SUFFIX")
    assert pointer_name("rahbar") == "rahbar-active"
