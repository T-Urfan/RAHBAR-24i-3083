"""The semantic cache (section 3.3), in Redis (ADR 0003).

A hit is a stored question whose embedding has cosine similarity >= CACHE_THRESHOLD
with the new question's embedding, asked under the same index generation and the
same version filter. The threshold comes from the ConfigMap and was chosen by data
(docs/PIPELINE.md, "Semantic cache").

Never cached (a cached mistake is served to everyone):
- follow-up questions ("what about graduate students?"), which depend on a previous
  turn the cache cannot see; they skip the lookup as well;
- abstentions and out-of-scope answers (answered_from_context: false);
- fallbacks and anything that failed validation.

Invalidation on re-index: every key carries the index generation, which is the name
of the live collection behind the ``rahbar-active`` pointer. The indexer Job changes
that name when it activates a new index, so the API's next lookup uses a new key
space and nothing from the old index is ever served. The first lookup that sees a
new generation also deletes the old generation's keys (``bump``). The indexer never
needs network access to Redis for this, which keeps the NetworkPolicy at
"redis from api only".

Redis failing never fails a question: a broken cache is a miss.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..config import Settings
from ..schemas import AskResponse

log = logging.getLogger("rahbar.cache")

PREFIX = "rahbar:cache"
GENERATION_KEY = f"{PREFIX}:generation"
TURNS_KEY = "rahbar:turns"
TURNS_KEPT = 1000
TURNS_TTL_SECONDS = 7 * 86400      # ADR 0004: questions are kept 7 days, then expire

_FOLLOWUP_START = re.compile(
    r"^\s*(and|but|also|so|then|what about|how about|and what|and how|same for|"
    r"what if (it|they|that)|why not|is that|does that|can they|can it)\b", re.I)
_DANGLING = re.compile(r"\b(it|that|this|those|these|they|them|its|their|there|he|she)\b",
                       re.I)


def is_followup(question: str) -> bool:
    """A question that only makes sense after a previous turn. A short question that
    leans on a pronoun ("is that allowed?") or starts like a continuation ("what about
    MS students?") is a follow-up; caching it would hand another user an answer to a
    question they never asked."""
    if _FOLLOWUP_START.search(question):
        return True
    words = re.findall(r"[A-Za-z0-9']+", question)
    return len(words) <= 6 and bool(_DANGLING.search(question))


def should_cache(response: AskResponse, *, followup: bool, fallback: bool) -> bool:
    return response.answered_from_context and not followup and not fallback \
        and bool(response.citations)


@dataclass(frozen=True)
class CacheHit:
    response: dict[str, Any]
    similarity: float
    question: str


class SemanticCache:
    def __init__(self, redis_client: Any, settings: Settings) -> None:
        self.redis = redis_client
        self.threshold = settings.cache_threshold
        self.ttl = settings.cache_ttl_seconds
        self.max_entries = settings.cache_max_entries
        self._generation_seen: str | None = None

    @staticmethod
    def _prefix(generation: str, include_superseded: bool) -> str:
        return f"{PREFIX}:{generation}:{'all' if include_superseded else 'current'}"

    def bump(self, generation: str) -> None:
        """Delete every entry from older index generations, once per new generation."""
        if generation == self._generation_seen:
            return
        stored = self.redis.get(GENERATION_KEY)
        stored = stored.decode() if isinstance(stored, bytes) else stored
        if stored != generation:
            keep = f"{PREFIX}:{generation}:"
            stale = [k for k in self.redis.scan_iter(match=f"{PREFIX}:*", count=500)
                     if not _key(k).startswith(keep) and _key(k) != GENERATION_KEY]
            if stale:
                self.redis.unlink(*stale)       # non-blocking delete
            self.redis.set(GENERATION_KEY, generation)
            log.info("cache invalidated for a new index",
                     extra={"generation": generation, "previous": stored,
                            "deleted_keys": len(stale)})
        self._generation_seen = generation

    def lookup(self, vector: list[float], generation: str,
               include_superseded: bool) -> CacheHit | None:
        try:
            self.bump(generation)
            prefix = self._prefix(generation, include_superseded)
            ids = self.redis.lrange(f"{prefix}:ids", 0, self.max_entries - 1)
            if not ids:
                return None
            pipe = self.redis.pipeline()
            for entry_id in ids:
                pipe.hmget(f"{prefix}:e:{_key(entry_id)}", "vec", "resp", "q")
            rows = [r for r in pipe.execute() if r and r[0] and r[1]]
            if not rows:
                return None
            matrix = np.frombuffer(b"".join(r[0] for r in rows),
                                   dtype=np.float32).reshape(len(rows), -1)
            query = np.asarray(vector, dtype=np.float32)
            if matrix.shape[1] != query.shape[0]:
                return None
            sims = matrix @ query / (np.linalg.norm(matrix, axis=1) * np.linalg.norm(query))
            best = int(np.argmax(sims))
            similarity = float(sims[best])
            if similarity < self.threshold:
                return None
            row = rows[best]
            return CacheHit(json.loads(row[1]), similarity, _key(row[2] or b""))
        except Exception as exc:          # a broken cache is a miss, never a 500
            log.warning("cache lookup failed", extra={"error": f"{type(exc).__name__}: {exc}"})
            return None

    def store(self, vector: list[float], generation: str, include_superseded: bool,
              question: str, response: AskResponse) -> None:
        try:
            prefix = self._prefix(generation, include_superseded)
            entry_id = uuid.uuid4().hex
            body = response.model_dump(exclude={"request_id"})
            pipe = self.redis.pipeline()
            pipe.hset(f"{prefix}:e:{entry_id}", mapping={
                "vec": np.asarray(vector, dtype=np.float32).tobytes(),
                "resp": json.dumps(body), "q": question})
            pipe.expire(f"{prefix}:e:{entry_id}", self.ttl)
            pipe.lpush(f"{prefix}:ids", entry_id)
            pipe.ltrim(f"{prefix}:ids", 0, self.max_entries - 1)
            pipe.expire(f"{prefix}:ids", self.ttl)
            pipe.execute()
        except Exception as exc:
            log.warning("cache store failed", extra={"error": f"{type(exc).__name__}: {exc}"})

    def record_turn(self, turn: dict[str, Any]) -> None:
        """Section 3.2 step 8: store the turn. Capped and expiring (ADR 0004)."""
        try:
            pipe = self.redis.pipeline()
            pipe.lpush(TURNS_KEY, json.dumps({"ts": round(time.time(), 3), **turn}))
            pipe.ltrim(TURNS_KEY, 0, TURNS_KEPT - 1)
            pipe.expire(TURNS_KEY, TURNS_TTL_SECONDS)
            pipe.execute()
        except Exception as exc:
            log.warning("turn store failed", extra={"error": f"{type(exc).__name__}: {exc}"})


def _key(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
