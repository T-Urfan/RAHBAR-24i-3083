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


EMBED = STORE.parent / "embed.py"


def _indexer_function(name: str):  # type: ignore[no-untyped-def]
    """Compile one function from the indexer's embed.py on its own (no package import)."""
    tree = ast.parse(EMBED.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace: dict = {}
    module = ast.fix_missing_locations(ast.Module(
        body=[ast.Import(names=[ast.alias("re"), ast.alias("unicodedata")]), fn], type_ignores=[]))
    exec(compile(module, str(EMBED), "exec"), namespace)
    return namespace[name]


def test_api_and_indexer_use_the_same_query_prefix() -> None:
    from app.services.embedding import QUERY_PREFIX

    tree = ast.parse(EMBED.read_text(encoding="utf-8"))
    value = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "QUERY_PREFIX" for t in n.targets)
                 and isinstance(n.value, ast.Constant))
    assert QUERY_PREFIX == value == "search_query: "


def test_api_and_indexer_normalise_text_identically() -> None:
    from app.services.embedding import normalise

    indexer_normalise = _indexer_function("normalise")
    samples = ["ﬁnal  exam", "  a\t b \n\n c  ", "Clause 1.21 requires 100%",
               "| Grade | Points |\n| A+ | 4.00 |", "CGPA　 2.00"]
    for text in samples:
        assert normalise(text) == indexer_normalise(text)
