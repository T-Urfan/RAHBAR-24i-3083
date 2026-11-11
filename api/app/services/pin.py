"""The embedding-model pin, enforced in code (section 4.4).

Vectors from two different embedding models are not comparable, and nothing errors
when they are mixed: retrieval just returns nonsense. So the indexer records the
model it used in the collection's metadata, and the API refuses to become ready
when that differs from its own EMBED_MODEL.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..retrieval.chroma import IndexStatus


@dataclass(frozen=True)
class PinCheck:
    ok: bool
    message: str


def check_pin(expected_model: str, status: IndexStatus) -> PinCheck:
    if status.embed_model is None:
        return PinCheck(False, f"embedding pin unknown: index {status.active} records no "
                               f"embed_model, EMBED_MODEL={expected_model}; refusing to be ready")
    if status.embed_model != expected_model:
        return PinCheck(False, f"embedding pin mismatch: index {status.active} was built with "
                               f"{status.embed_model} (dimension {status.dimension}) but "
                               f"EMBED_MODEL={expected_model}; refusing to be ready. Re-run the "
                               "indexer with this model, or set EMBED_MODEL back.")
    return PinCheck(True, f"{status.embed_model} (dimension {status.dimension})")
