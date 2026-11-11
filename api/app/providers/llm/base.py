"""The LLM provider interface (section 3.5). Swapping providers is one file.

A provider makes ONE call and returns a validated `Answer`. It raises
`pydantic.ValidationError` when the reply does not fit the schema, and
`ProviderError` when the call itself fails (timeout, connection, HTTP error). The
retry-once-then-fallback policy is not the provider's job: it lives in
services/answer.py, so it is identical for every provider.
"""

from __future__ import annotations

from typing import Protocol

from ...schemas import Answer, Passage


class ProviderError(RuntimeError):
    """The model could not be called, or did not reply in time."""


class LLMProvider(Protocol):
    name: str

    def generate(self, question: str, passages: list[Passage],
                 retry_reason: str | None = None) -> Answer:
        """Answer the question from the passages, as an `Answer`."""
        ...
