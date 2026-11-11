"""Stage 3: Chunk. Structure-aware, with overlap.

Sizes are in words (whitespace-separated tokens), not model tokens: the indexer
needs no tokenizer, and the unit is the same everywhere it is reported. For English
policy text one word is roughly 1.3 nomic tokens, so CHUNK_SIZE=400 words is about
520 tokens, well inside the model's context.

Structure-aware ("structure" strategy, the default):
1. Headings (``#`` lines) start a new section. A chunk never spans two sections,
   so every chunk has exactly one heading to cite.
2. Within a section the units are paragraphs, and every numbered clause ("4.18 ...")
   starts its own paragraph, so a clause is never split from its number.
3. Units are packed in order until the next would exceed CHUNK_SIZE words.
4. A unit longer than CHUNK_SIZE (a long definition) is split at sentence
   boundaries; only a single sentence longer than CHUNK_SIZE is cut by words.
   A long table is split by rows, repeating its header row in every part.
5. Overlap: the next chunk in the same section starts with the last whole sentences
   of the previous one, up to CHUNK_OVERLAP words. There is no overlap across
   sections; a heading is already a clean boundary.

"fixed" strategy: plain CHUNK_SIZE-word windows with CHUNK_OVERLAP, ignoring all
structure. It exists only to break stage B on purpose (docs/FAILURE-ANALYSIS.md).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Chunk, Document

FRONT_MATTER = "(front matter)"     # section name for text before the first heading

_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_CLAUSE = re.compile(r"^\(?(\d{1,2}(?:\.\d{1,2}){1,3})\)?\s")
_LIST_ITEM = re.compile(r"^(\(?[a-z]\)|\(?[ivx]{1,4}\)|[-•*]\s|\d{1,2}[.)]\s)")
_SENTENCE = re.compile(r"(?<=[.!?;])\s+(?=[\"'(\[]?[A-Z0-9])")


class ChunkingError(ValueError):
    """Chunk size or overlap is unusable."""


@dataclass
class _Block:
    kind: str                 # "text" or "table"
    lines: list[str]
    section: str
    page: int
    ocr: bool

    @property
    def text(self) -> str:
        if self.kind == "table":
            return "\n".join(self.lines)
        out = self.lines[0]
        for line in self.lines[1:]:
            out += ("\n" if _LIST_ITEM.match(line) else " ") + line
        return out


@dataclass
class _Unit:
    text: str
    block: int                # which block it came from: same block joins with a space
    page: int
    ocr: bool
    kind: str

    @property
    def words(self) -> int:
        return count_words(self.text)


@dataclass
class _Piece:
    section: str
    units: list[_Unit] = field(default_factory=list)
    lead: _Unit | None = None  # overlap carried from the previous chunk


def count_words(text: str) -> int:
    return sum(1 for w in text.split() if w != "|")


def parse_blocks(doc: Document) -> list[_Block]:
    """Light Markdown to blocks, each tagged with its section and page."""
    blocks: list[_Block] = []
    section = FRONT_MATTER
    for page in doc.pages:
        paragraph: list[str] = []
        table: list[str] = []

        def flush() -> None:
            if paragraph:
                blocks.append(_Block("text", paragraph.copy(), section, page.page, page.ocr))
                paragraph.clear()
            if table:
                blocks.append(_Block("table", table.copy(), section, page.page, page.ocr))
                table.clear()

        for raw in page.text.splitlines():
            line = raw.strip()
            heading = _HEADING.match(line)
            if not line:
                flush()
            elif heading:
                flush()
                section = " ".join(heading.group(2).split())[:150]
            elif line.startswith("|"):
                if paragraph:
                    blocks.append(_Block("text", paragraph.copy(), section, page.page, page.ocr))
                    paragraph.clear()
                table.append(line)
            else:
                if table:
                    blocks.append(_Block("table", table.copy(), section, page.page, page.ocr))
                    table.clear()
                if paragraph and _CLAUSE.match(line):
                    flush()
                paragraph.append(line)
        flush()
    return blocks


def source_words(doc: Document) -> int:
    """Words in the cleaned document, counted the same way as chunk words, so
    stored / source is the storage amplification reported in docs/PIPELINE.md."""
    return sum(count_words(b.text) for b in parse_blocks(doc))


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE.split(text) if s.strip()]


def _word_windows(text: str, size: int, overlap: int = 0) -> list[str]:
    """Windows of at most `size` words, each starting `size - overlap` words after the
    previous one, so a sentence too long for one chunk keeps its overlap."""
    words = text.split()
    step = max(1, size - overlap)
    windows = []
    for start in range(0, len(words), step):
        windows.append(" ".join(words[start:start + size]))
        if start + size >= len(words):
            break
    return windows


def _table_rows(header: str, row: str, size: int) -> list[str]:
    """One table row as one or more pieces that each fit `size` words with the header.
    A row longer than that is cut into word windows; the header is repeated on each
    piece when it leaves room for at least ten words of the row."""
    room = size - count_words(header)
    if count_words(row) <= room:
        return [f"{header}\n{row}"]
    if room >= 10:
        return [f"{header}\n{piece}" for piece in _word_windows(row, room)]
    return _word_windows(row, size)


def _units(block: _Block, index: int, size: int, overlap: int = 0) -> list[_Unit]:
    """Split one block into units of at most `size` words."""
    text = block.text
    if count_words(text) <= size:
        return [_Unit(text, index, block.page, block.ocr, block.kind)]
    if block.kind == "table" and count_words(block.lines[0]) > size - 10:
        # A header row too long to repeat is not a usable header: split as prose.
        return [_Unit(w, index, block.page, block.ocr, "text")
                for w in _word_windows(text, size, overlap)]
    if block.kind == "table":
        header, rows = block.lines[0], block.lines[1:]
        parts: list[_Unit] = []
        current: list[str] = []

        def emit(lines: list[str]) -> None:
            parts.append(_Unit("\n".join(lines), index, block.page, block.ocr, "table"))

        for row in rows:
            if count_words(f"{header}\n{row}") > size:      # a single oversized row
                if current:
                    emit([header, *current])
                    current = []
                for piece in _table_rows(header, row, size):
                    emit([piece])
                continue
            if current and count_words("\n".join([header, *current, row])) > size:
                emit([header, *current])
                current = []
            current.append(row)
        if current:
            emit([header, *current])
        return parts
    units: list[_Unit] = []
    for sentence in _sentences(text):
        pieces = [sentence] if count_words(sentence) <= size else \
            _word_windows(sentence, size, overlap)
        units += [_Unit(p, index, block.page, block.ocr, "text") for p in pieces]
    return units


def _overlap_tail(units: list[_Unit], limit: int) -> _Unit | None:
    """The last whole sentences of the previous chunk, at most `limit` words, taken
    across its units (a paragraph may have been split into one unit per sentence).
    Stops at a table: a table's context is carried by its repeated header instead."""
    if limit <= 0 or not units or units[-1].kind == "table":
        return None
    last = units[-1]
    tail: list[str] = []
    for unit in reversed(units):
        if unit.kind == "table":
            break
        for sentence in reversed(_sentences(unit.text)):
            if count_words(" ".join([sentence, *tail])) > limit:
                break
            tail.insert(0, sentence)
        else:
            continue
        break
    if not tail:                       # the last sentence alone is longer than the overlap
        tail = [" ".join(last.text.split()[-limit:])]
    return _Unit(" ".join(tail), last.block, last.page, last.ocr, "text")


def _pack(blocks: list[tuple[int, _Block]], size: int, overlap: int) -> list[_Piece]:
    section = blocks[0][1].section
    pieces: list[_Piece] = []
    current = _Piece(section)
    used = 0
    for index, block in blocks:
        for unit in _units(block, index, size, overlap):
            if current.units and used + unit.words > size:
                pieces.append(current)
                # The size limit wins over overlap: a unit that already fills a chunk
                # (an overlapping word window, for example) gets no extra lead.
                lead = _overlap_tail(current.units, min(overlap, size - unit.words))
                current = _Piece(section, lead=lead)
                used = lead.words if lead else 0
            current.units.append(unit)
            used += unit.words
    if current.units:
        pieces.append(current)
    return pieces


def _join(units: list[_Unit]) -> str:
    out = ""
    previous: _Unit | None = None
    for unit in units:
        if previous is None:
            out = unit.text
        else:
            out += (" " if unit.block == previous.block and unit.kind == "text" else "\n\n") \
                + unit.text
        previous = unit
    return out


def _chunk_id(source: str, doc_version: str, section: str, index: int, text: str) -> str:
    """Readable and stable: the same document and settings give the same ids."""
    slug = re.sub(r"[^a-z0-9]+", "-", Path(source).stem.lower()).strip("-")[:40] or "doc"
    digest = hashlib.sha1(f"{source}|{doc_version}|{section}|{text}".encode()).hexdigest()[:10]
    return f"{slug}-{index:04d}-{digest}"


def _clauses(text: str) -> list[str]:
    found: list[str] = []
    for line in re.split(r"\n+|(?<=[.;:])\s+(?=\(?\d)", text):
        m = _CLAUSE.match(line.strip())
        if m and m.group(1) not in found:
            found.append(m.group(1))
    return found


def _make_chunk(doc: Document, index: int, section: str, units: list[_Unit],
                lead: _Unit | None, page_end: int | None = None) -> Chunk:
    body = _join([lead, *units] if lead else units)
    extra: dict[str, str | int | bool] = {
        "chunk_index": index,
        "page_end": page_end if page_end is not None else max(u.page for u in units),
        "words": count_words(body),
        "ocr": any(u.ocr for u in units),
    }
    clauses = _clauses(body)
    if clauses:
        extra["clauses"] = ",".join(clauses)
    return Chunk(
        id=_chunk_id(doc.source, doc.doc_version, section, index, body),
        text=body,
        source=doc.source,
        section=section,
        page=units[0].page,          # where the new content starts (not the overlap)
        doc_version=doc.doc_version,
        superseded=doc.superseded,
        extra=extra,
    )


def _fixed(doc: Document, size: int, overlap: int) -> list[Chunk]:
    tagged: list[tuple[str, int, str, bool]] = []    # word, page, section, ocr
    for block in parse_blocks(doc):
        tagged += [(w, block.page, block.section, block.ocr) for w in block.text.split()]
    chunks: list[Chunk] = []
    step = size - overlap
    for start in range(0, len(tagged), step):
        window = tagged[start:start + size]
        if not window:
            break
        unit = _Unit(" ".join(w for w, *_ in window), 0, window[0][1],
                     any(ocr for *_, ocr in window), "text")
        chunks.append(_make_chunk(doc, len(chunks), window[0][2], [unit], None,
                                  page_end=window[-1][1]))
        if start + size >= len(tagged):
            break
    return chunks


def chunk(doc: Document, size: int, overlap: int, strategy: str = "structure") -> list[Chunk]:
    """Split on sections, then paragraphs, then sentences, keeping each chunk under
    `size` words, with `overlap` words shared between neighbours in a section. Every
    chunk keeps the section heading it sits under, its page, doc_version and superseded."""
    if size < 1 or overlap < 0 or overlap >= size:
        raise ChunkingError(f"need 0 <= overlap < size, got size={size} overlap={overlap}")
    if strategy == "fixed":
        return _fixed(doc, size, overlap)
    if strategy != "structure":
        raise ChunkingError(f"unknown chunk strategy {strategy!r}")
    blocks = list(enumerate(parse_blocks(doc)))
    runs: list[list[tuple[int, _Block]]] = []
    for item in blocks:
        if runs and runs[-1][-1][1].section == item[1].section:
            runs[-1].append(item)
        else:
            runs.append([item])
    chunks: list[Chunk] = []
    for run in runs:
        for piece in _pack(run, size, overlap):
            chunks.append(_make_chunk(doc, len(chunks), piece.section, piece.units, piece.lead))
    return chunks
