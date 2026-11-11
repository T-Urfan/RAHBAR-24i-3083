"""Citation repair stays grounded, and the exact-term step finds a quoted clause."""

from __future__ import annotations

import pytest
from fakes import FakeChroma, FakeEmbedder, QueryableCollection

from app.config import Settings
from app.retrieval.chroma import ChromaRetriever, exact_terms
from app.schemas import Citation, Passage
from app.services.citations import check_citations

RULES = "academic-rules-handbook-jun-2026.pdf"
WARNING = Passage(chunk_id="w", text="5.4 Minimum CGPA required to avoid academic warning is "
                  "2.00.\n5.5 If at the end of a semester ...", source=RULES,
                  section="Academic Warning/Probation", page=24, score=0.8,
                  clauses=["5.4", "5.5"])
REPEAT = Passage(chunk_id="r", text="5.8 Any failed course ...", source=RULES,
                 section="Repeating a Course", page=25, score=0.7, clauses=["5.8"])
FEES = Passage(chunk_id="f", text="| Paper Rechecking Fee (Per Course/Lab) | Rs. 500 |\n"
               "| Campus Transfer Fee | Rs. 5,000 |", source="fee-structure-page.html",
               section="Miscellaneous Fees", page=1, score=0.7)


def test_a_clause_number_cited_as_the_section_maps_to_the_chunk_holding_it() -> None:
    check = check_citations([Citation(source=RULES, section="5.4", page=None)],
                            [WARNING, REPEAT])
    assert check.valid == [Citation(source=RULES, section="Academic Warning/Probation", page=24)]


def test_a_table_row_cited_as_the_section_maps_to_its_chunk() -> None:
    cite = Citation(source="fee-structure-page.html",
                    section="Paper Rechecking Fee (Per Course/Lab)")
    assert check_citations([cite], [FEES]).valid[0].section == "Miscellaneous Fees"


def test_a_clause_no_retrieved_chunk_holds_is_still_fabricated() -> None:
    check = check_citations([Citation(source=RULES, section="9.9")], [WARNING, REPEAT])
    assert check.valid == [] and len(check.invalid) == 1


def test_the_right_clause_from_the_wrong_source_is_fabricated() -> None:
    check = check_citations([Citation(source="other-university.pdf", section="5.4")], [WARNING])
    assert check.valid == []


def test_an_ambiguous_clause_matches_nothing() -> None:
    twin = WARNING.model_copy(update={"chunk_id": "w2", "section": "Other", "page": 30})
    assert check_citations([Citation(source=RULES, section="5.4")], [WARNING, twin]).valid == []


def test_short_text_is_not_matched_inside_a_passage() -> None:
    # "Fee" appears in the passage, but four characters is not evidence of anything
    assert check_citations([Citation(source="fee-structure-page.html", section="Fee")],
                           [FEES]).valid == []


@pytest.mark.parametrize("question, terms", [
    ("What does clause 1.21 of the academic rules require?", ["1.21"]),
    ("What do rules 4.18 and rule 5.4 say?", ["4.18", "5.4"]),
    ("Explain section 5.4", ["5.4"]),
    ("What CGPA of 2.00 is needed?", []),
    ("Is the fee Rs 12.000?", []),
])
def test_exact_terms(question: str, terms: list[str]) -> None:
    assert exact_terms(question) == terms


HITS = [
    {"id": "conduct-1", "text": "Students must behave ...", "distance": 0.30,
     "meta": {"source": "student-handbook.pdf", "section": "Code of Conduct", "page": 9,
              "superseded": False}},
    {"id": "conduct-2", "text": "Cheating includes ...", "distance": 0.31,
     "meta": {"source": "student-handbook.pdf", "section": "Code of Conduct", "page": 10,
              "superseded": False}},
    {"id": "attendance", "text": "1.21 Students are required to maintain 100% attendance ",
     "distance": 0.40, "meta": {"source": RULES, "section": "Attendance Requirements",
                                "page": 10, "superseded": False, "clauses": "1.21,1.22"}},
    {"id": "attendance-2023", "text": "1.21 Students are required to maintain 100% ",
     "distance": 0.39, "meta": {"source": "rules-2023.pdf", "section": "1. Introduction",
                                "page": 7, "superseded": True, "clauses": "1.21"}},
]


def retriever() -> ChromaRetriever:
    chroma = FakeChroma()
    chroma.publish(active="rahbar-v1")
    chroma.collections["rahbar-v1"] = QueryableCollection(  # type: ignore[assignment]
        "rahbar-v1", HITS, chroma.collections["rahbar-v1"].metadata)
    return ChromaRetriever(Settings(), client_factory=lambda s: chroma, embedder=FakeEmbedder())


def test_a_quoted_clause_is_fetched_and_ranked_first() -> None:
    passages = retriever().retrieve("What does clause 1.21 of the academic rules require?", k=2)
    assert [p.chunk_id for p in passages] == ["attendance", "conduct-1"]


def test_the_exact_term_step_respects_the_superseded_filter() -> None:
    ids = [p.chunk_id for p in retriever().retrieve("What does clause 1.21 say?", k=4)]
    assert "attendance-2023" not in ids


def test_no_clause_means_plain_vector_order() -> None:
    passages = retriever().retrieve("What are the rules of conduct?", k=2)
    assert [p.chunk_id for p in passages] == ["conduct-1", "conduct-2"]
