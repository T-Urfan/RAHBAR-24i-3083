"""Prometheus metrics at /metrics (section 4.8).

Request count and latency by route, plus the pipeline's own signals: retrieval,
embedding and generation latency, cache lookups (hit ratio = hits / lookups),
abstentions, fallbacks, tokens in and out per request, and requests in flight (the
signal that should scale a generation service; see ENGINEERING-NOTES.md question 3).
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

router = APIRouter()

_SECONDS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)

REQUESTS = Counter("rahbar_requests_total", "HTTP requests", ["path", "status"])
LATENCY = Histogram("rahbar_request_seconds", "HTTP request latency", ["path"],
                    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60))
IN_FLIGHT = Gauge("rahbar_asks_in_flight", "Questions being answered right now")

EMBED_LATENCY = Histogram("rahbar_embed_seconds", "Question embedding latency",
                          buckets=_SECONDS)
RETRIEVAL_LATENCY = Histogram("rahbar_retrieval_seconds", "Chroma query latency",
                              buckets=_SECONDS)
GENERATION_LATENCY = Histogram("rahbar_generation_seconds", "LLM call latency, per attempt",
                               ["provider"], buckets=_SECONDS)
TOP_SCORE = Histogram("rahbar_retrieval_top_score", "Similarity of the best passage",
                      buckets=(0.2, 0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.9, 1.0))

CACHE = Counter("rahbar_cache_lookups_total", "Semantic cache lookups", ["result"])
CACHE_HIT_RATIO = Gauge("rahbar_cache_hit_ratio", "Cache hits / lookups since start")
ABSTENTIONS = Counter("rahbar_abstentions_total", "Questions declined", ["reason"])
FALLBACKS = Counter("rahbar_fallbacks_total", "Canned fallback answers", ["reason"])
RETRIES = Counter("rahbar_generation_retries_total", "Generation retries after a rejection",
                  ["reason"])
TOKENS = Histogram("rahbar_tokens_per_request", "LLM tokens per request", ["direction"],
                   buckets=(16, 64, 128, 256, 512, 1024, 2048, 4096, 8192))

_cache_counts = {"hit": 0, "lookups": 0}


def record_cache(result: str) -> None:
    """result: hit, miss or skip. A skip (follow-up, no Redis) is not a lookup."""
    CACHE.labels(result=result).inc()
    if result == "skip":
        return
    _cache_counts["lookups"] += 1
    _cache_counts["hit"] += result == "hit"
    CACHE_HIT_RATIO.set(_cache_counts["hit"] / _cache_counts["lookups"])


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
