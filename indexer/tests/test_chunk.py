"""Stage 3, chunking: structure first, sizes respected, overlap inside a section only."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from pipeline.chunk import FRONT_MATTER, ChunkingError, chunk, count_words, source_words
from pipeline.clean import clean
from pipeline.config import get_settings
from pipeline.extract import extract
from pipeline.models import Document, Page

FIXTURES = Path(__file__).resolve().parents[2] / "eval" / "fixtures"
REQUIRED = {"source", "section", "page", "doc_version", "superseded"}


def _doc(*pages: str, source: str = "rules.pdf", version: str = "2026",
         superseded: bool = False) -> Document:
    return Document(source, version, superseded,
                    [Page(source, i + 1, t) for i, t in enumerate(pages)])


def _sentences(n: int, prefix: str = "S") -> str:
    return " ".join(f"{prefix}{i} has exactly seven words here." for i in range(n))


def test_every_chunk_carries_the_required_metadata_and_an_id() -> None:
    doc = _doc("## Fees\n\nThe admission fee is Rs 30,000.", superseded=True, version="2023-08")
    [c] = chunk(doc, 100, 10)
    meta = c.metadata()
    assert REQUIRED <= set(meta)
    assert (meta["source"], meta["section"], meta["page"], meta["doc_version"],
            meta["superseded"]) == ("rules.pdf", "Fees", 1, "2023-08", True)
    assert c.id.startswith("rules-0000-")
    assert all(v is not None for v in meta.values())          # Chroma rejects None


def test_a_chunk_never_crosses_a_section_and_keeps_its_heading() -> None:
    doc = _doc("Intro before any heading.\n\n## 1. Attendance\n\n1.1 Attend 80 percent.\n\n"
               "## 2. Academic warning\n\n2.1 Below 2.00 means warning.")
    chunks = chunk(doc, 400, 50)
    assert [c.section for c in chunks] == [FRONT_MATTER, "1. Attendance", "2. Academic warning"]
    assert "80 percent" in chunks[1].text and "2.00" not in chunks[1].text


def test_sections_continue_across_pages_and_record_the_page_range() -> None:
    doc = _doc("## Registration\n\n4.1 First clause on page one.",
               "4.2 Second clause continues on page two.")
    [c] = chunk(doc, 400, 50)
    assert c.section == "Registration"
    assert (c.page, c.extra["page_end"]) == (1, 2)
    assert c.extra["clauses"] == "4.1,4.2"


def test_chunks_stay_within_the_size_limit() -> None:
    doc = _doc("## Policy\n\n" + "\n\n".join(_sentences(6, f"P{p}-") for p in range(12)))
    chunks = chunk(doc, 60, 10)
    assert len(chunks) > 3
    assert all(count_words(c.text) <= 60 for c in chunks)


def test_long_definition_is_split_at_sentence_boundaries() -> None:
    definition = _sentences(40)                    # one 280-word paragraph
    chunks = chunk(_doc("## Generative AI\n\n" + definition), 100, 15)
    assert len(chunks) >= 3
    for c in chunks:
        assert c.text.rstrip().endswith("."), c.text[-40:]
        assert re.match(r"^S\d+ has", c.text), c.text[:40]   # starts at a sentence


def test_overlap_repeats_whole_sentences_within_a_section_only() -> None:
    doc = _doc("## A\n\n" + _sentences(30, "A") + "\n\n## B\n\n" + _sentences(30, "B"))
    chunks = chunk(doc, 70, 14)
    pairs = list(zip(chunks, chunks[1:]))
    same = [(a, b) for a, b in pairs if a.section == b.section]
    assert same
    for a, b in same:
        # b's lead is the longest run of whole sentences that ends a and fits 14 words
        a_sentences = [s.strip() for s in re.split(r"(?<=\.)\s+", a.text) if s.strip()]
        lead: list[str] = []
        for s in reversed(a_sentences):
            if count_words(" ".join([s, *lead])) > 14:
                break
            lead.insert(0, s)
        expected = " ".join(lead)
        assert lead and b.text.startswith(expected), (expected, b.text[:80])
        assert count_words(expected) <= 14
    crossing = [(a, b) for a, b in pairs if a.section != b.section]
    for a, b in crossing:
        assert b.text.startswith("B0 ")                  # a new section starts clean


def test_zero_overlap_shares_nothing() -> None:
    chunks = chunk(_doc("## A\n\n" + _sentences(30)), 70, 0)
    seen: set[str] = set()
    for c in chunks:
        sentences = set(c.text.split(". "))
        assert not sentences & seen
        seen |= sentences


def test_a_numbered_clause_starts_its_own_paragraph() -> None:
    doc = _doc("## Withdrawal\n\n4.22 Fee is not refunded for a withdrawn course.\n"
               "4.23 Application for withdrawal must be countersigned.")
    [c] = chunk(doc, 400, 50)
    assert "course.\n\n4.23 Application" in c.text
    assert c.extra["clauses"] == "4.22,4.23"


def test_list_items_keep_their_own_lines() -> None:
    doc = _doc("## Rescheduling\n\n1.14 A class may be postponed as follows:\n"
               "a) only through the Academic Office,\nb) never at the teacher's discretion.")
    [c] = chunk(doc, 400, 50)
    assert ":\na) only" in c.text and ",\nb) never" in c.text


def test_a_long_table_is_split_by_rows_with_its_header_repeated() -> None:
    rows = "\n".join(f"| Charge {i} | Rs. {i},000 |" for i in range(40))
    chunks = chunk(_doc("## Miscellaneous Fees\n\n| Charge | Amount |\n" + rows), 60, 10)
    assert len(chunks) > 2
    for c in chunks:
        assert c.text.startswith("| Charge | Amount |")
        assert count_words(c.text) <= 60
    joined = "\n".join(c.text for c in chunks)
    assert all(f"| Charge {i} | Rs. {i},000 |" in joined for i in range(40))


def test_a_table_row_longer_than_the_chunk_is_split_and_keeps_its_header() -> None:
    long_row = "| Refund rule | " + " ".join(f"w{i}" for i in range(70)) + " |"
    chunks = chunk(_doc("## Refunds\n\n| Rule | Detail |\n| Short | ok |\n" + long_row), 60, 10)
    assert all(count_words(c.text) <= 60 for c in chunks)
    assert all(c.text.startswith("| Rule | Detail |") for c in chunks)
    joined = " ".join(c.text for c in chunks)
    assert all(f"w{i}" in joined.split() for i in range(70))     # nothing lost


def test_a_table_whose_header_is_too_long_is_split_as_text() -> None:
    header = "| " + " | ".join(f"h{i}" for i in range(60)) + " |"
    chunks = chunk(_doc(f"## T\n\n{header}\n| a | b |"), 50, 5)
    assert all(count_words(c.text) <= 50 for c in chunks)
    assert "h0" in chunks[0].text and "h59" in " ".join(c.text for c in chunks)


def test_a_sentence_longer_than_the_chunk_keeps_its_overlap() -> None:
    sentence = " ".join(f"w{i}" for i in range(150)) + "."
    chunks = chunk(_doc("## Definition\n\n" + sentence), 60, 10)
    assert all(count_words(c.text) <= 60 for c in chunks)
    for a, b in zip(chunks, chunks[1:]):
        assert a.text.split()[-10:] == b.text.split()[:10]       # 10 words shared


def test_ids_are_stable_across_runs_and_unique() -> None:
    doc = _doc("## A\n\n" + _sentences(30) + "\n\n## A\n\n" + _sentences(30))
    first = [c.id for c in chunk(doc, 50, 10)]
    second = [c.id for c in chunk(doc, 50, 10)]
    assert first == second
    assert len(set(first)) == len(first)          # same heading twice still gives unique ids


def test_ids_change_when_the_text_changes() -> None:
    a = chunk(_doc("## A\n\nOriginal sentence."), 50, 10)[0].id
    b = chunk(_doc("## A\n\nEdited sentence."), 50, 10)[0].id
    assert a != b


def test_ocr_pages_are_flagged_on_their_chunks() -> None:
    doc = Document("old.pdf", "2023-08", True, [Page("old.pdf", 3, "## Rules\n\nText.", ocr=True)])
    [c] = chunk(doc, 50, 10)
    assert c.extra["ocr"] is True and c.page == 3


def test_fixed_strategy_ignores_structure_for_failure_analysis() -> None:
    doc = _doc("## A\n\n" + _sentences(10, "A") + "\n\n## B\n\n" + _sentences(10, "B"))
    chunks = chunk(doc, 25, 0, strategy="fixed")
    assert all(count_words(c.text) == 25 for c in chunks[:-1])
    # the point of "fixed": a window runs straight across the section boundary
    assert any("A9 " in c.text and "B0 " in c.text for c in chunks)
    assert sum(count_words(c.text) for c in chunks) == source_words(doc)   # no overlap: 1.0x


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (10, 10), (10, -1)])
def test_invalid_sizes_are_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ChunkingError):
        chunk(_doc("text"), size, overlap)


def test_unknown_strategy_is_rejected() -> None:
    with pytest.raises(ChunkingError, match="unknown"):
        chunk(_doc("text"), 50, 5, strategy="semantic")


def test_fixture_sections_match_the_golden_set_exactly() -> None:
    """recall@k compares golden (source, section) pairs with what the chunker records;
    if these drift apart, every recall number is silently zero."""
    golden = yaml.safe_load((FIXTURES / "golden.fixture.yaml").read_text(encoding="utf-8"))
    wanted = {(r["source"], r["section"]) for q in golden["questions"] for r in q["relevant"]}
    produced = set()
    for name in ("academic-rules-2023.md", "academic-rules-2026.md", "fees-2026.md"):
        doc = clean(extract(str(FIXTURES / name), "v", False, get_settings()))
        produced |= {(c.source, c.section) for c in chunk(doc, 400, 50)}
    assert wanted <= produced, wanted - produced
