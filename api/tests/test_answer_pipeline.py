"""The query pipeline (sections 3.2 and 3.4): abstention, validation with one retry,
the safe fallback, the citation check, and the version filter. Offline: a scripted
provider and an in-memory retriever stand in for the model and Chroma."""

from __future__ import annotations

from typing import Any

import pytest
from fakes import FakeEmbedder
from pydantic import ValidationError

from app.config import Settings
from app.providers.llm.base import ProviderError
from app.routes import metrics as m
from app.schemas import Answer, Citation, Passage
from app.services.answer import FALLBACK_TEXT, answer_question
from app.services.embedding import QueryEmbeddingError
from app.services.prompt import ABSTAIN_TEXT

RULES = Passage(chunk_id="rules-0019-a", text="Minimum CGPA required to avoid academic warning "
                "is 2.00.", source="academic-rules-handbook-jun-2026.pdf",
                section="Academic Warning/Probation", page=24, score=0.71,
                doc_version="2026-06", superseded=False)
FEES = Passage(chunk_id="fees-0001-b", text="| Tuition per credit hour | Rs 12,000 |",
               source="fee-structure.html", section="Fee Structure", page=1, score=0.62,
               doc_version="2026", superseded=False)
WEAK = Passage(chunk_id="report-0040-c", text="Faculty by campus.", source="annual-report.pdf",
               section="Faculty", page=40, score=0.31)


def answer_with(citations: list[Citation], answered: bool = True) -> Answer:
    return Answer(reasoning="r", answer="It is 2.00.", citations=citations,
                  answered_from_context=answered, confidence="high")


def bad_json() -> ValidationError:
    try:
        Answer.model_validate_json("this is prose, not JSON")
    except ValidationError as exc:
        return exc
    raise AssertionError("unreachable")


class Scripted:
    """Returns (or raises) the scripted replies in order; records every call."""

    name = "scripted"

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def generate(self, question: str, passages: list[Passage],
                 retry_reason: str | None = None) -> Answer:
        self.calls.append({"question": question, "passages": passages,
                           "retry_reason": retry_reason})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class ListRetriever:
    name = "list"

    def __init__(self, passages: list[Passage], embedder: Any = None,
                 error: Exception | None = None) -> None:
        self.passages = passages
        self.embedder = embedder
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, question: str, k: int, *, include_superseded: bool = False,
                 vector: list[float] | None = None) -> list[Passage]:
        self.calls.append({"k": k, "include_superseded": include_superseded, "vector": vector})
        if self.error:
            raise self.error
        return [p for p in self.passages if include_superseded or not p.superseded][:k]


SETTINGS = Settings(relevance_floor=0.5, top_k=5)


def ask(question: str, retriever: Any, provider: Any, settings: Settings = SETTINGS) -> Any:
    return answer_question(question, retriever, provider, settings)


def counter(metric: Any, **labels: str) -> float:
    return metric.labels(**labels)._value.get()


def test_answers_with_citations_that_name_retrieved_passages() -> None:
    cite = Citation(source=RULES.source, section=RULES.section, page=24)
    r = ask("What CGPA avoids academic warning?", ListRetriever([RULES, FEES]),
            Scripted(answer_with([cite])))
    assert r.answered_from_context is True
    assert r.citations == [cite]
    assert [x.chunk_id for x in r.retrieved] == [RULES.chunk_id, FEES.chunk_id]


def test_abstains_below_the_relevance_floor_without_calling_the_model() -> None:
    provider = Scripted()
    before = counter(m.ABSTENTIONS, reason="floor")
    r = ask("How do I apply for a hostel room?", ListRetriever([WEAK]), provider)
    assert r.answered_from_context is False
    assert r.citations == []
    assert r.answer == ABSTAIN_TEXT
    assert provider.calls == []                       # the model was never asked
    assert r.retrieved[0].chunk_id == WEAK.chunk_id   # still reported, for diagnosis
    assert counter(m.ABSTENTIONS, reason="floor") == before + 1


def test_only_passages_above_the_floor_reach_the_model() -> None:
    provider = Scripted(answer_with([Citation(source=RULES.source, section=RULES.section)]))
    ask("What CGPA avoids academic warning?", ListRetriever([RULES, WEAK]), provider)
    assert [p.chunk_id for p in provider.calls[0]["passages"]] == [RULES.chunk_id]


def test_the_model_abstaining_is_passed_on_with_no_citations() -> None:
    fake_cite = Citation(source=RULES.source, section=RULES.section)
    r = ask("Is there a hostel curfew?", ListRetriever([RULES]),
            Scripted(answer_with([fake_cite], answered=False)))
    assert r.answered_from_context is False
    assert r.citations == []
    assert r.answer == ABSTAIN_TEXT


def test_invalid_json_is_retried_once_with_the_reason() -> None:
    good = answer_with([Citation(source=RULES.source, section=RULES.section)])
    provider = Scripted(bad_json(), good)
    r = ask("What CGPA avoids academic warning?", ListRetriever([RULES]), provider)
    assert r.answered_from_context is True
    assert len(provider.calls) == 2
    assert provider.calls[0]["retry_reason"] is None
    assert "schema" in provider.calls[1]["retry_reason"]


def test_two_invalid_replies_give_the_fallback_not_a_500() -> None:
    before = counter(m.FALLBACKS, reason="schema")
    provider = Scripted(bad_json(), bad_json())
    r = ask("What CGPA avoids academic warning?", ListRetriever([RULES]), provider)
    assert r.answer == FALLBACK_TEXT
    assert r.answered_from_context is False and r.citations == []
    assert len(provider.calls) == 2                   # retry ONCE, then fall back
    assert counter(m.FALLBACKS, reason="schema") == before + 1


def test_fabricated_citations_are_dropped_and_real_ones_kept() -> None:
    real = Citation(source=RULES.source, section=RULES.section, page=24)
    fake = Citation(source="some-other-university.pdf", section="4.2", page=9)
    r = ask("What CGPA avoids academic warning?", ListRetriever([RULES]),
            Scripted(answer_with([fake, real])))
    assert r.citations == [real]


def test_only_fabricated_citations_trigger_a_retry_then_the_fallback() -> None:
    fake = Citation(source="handbook-2019.pdf", section="9.9")
    provider = Scripted(answer_with([fake]), answer_with([fake]))
    r = ask("What CGPA avoids academic warning?", ListRetriever([RULES]), provider)
    assert r.answer == FALLBACK_TEXT and r.citations == []
    assert "citations" in provider.calls[1]["retry_reason"]


def test_citation_with_sloppy_case_is_rewritten_to_the_exact_passage_fields() -> None:
    sloppy = Citation(source=RULES.source.upper(), section=" academic warning/probation. ",
                      page=None)
    r = ask("What CGPA avoids academic warning?", ListRetriever([RULES]),
            Scripted(answer_with([sloppy])))
    assert r.citations == [Citation(source=RULES.source, section=RULES.section, page=24)]


def test_every_returned_citation_matches_a_retrieved_chunk() -> None:
    cites = [Citation(source=RULES.source, section=RULES.section),
             Citation(source=FEES.source, section="Fee structure"),
             Citation(source="invented.pdf", section="1")]
    r = ask("What does a credit hour cost and what CGPA is needed?",
            ListRetriever([RULES, FEES]), Scripted(answer_with(cites)))
    retrieved = {(x.source, x.section) for x in r.retrieved}
    assert r.citations and all((c.source, c.section) in retrieved for c in r.citations)


@pytest.mark.parametrize("failure, reason", [
    (ProviderError("timeout"), "provider"),
])
def test_provider_failure_gives_the_fallback(failure: Exception, reason: str) -> None:
    before = counter(m.FALLBACKS, reason=reason)
    r = ask("What CGPA avoids academic warning?", ListRetriever([RULES]), Scripted(failure))
    assert r.answer == FALLBACK_TEXT and not r.answered_from_context
    assert counter(m.FALLBACKS, reason=reason) == before + 1


def test_embedding_failure_gives_the_fallback() -> None:
    retriever = ListRetriever([RULES], embedder=FakeEmbedder(error=QueryEmbeddingError("down")))
    r = ask("What CGPA avoids academic warning?", retriever, Scripted())
    assert r.answer == FALLBACK_TEXT
    assert retriever.calls == []


def test_retrieval_failure_gives_the_fallback() -> None:
    r = ask("What CGPA avoids academic warning?",
            ListRetriever([], error=ConnectionError("chroma down")), Scripted())
    assert r.answer == FALLBACK_TEXT and r.retrieved == []


def test_the_question_vector_is_computed_once_and_passed_to_the_retriever() -> None:
    embedder = FakeEmbedder([0.1, 0.2, 0.3])
    retriever = ListRetriever([RULES], embedder=embedder)
    ask("What CGPA avoids academic warning?", retriever,
        Scripted(answer_with([Citation(source=RULES.source, section=RULES.section)])))
    assert embedder.calls == ["What CGPA avoids academic warning?"]
    assert retriever.calls[0]["vector"] == [0.1, 0.2, 0.3]


OLD = Passage(chunk_id="rules-2023-0007-d", text="Minimum CGPA to avoid warning is 1.75.",
              source="undergraduate-academic-rules-aug-2023.pdf", section="Academic Warning",
              page=7, score=0.74, doc_version="2023-08", superseded=True)


def test_superseded_documents_are_excluded_by_default() -> None:
    retriever = ListRetriever([OLD, RULES])
    provider = Scripted(answer_with([Citation(source=RULES.source, section=RULES.section)]))
    r = ask("What CGPA avoids academic warning?", retriever, provider)
    assert retriever.calls[0]["include_superseded"] is False
    assert all(not x.superseded for x in r.retrieved)


def test_asking_about_the_old_version_lifts_the_filter_and_labels_it() -> None:
    retriever = ListRetriever([OLD, RULES])
    provider = Scripted(answer_with([Citation(source=OLD.source, section=OLD.section)]))
    r = ask("What did the 2023 rules say about academic warning?", retriever, provider)
    assert retriever.calls[0]["include_superseded"] is True
    assert {x.chunk_id for x in r.retrieved} == {OLD.chunk_id, RULES.chunk_id}
    assert r.retrieved[0].superseded is True and r.retrieved[0].doc_version == "2023-08"
    assert r.citations[0].source == OLD.source


def low(citations: list[Citation]) -> Answer:
    return Answer(reasoning="r", answer="Contact the hostel office.", citations=citations,
                  answered_from_context=True, confidence="low")


def test_a_low_confidence_answer_is_declined() -> None:
    before = counter(m.ABSTENTIONS, reason="low_confidence")
    r = ask("How do I apply for a hostel room?", ListRetriever([RULES]),
            Scripted(low([Citation(source=RULES.source, section=RULES.section)])))
    assert r.answered_from_context is False and r.citations == []
    assert r.answer == ABSTAIN_TEXT
    assert counter(m.ABSTENTIONS, reason="low_confidence") == before + 1


@pytest.mark.parametrize("floor, answered", [("low", True), ("medium", False), ("high", False)])
def test_min_confidence_is_configurable(floor: str, answered: bool) -> None:
    r = ask("q about warning?", ListRetriever([RULES]),
            Scripted(low([Citation(source=RULES.source, section=RULES.section)])),
            Settings(relevance_floor=0.5, min_confidence=floor))
    assert r.answered_from_context is answered
