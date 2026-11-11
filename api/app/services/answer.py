"""Turns a question into an AskResponse: the query pipeline of section 3.2.

    1 validate      AskRequest (3-500 characters), before this function runs
    2 cache check   skipped for follow-up questions          services/cache.py
    3 embed         the pinned model, search_query: prefix    services/embedding.py
    4 retrieve      top_k, superseded excluded unless asked   retrieval/chroma.py
    5 augment       instructions, numbered evidence, question services/prompt.py
    6 generate      schema-constrained, low temperature       providers/llm/
    7 validate      parse, validate, check citations, retry once, then fall back
    8 store         the turn, and a cache entry when it is safe to cache

Abstention happens twice: before the model (nothing retrieved reaches
RELEVANCE_FLOOR, so the model is never asked) and by the model (it reports
answered_from_context: false). Either way the user is told the corpus does not
cover the question, with no citations.

This function never raises for a dependency failure: an unreachable embedder,
Chroma or LLM gives the canned fallback with answered_from_context: false, never a
500 (section 3.4).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider, ProviderError
from ..retrieval.base import Retriever
from ..routes import metrics as m
from ..schemas import Answer, AskResponse, Passage, RetrievedRef
from .cache import SemanticCache, is_followup, should_cache
from .citations import check_citations
from .embedding import QueryEmbeddingError
from .prompt import ABSTAIN_TEXT
from .versioning import wants_older_version

log = logging.getLogger("rahbar.answer")

MAX_ATTEMPTS = 2            # the first try and one retry (section 3.4)
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
FALLBACK_TEXT = ("Sorry, I could not produce a reliable answer right now. Please try "
                 "again, or check the official documents directly.")


@dataclass
class _Trace:
    """What the one log line per request records (section 4.8)."""

    followup: bool = False
    include_superseded: bool = False
    cache: str = "skip"
    cache_similarity: float | None = None
    abstained: str | None = None          # floor | model
    fallback: str | None = None           # embedding | retrieval | provider | schema | citations
    attempts: int = 0
    rejected_citations: list[str] = field(default_factory=list)
    ms: dict[str, float] = field(default_factory=dict)


def _refs(passages: list[Passage]) -> list[RetrievedRef]:
    return [RetrievedRef(chunk_id=p.chunk_id, source=p.source, section=p.section, page=p.page,
                         score=p.score, doc_version=p.doc_version, superseded=p.superseded,
                         clauses=p.clauses)
            for p in passages]


def _response(answer: str, citations: list[Any], answered: bool, confidence: str,
              passages: list[Passage], provider: str, cached: bool = False) -> AskResponse:
    return AskResponse(request_id=request_id_var.get(), answer=answer, citations=citations,
                       answered_from_context=answered, confidence=confidence,  # type: ignore[arg-type]
                       retrieved=_refs(passages), provider=provider, cached=cached)


def _generation_of(retriever: Retriever) -> str | None:
    fn = getattr(retriever, "generation", None)
    if fn is None:
        return None
    try:
        return str(fn())
    except Exception:
        return None


def _generate(question: str, passages: list[Passage], provider: LLMProvider,
              trace: _Trace) -> tuple[Answer | None, list[Any]]:
    """Step 6 and 7. Returns (answer, valid citations), or (None, []) for a fallback."""
    reason: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        trace.attempts = attempt
        if reason:
            m.RETRIES.labels(reason=reason.split(":")[0]).inc()
        try:
            answer = provider.generate(question, passages, retry_reason=reason)
        except ProviderError as exc:
            log.warning("provider failed", extra={"error": str(exc)})
            trace.fallback = "provider"
            return None, []
        except ValidationError as exc:
            reason = f"schema: the reply was not valid JSON for the schema ({exc.error_count()} errors)"
            trace.fallback = "schema"
            continue
        if not answer.answered_from_context:
            trace.fallback = None
            return answer, []
        checked = check_citations(answer.citations, passages)
        trace.rejected_citations = [f"{c.source} / {c.section}" for c in checked.invalid]
        if checked.valid:
            trace.fallback = None
            return answer, checked.valid
        reason = "citations: no citation matched a passage header; copy source and section exactly"
        trace.fallback = "citations"
    return None, []


def answer_question(question: str, retriever: Retriever, provider: LLMProvider,
                    settings: Settings, cache: SemanticCache | None = None) -> AskResponse:
    m.IN_FLIGHT.inc()
    started = time.perf_counter()
    trace = _Trace(followup=is_followup(question),
                   include_superseded=wants_older_version(question))
    passages: list[Passage] = []
    response: AskResponse | None = None
    try:
        response = _answer(question, retriever, provider, settings, cache, trace, passages)
        if cache is not None:
            cache.record_turn(_turn(question, response))      # 8. store the turn
        return response
    finally:
        m.IN_FLIGHT.dec()
        trace.ms["total"] = round((time.perf_counter() - started) * 1000, 1)
        log.info("answered", extra={
            "retriever": retriever.name, "provider": provider.name, "top_k": settings.top_k,
            "chunk_ids": [p.chunk_id for p in passages],
            "scores": [round(p.score, 4) for p in passages],
            "relevance_floor": settings.relevance_floor,
            "include_superseded": trace.include_superseded, "followup": trace.followup,
            "cache": trace.cache, "cache_similarity": trace.cache_similarity,
            "abstention": trace.abstained is not None, "abstained": trace.abstained,
            "fallback": trace.fallback, "attempts": trace.attempts,
            "rejected_citations": trace.rejected_citations, "ms": trace.ms,
            "answered_from_context": response.answered_from_context if response else None,
            "confidence": response.confidence if response else None,
        })


def _answer(question: str, retriever: Retriever, provider: LLMProvider, settings: Settings,
            cache: SemanticCache | None, trace: _Trace, passages: list[Passage]) -> AskResponse:
    embedder = getattr(retriever, "embedder", None)
    vector: list[float] | None = None

    # 3. embed (first, because the cache lookup needs the vector too)
    if embedder is not None:
        t = time.perf_counter()
        try:
            vector = embedder.embed(question)
        except QueryEmbeddingError as exc:
            log.warning("embedding failed", extra={"error": str(exc)})
            return _fallback("embedding", trace, passages, provider)
        finally:
            m.EMBED_LATENCY.observe(time.perf_counter() - t)
            trace.ms["embed"] = round((time.perf_counter() - t) * 1000, 1)

    # 2. cache check
    generation = _generation_of(retriever) if cache is not None and vector is not None else None
    use_cache = (cache is not None and settings.cache_enabled and vector is not None
                 and generation is not None and not trace.followup)
    if use_cache:
        assert cache is not None and vector is not None and generation is not None
        hit = cache.lookup(vector, generation, trace.include_superseded)
        trace.cache = "hit" if hit else "miss"
        m.record_cache(trace.cache)
        if hit:
            trace.cache_similarity = round(hit.similarity, 4)
            body = {**hit.response, "request_id": request_id_var.get(), "cached": True}
            cached = AskResponse.model_validate(body)
            passages.extend(Passage(chunk_id=r.chunk_id, text="", source=r.source,
                                    section=r.section, page=r.page, score=r.score)
                            for r in cached.retrieved)
            return cached
    else:
        m.record_cache("skip")

    # 4. retrieve
    t = time.perf_counter()
    try:
        passages.extend(retriever.retrieve(question, k=settings.top_k,
                                           include_superseded=trace.include_superseded,
                                           vector=vector))
    except Exception as exc:
        log.warning("retrieval failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        return _fallback("retrieval", trace, passages, provider)
    finally:
        m.RETRIEVAL_LATENCY.observe(time.perf_counter() - t)
        trace.ms["retrieve"] = round((time.perf_counter() - t) * 1000, 1)
    if passages:
        m.TOP_SCORE.observe(passages[0].score)

    # the relevance floor: abstain without asking the model
    grounded = [p for p in passages if p.score >= settings.relevance_floor]
    if not grounded:
        return _abstain("floor", trace, passages, provider)

    # 5-7. augment, generate, validate (retry once, then fall back)
    t = time.perf_counter()
    answer, citations = _generate(question, grounded, provider, trace)
    trace.ms["generate"] = round((time.perf_counter() - t) * 1000, 1)
    if answer is None:
        return _fallback(trace.fallback or "schema", trace, passages, provider)
    # A reply that says "not covered" while flagging answered_from_context is the model
    # contradicting itself (measured: 200/30 probe p01); the text wins.
    if not answer.answered_from_context or _says_not_covered(answer.answer):
        return _abstain("model", trace, passages, provider)
    # Measured (PIPELINE.md, "Generation"): every correct golden answer came back "high";
    # every answer at "low" was a wrong answer or an invented one about an absent topic.
    if _CONFIDENCE_RANK[answer.confidence] < _CONFIDENCE_RANK[settings.min_confidence]:
        return _abstain("low_confidence", trace, passages, provider)

    response = _response(answer.answer, citations, True, answer.confidence, passages,
                         provider.name)
    # 8. a cache entry, only when it is safe to cache
    if use_cache and should_cache(response, followup=trace.followup, fallback=False):
        assert cache is not None and vector is not None and generation is not None
        cache.store(vector, generation, trace.include_superseded, question, response)
    return response


def _says_not_covered(text: str) -> bool:
    words = " ".join(text.casefold().split())
    return words.startswith(_NOT_COVERED) or words in ("", "i don't know.", "i do not know.")


_NOT_COVERED = " ".join(ABSTAIN_TEXT.casefold().split()).rstrip(".")


def _turn(question: str, response: AskResponse) -> dict[str, Any]:
    return {"request_id": response.request_id, "question": question,
            "answered_from_context": response.answered_from_context, "cached": response.cached,
            "chunk_ids": [r.chunk_id for r in response.retrieved]}


def _abstain(reason: str, trace: _Trace, passages: list[Passage],
             provider: LLMProvider) -> AskResponse:
    trace.abstained = reason
    m.ABSTENTIONS.labels(reason=reason).inc()
    return _response(ABSTAIN_TEXT, [], False, "low", passages, provider.name)


def _fallback(reason: str, trace: _Trace, passages: list[Passage],
              provider: LLMProvider) -> AskResponse:
    trace.fallback = reason
    m.FALLBACKS.labels(reason=reason).inc()
    return _response(FALLBACK_TEXT, [], False, "low", passages, provider.name)
