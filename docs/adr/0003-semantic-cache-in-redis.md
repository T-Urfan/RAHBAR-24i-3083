# ADR 0003: The semantic cache lives in Redis, keyed by index generation

## Status
Accepted · 11 November 2026

## Context
Generation is the slow, expensive step: about 10 to 20 seconds per answer on a laptop CPU,
against about 50 ms for embedding and retrieval. Students ask the same questions in
slightly different words. Section 3.3 asks for a semantic cache in Redis or a second Chroma
collection, with a threshold chosen by data, never-cache rules, and invalidation on
re-index.

## Decision
- **Redis** (`k8s/base/redis.yaml`, a Deployment with a PVC and append-only persistence).
  Entries are small hashes (the question's embedding as float32 bytes, the response JSON,
  the question) in a capped list per key space, with a TTL. Lookup is a cosine
  similarity over at most `CACHE_MAX_ENTRIES` vectors with numpy (`api/app/services/cache.py`).
- **A hit** is cosine similarity >= `CACHE_THRESHOLD` = **0.95** under the same index
  generation and the same version filter. 0.95 is the lowest threshold we tried with zero
  false hits (PIPELINE.md, "Semantic cache": "Dean's List" vs "Rector's List" scored 0.93).
- **Never cached:** follow-up questions (also never looked up), abstentions, out-of-scope
  answers and fallbacks.
- **Invalidation without coupling.** The key space includes the live collection's name.
  The indexer changes that name when it activates a new index, so the next lookup after a
  re-index starts from an empty key space, and the first lookup under the new generation
  deletes the old one's keys. The indexer never talks to Redis, which keeps the
  NetworkPolicy at "redis from api only".
- **Failure is a miss.** Redis down means every question is answered normally.

## Contract change
`AskResponse` gained an optional `cached: bool = false` (and the API sends `X-Cache`), and
`RetrievedRef` gained optional `doc_version`, `superseded` and `clauses`. All are additive:
the starter tests, the UI and `eval/run_eval.py` still work unchanged.

## Options considered
- **A second Chroma collection.** No TTL or capped lists, and every cache write would make
  the API a Chroma writer, breaking the one-writer rule of ADR 0001.
- **Redis Stack vector search.** Faster at scale, but a bigger image; at a few hundred
  entries a linear scan takes about a millisecond.
- **An exact-match cache.** Cannot serve "what's" for "what is". At 0.95 our semantic
  cache serves exactly those rewordings (0.98 to 0.999) and nothing riskier.

## Consequences
- Easier: a repeated question costs about 50 ms instead of 10 s; re-indexing can never serve
  stale answers.
- Harder: at 0.95 genuine paraphrases ("What does one credit hour cost?", 0.82) miss. On
  this corpus, sentences that differ in one fact are closer than many true paraphrases, so
  a lower threshold would serve wrong answers.
- Watch: `rahbar_cache_hit_ratio` in /metrics.
