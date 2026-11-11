"""The retriever interface. The rest of the API only ever sees this."""

from __future__ import annotations

from typing import Protocol

from ..schemas import Passage


class Retriever(Protocol):
    name: str

    def retrieve(self, question: str, k: int, *, include_superseded: bool = False,
                 vector: list[float] | None = None) -> list[Passage]:
        """Return up to k passages, best first. Superseded documents are excluded
        unless include_superseded. `vector` is the question's embedding when the
        caller already has it (the semantic cache computed it first)."""
        ...
