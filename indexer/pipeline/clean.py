"""Stage 2: Clean. Remove artefacts without destroying structure.

Rules, in order (each measured on the real corpus; see docs/PIPELINE.md):

1. Unicode NFKC (ligatures such as "ﬁ" become "fi"); a U+FFFD replacement character
   between two letters is an apostrophe the PDF lost ("city�s" -> "city's"); any
   other U+FFFD becomes a space, because the original glyph cannot be recovered.
2. Letter-spaced words ("S e m e s t e r") are joined back together.
3. A word hyphenated across a line break is joined ("regis-" + "tration"), except a
   real compound: a known prefix ("self-", "non-", "post-") or a capitalised first
   part ("AI-generated") keeps its hyphen.
4. Running headers and footers are removed: a line within the first or last three
   lines of a page whose text (numbers ignored) is at the page edge on at least 40% of
   the document's pages. Never a line whose sentence continues on the next line.
5. A bare page number ("14", "Page 3", "iv") as the first or last line of a page.
6. Table-of-contents runs: four or more lines ending in page numbers that never go
   down, on a page that is a contents page (a "Contents" line, or most of its lines
   are TOC-shaped) or written with dot leaders. They match many questions and answer
   none. A fee list or a table of marks on an ordinary page is data and stays.
7. A page whose text is identical to an earlier page of the same document is dropped
   (the Student Handbook repeats several pages verbatim).

Deliberately NOT stripped: clause numbers ("4.18"), "a)" list markers, ``#`` heading
lines, ``|`` table rows, footnotes, and capitalisation. Clause numbers are exactly what
a student quotes ("what does clause 1.21 say?"), headings become citations, and
tables carry the fees and grade points.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter

from .models import Document, Page

EDGE_LINES = 3              # how many lines at the top and bottom count as header/footer
REPEAT_SHARE = 0.4          # a header/footer appears on at least this share of pages
MIN_PAGES_FOR_REPEAT = 3    # ...and on at least this many pages
TOC_RUN = 4                 # consecutive "title ... 18" lines that make a TOC run
TOC_PAGE_SHARE = 0.5        # share of a page's lines that are TOC-shaped on a contents page
TOC_PAGE_MIN = 8            # ...and how many: a short list of marks is not a contents page

# Roman page numbers in these handbooks use i, v and x only; "c" or "l" alone is not one.
_PAGE_NUMBER = re.compile(r"^\s*(page\s+)?(\d{1,4}|[ivx]{1,5})(\s+of\s+\d{1,4})?\s*$", re.I)
_TOC_TAIL = re.compile(r"^(?P<title>.{3,120}?)(?P<leader>[\s.…_-]*)\s(?P<page>\d{1,3})$")
_CONTENTS = re.compile(r"^(#+\s*)?(table of )?contents\b", re.I)
_SPACED = re.compile(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b")
_HYPHEN_BREAK = re.compile(r"([A-Za-z]{2,})-\n([a-z]{2,})")
_COMPOUND_PREFIXES = {
    "self", "non", "post", "pre", "co", "anti", "multi", "inter", "intra", "semi", "sub",
    "ex", "cross", "full", "part", "well", "high", "low", "long", "short", "first",
    "second", "third", "one", "two", "three", "e", "on", "off", "under", "over", "re",
}


def _toc_entry(line: str) -> tuple[int, bool] | None:
    """(page number, has dot leader) for a TOC-shaped line ('Academic Warning/Probation
    18', 'Grading ........ 11'), or None. A one-word label with a number ('Islamabad
    73') is a table row, not a TOC entry."""
    if line.startswith(("|", "#")):
        return None
    m = _TOC_TAIL.match(line)
    if not m or not re.search(r"[A-Za-z)]$", m.group("title").rstrip()):
        return None
    leader = "..." in m.group("leader") or "…" in m.group("leader")
    words = re.findall(r"[A-Za-z]{2,}", m.group("title"))
    if len(words) >= 2 or leader:
        return int(m.group("page")), leader
    return None


def _normalise_chars(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=[A-Za-z])�(?=[A-Za-z])", "'", text)
    return re.sub(r"[ \t]*�[ \t]*", " ", text)


def _join_hyphen_break(match: re.Match[str]) -> str:
    first, second = match.group(1), match.group(2)
    if first.lower() in _COMPOUND_PREFIXES or any(ch.isupper() for ch in first):
        return f"{first}-{second}"            # self-motivation, AI-generated: a compound
    return f"{first}{second}"                 # regis-tration: a line-break hyphen


def _join_letter_spacing(line: str) -> str:
    return _SPACED.sub(lambda m: m.group(0).replace(" ", ""), line)


def _signature(line: str) -> str:
    """How a header/footer line is recognised across pages: numbers, case, spacing and
    a heading marker ignored. (On cover pages the running header is set large enough
    to be marked ``##``; it is still the same running header.) Numbers are dropped,
    not just masked: odd and even pages print the page number on opposite sides."""
    text = re.sub(r"^#+\s*", "", line.strip()).lower()
    text = re.sub(r"\d+|\b[ivx]{1,5}\b", "", text)
    return re.sub(r"\s+", "", text)


def _content_indices(lines: list[str]) -> list[int]:
    """Lines that can be a header or footer: not blank, not a table row."""
    return [i for i, ln in enumerate(lines) if ln.strip() and not ln.lstrip().startswith("|")]


def _edge_signatures(lines: list[str]) -> set[str]:
    content = [lines[i] for i in _content_indices(lines)]
    edges = content[:EDGE_LINES] + content[-EDGE_LINES:]
    return {_signature(ln) for ln in edges}


def _repeated_edges(pages: list[list[str]]) -> set[str]:
    if len(pages) < MIN_PAGES_FOR_REPEAT:
        return set()
    counts: Counter[str] = Counter()
    for lines in pages:
        counts.update(_edge_signatures(lines))
    needed = max(MIN_PAGES_FOR_REPEAT, int(REPEAT_SHARE * len(pages) + 0.999))
    return {sig for sig, n in counts.items() if n >= needed and len(sig) >= 4}


def _continues(lines: list[str], i: int) -> bool:
    """Does line i's sentence carry on into the next line? ('National University of
    Computer and Emerging Sciences' / 'endeavours to ...' is a sentence, not a header.)"""
    nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
    return bool(nxt) and nxt[0].islower()


def _is_contents_page(lines: list[str]) -> bool:
    content = [ln.strip() for ln in lines if ln.strip()]
    if any(_CONTENTS.match(ln) for ln in content):
        return True
    shaped = sum(1 for ln in content if _toc_entry(ln) is not None)
    return shaped >= TOC_PAGE_MIN and shaped / len(content) >= TOC_PAGE_SHARE


def _drop_toc_runs(lines: list[str]) -> tuple[list[str], int]:
    """Remove TOC runs (rule 6). Blank lines inside a run are kept with it, so a run
    that turns out to be data is put back exactly as it was."""
    contents_page = _is_contents_page(lines)
    out: list[str] = []
    run: list[str] = []
    pages: list[int] = []
    leaders = 0
    removed = 0

    def close() -> None:
        nonlocal removed, leaders
        is_toc = (len(pages) >= TOC_RUN and all(a <= b for a, b in zip(pages, pages[1:]))
                  and (contents_page or leaders >= TOC_RUN))
        if is_toc:
            removed += len(pages)
        else:
            out.extend(run)
        run.clear()
        pages.clear()
        leaders = 0

    for line in lines:
        entry = _toc_entry(line.strip())
        if entry is not None:
            if pages and entry[0] < pages[-1]:
                close()                  # numbers went down: a new run starts here
            run.append(line)
            pages.append(entry[0])
            leaders += entry[1]
        elif not line.strip() and run:
            run.append(line)             # blank lines inside a TOC do not break the run
        else:
            close()
            out.append(line)
    close()
    return out, removed


def _collapse(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def clean(doc: Document) -> Document:
    """Strip headers and footers that repeat on every page; keep headings and
    section boundaries, because chunking and citations depend on them."""
    stats: Counter[str] = Counter(doc.stats)
    texts = [_HYPHEN_BREAK.sub(_join_hyphen_break, _normalise_chars(p.text)) for p in doc.pages]
    split = [[_join_letter_spacing(line) for line in text.splitlines()] for text in texts]
    repeated = _repeated_edges(split)

    pages: list[Page] = []
    seen: set[str] = set()
    for page, lines in zip(doc.pages, split):
        content_idx = _content_indices(lines)
        edge_idx = set(content_idx[:EDGE_LINES] + content_idx[-EDGE_LINES:])
        first_last = {content_idx[0], content_idx[-1]} if content_idx else set()
        kept: list[str] = []
        for i, line in enumerate(lines):
            if i in edge_idx and _signature(line) in repeated and not _continues(lines, i):
                stats["header_footer_lines_removed"] += 1
                continue
            # Only the very first or last line of a page: a bare number anywhere else
            # is data (a chart value, a table cell), not a page number.
            if i in first_last and _PAGE_NUMBER.match(line) and line.strip():
                stats["page_number_lines_removed"] += 1
                continue
            kept.append(line)
        kept, toc = _drop_toc_runs(kept)
        stats["toc_lines_removed"] += toc
        text = _collapse(kept)
        digest = hashlib.sha1(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()
        if text.strip() and digest in seen:
            stats["duplicate_pages_removed"] += 1
            continue
        seen.add(digest)
        pages.append(Page(page.source, page.page, text, page.ocr))
    return Document(source=doc.source, doc_version=doc.doc_version, superseded=doc.superseded,
                    pages=pages, stats=dict(stats))
