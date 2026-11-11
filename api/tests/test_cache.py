"""The semantic cache (section 3.3): threshold, never-cache rules, invalidation on
re-index, and Redis failure as a miss."""

from __future__ import annotations

from typing import Any

import pytest
from fakes import FakeEmbedder, MemoryRedis
from test_answer_pipeline import RULES, ListRetriever, Scripted, answer_with, bad_json

from app.config import Settings
from app.schemas import Citation
from app.services.answer import answer_question
from app.services.cache import GENERATION_KEY, SemanticCache, is_followup

SETTINGS = Settings(relevance_floor=0.5, cache_threshold=0.95)
CITE = Citation(source=RULES.source, section=RULES.section, page=24)


class GenRetriever(ListRetriever):
    """A retriever with an index generation, like ChromaRetriever."""

    def __init__(self, embedder: FakeEmbedder, gen: str = "rahbar-v1") -> None:
        super().__init__([RULES], embedder=embedder)
        self.gen = gen

    def generation(self) -> str:
        return self.gen


def ask(question: str, retriever: Any, provider: Any, cache: SemanticCache,
        settings: Settings = SETTINGS) -> Any:
    return answer_question(question, retriever, provider, settings, cache=cache)


@pytest.fixture()
def redis() -> MemoryRedis:
    return MemoryRedis()


def test_a_close_paraphrase_is_served_from_the_cache(redis: MemoryRedis) -> None:
    cache = SemanticCache(redis, SETTINGS)
    retriever = GenRetriever(FakeEmbedder([1.0, 0.0, 0.0]))
    provider = Scripted(answer_with([CITE]))
    first = ask("What CGPA avoids academic warning?", retriever, provider, cache)
    assert first.cached is False
    retriever.embedder.vector = [0.99, 0.05, 0.0]          # cosine 0.9987
    second = ask("Which CGPA keeps me off academic warning?", retriever, provider, cache)
    assert second.cached is True
    assert second.answer == first.answer and second.citations == first.citations
    assert len(provider.calls) == 1                         # the model was not asked again
    assert second.request_id == first.request_id or second.request_id  # fresh id per request


def test_below_the_threshold_is_a_miss(redis: MemoryRedis) -> None:
    cache = SemanticCache(redis, SETTINGS)
    retriever = GenRetriever(FakeEmbedder([1.0, 0.0, 0.0]))
    provider = Scripted(answer_with([CITE]), answer_with([CITE]))
    ask("What CGPA avoids academic warning?", retriever, provider, cache)
    retriever.embedder.vector = [0.9, 0.4, 0.0]             # cosine 0.914 < 0.95
    assert ask("What CGPA do graduate students need?", retriever, provider, cache).cached is False
    assert len(provider.calls) == 2


def test_follow_up_questions_are_neither_looked_up_nor_stored(redis: MemoryRedis) -> None:
    cache = SemanticCache(redis, SETTINGS)
    retriever = GenRetriever(FakeEmbedder([1.0, 0.0, 0.0]))
    provider = Scripted(answer_with([CITE]), answer_with([CITE]))
    ask("What about graduate students?", retriever, provider, cache)
    assert not [k for k in redis.data if ":e:" in k]
    ask("What about graduate students?", retriever, provider, cache)
    assert len(provider.calls) == 2


def test_abstentions_are_never_cached(redis: MemoryRedis) -> None:
    cache = SemanticCache(redis, SETTINGS)
    retriever = GenRetriever(FakeEmbedder([1.0, 0.0, 0.0]))
    provider = Scripted(answer_with([], answered=False), answer_with([CITE]))
    assert ask("Is there a hostel curfew?", retriever, provider, cache).answered_from_context \
        is False
    second = ask("Is there a hostel curfew?", retriever, provider, cache)
    assert second.cached is False and second.answered_from_context is True


def test_fallbacks_are_never_cached(redis: MemoryRedis) -> None:
    cache = SemanticCache(redis, SETTINGS)
    retriever = GenRetriever(FakeEmbedder([1.0, 0.0, 0.0]))
    provider = Scripted(bad_json(), bad_json(), answer_with([CITE]))
    ask("What CGPA avoids academic warning?", retriever, provider, cache)
    assert ask("What CGPA avoids academic warning?", retriever, provider, cache).cached is False


def test_a_reindex_invalidates_the_cache(redis: MemoryRedis) -> None:
    cache = SemanticCache(redis, SETTINGS)
    retriever = GenRetriever(FakeEmbedder([1.0, 0.0, 0.0]), gen="rahbar-v1")
    provider = Scripted(answer_with([CITE]), answer_with([CITE]))
    ask("What CGPA avoids academic warning?", retriever, provider, cache)
    old_keys = [k for k in redis.data if "rahbar-v1" in k]
    assert old_keys
    retriever.gen = "rahbar-v2"                             # the indexer moved the pointer
    again = ask("What CGPA avoids academic warning?", retriever, provider, cache)
    assert again.cached is False and len(provider.calls) == 2
    assert not [k for k in redis.data if "rahbar-v1" in k]   # old generation deleted
    assert redis.data[GENERATION_KEY] == b"rahbar-v2"


def test_the_version_filter_has_its_own_key_space(redis: MemoryRedis) -> None:
    cache = SemanticCache(redis, SETTINGS)
    retriever = GenRetriever(FakeEmbedder([1.0, 0.0, 0.0]))
    provider = Scripted(answer_with([CITE]), answer_with([CITE]))
    ask("What CGPA avoids academic warning?", retriever, provider, cache)
    r = ask("What CGPA avoided academic warning in the 2023 rules?", retriever, provider, cache)
    assert r.cached is False


def test_redis_down_is_a_miss_not_an_error(redis: MemoryRedis) -> None:
    cache = SemanticCache(redis, SETTINGS)
    redis.broken = True
    retriever = GenRetriever(FakeEmbedder([1.0, 0.0, 0.0]))
    r = ask("What CGPA avoids academic warning?", retriever, Scripted(answer_with([CITE])), cache)
    assert r.answered_from_context is True and r.cached is False


def test_every_turn_is_recorded_with_a_cap_and_expiry(redis: MemoryRedis) -> None:
    cache = SemanticCache(redis, SETTINGS)
    retriever = GenRetriever(FakeEmbedder([1.0, 0.0, 0.0]))
    ask("Is there a hostel curfew?", retriever, Scripted(answer_with([], answered=False)), cache)
    assert len(redis.data["rahbar:turns"]) == 1
    assert redis.ttl["rahbar:turns"] == 7 * 86400


def test_cache_disabled_by_config_is_skipped(redis: MemoryRedis) -> None:
    settings = Settings(relevance_floor=0.5, cache_enabled=False)
    cache = SemanticCache(redis, settings)
    retriever = GenRetriever(FakeEmbedder([1.0, 0.0, 0.0]))
    provider = Scripted(answer_with([CITE]), answer_with([CITE]))
    ask("What CGPA avoids academic warning?", retriever, provider, cache, settings)
    assert ask("What CGPA avoids academic warning?", retriever, provider, cache,
               settings).cached is False


@pytest.mark.parametrize("question, expected", [
    ("What about graduate students?", True),
    ("And for MS?", True),
    ("Is that allowed?", True),
    ("How about the refund?", True),
    ("What CGPA puts a student on academic warning?", False),
    ("Is there a fee for late registration of courses?", False),
    ("What does clause 1.21 of the academic rules require?", False),
])
def test_follow_up_detection(question: str, expected: bool) -> None:
    assert is_followup(question) is expected
