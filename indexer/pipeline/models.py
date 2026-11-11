"""The shapes passed between stages. Metadata fields are required (section 3.1).

Text between stages is a light Markdown, whatever the source format:
- ``# Heading`` / ``## Heading`` lines are section headings,
- ``| a | b |`` lines are table rows (the first row of a table is its header),
- blank lines separate paragraphs.
Extraction produces it, cleaning preserves it, chunking reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Page:
    """Text of one page, as extraction produced it."""

    source: str              # file name, e.g. Academic_Rules_Handbook_2026.pdf
    page: int                # 1-based page number in the file (not the printed number)
    text: str
    ocr: bool = False        # True when this page had no text layer and was OCR'd


@dataclass
class Document:
    source: str
    doc_version: str         # e.g. "2026-06"; from corpus/sources.yaml
    superseded: bool         # True for an older version of a policy
    pages: list[Page] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)   # per-stage counters, for the logs


@dataclass
class Chunk:
    id: str                  # stable: same document + same position gives the same id
    text: str
    source: str
    section: str             # the heading this chunk sits under, e.g. "Academic Warning"
    page: int
    doc_version: str
    superseded: bool
    # Optional extras (page_end, clauses, ocr, chunk_index, words). Empty by default, so
    # a chunk built without them has exactly the five required metadata fields.
    extra: dict[str, str | int | bool] = field(default_factory=dict)

    def metadata(self) -> dict[str, str | int | bool]:
        """Chroma metadata values must be str, int, float or bool (never None)."""
        return {
            "source": self.source,
            "section": self.section,
            "page": self.page,
            "doc_version": self.doc_version,
            "superseded": self.superseded,
            **self.extra,
        }
