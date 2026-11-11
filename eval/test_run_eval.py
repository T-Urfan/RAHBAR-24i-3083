"""The evaluation is the CI gate, so it is tested like code. Run: pytest eval"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_eval as ev  # noqa: E402

RULES = "academic-rules-handbook-jun-2026.pdf"


def ref(source: str, section: str, page: int = 1, clauses: list[str] | None = None) -> dict:
    return {"chunk_id": "x", "source": source, "section": section, "page": page,
            "score": 0.7, "clauses": clauses or []}


def test_recall_matches_by_section_prefix_and_by_clause() -> None:
    relevant = [{"source": RULES, "section": "5.4", "clause": "5.4"},
                {"source": "fee-structure-page.html", "section": "Tuition Fee"}]
    retrieved = [ref(RULES, "Academic Warning/Probation", 24, ["5.4", "5.5"]),
                 ref("x.pdf", "a"), ref("y.pdf", "b"),
                 ref("fee-structure-page.html", "Tuition Fee (w.e.f. Fall 2026 semester)")]
    assert ev.recall_at_k(relevant[:1], retrieved, 1) == 1.0
    assert ev.recall_at_k(relevant, retrieved, 3) == 0.5
    assert ev.recall_at_k(relevant, retrieved, 5) == 1.0


def test_recall_requires_the_same_source() -> None:
    relevant = [{"source": RULES, "section": "Withdrawal from a Course", "clause": "4.25"}]
    old = ref("undergraduate-academic-rules-aug-2023.pdf", "Withdrawal from a Course", 15,
              ["4.25"])
    assert ev.recall_at_k(relevant, [old], 5) == 0.0


def test_recall_is_none_for_must_abstain() -> None:
    assert ev.recall_at_k([], [ref(RULES, "x")], 5) is None


def test_section_matching_ignores_case_quotes_and_trailing_dot() -> None:
    golden = {"source": RULES, "section": "Dean’s List of Honors."}
    assert ev.passage_matches(golden, ref(RULES, "dean's list of honors"))


def test_citation_validity() -> None:
    retrieved = [ref(RULES, "Attendance Requirements", 10)]
    good = {"source": RULES, "section": "Attendance Requirements", "page": 10}
    wrong_page = {"source": RULES, "section": "Attendance Requirements", "page": 99}
    invented = {"source": "other.pdf", "section": "4.2", "page": 1}
    assert ev.citation_validity({"citations": [good], "retrieved": retrieved}) == 1.0
    assert ev.citation_validity({"citations": [good, invented, wrong_page],
                                 "retrieved": retrieved}) == 1 / 3
    assert ev.citation_validity({"citations": [], "retrieved": retrieved}) is None


def row(qid: str, qtype: str, relevant: list, response: dict, must_abstain: bool = False,
        expect: str | None = None) -> dict:
    return {"id": qid, "type": qtype, "must_abstain": must_abstain, "relevant": relevant,
            "expect": expect, "response": response, "seconds": 1.0}


def answered(retrieved: list[dict], citations: list[dict], text: str = "It is 2.00.") -> dict:
    return {"answer": text, "citations": citations, "answered_from_context": True,
            "retrieved": retrieved}


def declined(retrieved: list[dict]) -> dict:
    return {"answer": "not covered", "citations": [], "answered_from_context": False,
            "retrieved": retrieved}


def sample_rows() -> list[dict]:
    hit = ref(RULES, "Academic Warning/Probation", 24, ["5.4"])
    miss = ref("annual-report-2019-20.pdf", "Faculty")
    cite = {"source": RULES, "section": "Academic Warning/Probation", "page": 24}
    return [
        row("q1", "ordinary", [{"source": RULES, "section": "Academic Warning"}],
            answered([hit], [cite]), expect="2.00"),
        row("q2", "exact", [{"source": RULES, "section": "x", "clause": "1.21"}],
            answered([miss], [{"source": "invented.pdf", "section": "1.21"}])),
        row("q3", "abstain", [], declined([miss]), must_abstain=True),
    ]


def test_summarise_reports_overall_and_by_type() -> None:
    s = ev.summarise(sample_rows())
    assert s["recall@5"] == 0.5 and s["recall@3"] == 0.5
    assert s["recall@5_by_type"] == {"exact": {"recall@5": 0.0, "questions": 1},
                                     "ordinary": {"recall@5": 1.0, "questions": 1}}
    assert s["citation_validity"] == 0.5 and s["citations"] == 2
    assert s["abstention_correctness"] == 1.0
    assert s["expect_found_rate"] == 1.0
    assert [q["id"] for q in s["per_question"]] == ["q1", "q2", "q3"]


def test_gate_passes_a_clean_run() -> None:
    results = {"recall@5": 0.8, "abstention_correctness": 1.0, "citation_validity": 1.0}
    assert ev.gate(results, {"recall@5": 0.82}) == []


def test_gate_fails_on_a_recall_drop_of_more_than_five_points() -> None:
    results = {"recall@5": 0.76, "abstention_correctness": 1.0, "citation_validity": 1.0}
    assert ev.gate(results, {"recall@5": 0.80}) == []                 # 4 points: allowed
    failures = ev.gate({**results, "recall@5": 0.74}, {"recall@5": 0.80})
    assert len(failures) == 1 and "recall@5" in failures[0]


def test_gate_fails_when_any_must_abstain_question_is_answered() -> None:
    rows = sample_rows()
    rows[2]["response"] = answered([], [])
    failures = ev.gate(ev.summarise(rows), None)
    assert any("abstention" in f and "q3" in f for f in failures)


def test_gate_fails_below_95_percent_citation_validity() -> None:
    failures = ev.gate(ev.summarise(sample_rows()), None)
    assert any("citation validity" in f for f in failures)


def test_no_baseline_means_no_recall_gate() -> None:
    assert ev.gate({"recall@5": 0.1, "abstention_correctness": 1.0,
                    "citation_validity": 1.0}, None) == []
