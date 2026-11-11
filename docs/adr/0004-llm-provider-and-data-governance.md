# ADR 0004: The LLM provider is Ollama on the host; what happens to students' questions

## Status
Accepted · 11 November 2026

## Context
The development machine has 16 GB of RAM and no GPU that this Ollama build can use (its
GTX 1050 Ti driver is older than the CUDA kernels it ships). minikube needs about 6 GB.
Questions may contain personal details ("I am Ali, roll number 21I-1234, can I...").
Section 3.5 asks for a pluggable provider, section 4.7 for model output to be treated as
untrusted.

## Decision
**Provider.** `LLM_PROVIDER=ollama-host` with `llama3.2:3b` (2.0 GB). Ollama runs natively on
the Windows host on CPU (`CUDA_VISIBLE_DEVICES=-1`) and the cluster reaches it at
`host.minikube.internal:11434`. Measured on this laptop: about 25 tokens/s, 10 to 20 s per
answer. Embeddings go through the same Ollama (`nomic-embed-text`, 768 dimensions),
pinned separately by `EMBED_MODEL`.

The interface is `api/app/providers/llm/base.py` (`generate()` returns a validated
`Answer` or raises), so a swap is one file:
- `ollama.py`: `/api/chat` with `format=` set to the Answer JSON schema (constrained
  decoding), temperature from the ConfigMap;
- `hosted.py`: any OpenAI-compatible free tier with a `json_schema` response format, key
  from the `rahbar-secret` Secret, created on the command line;
- `stub.py`: deterministic and offline, for every unit test and for CI.

The retry-once-then-fallback policy and the citation check sit outside the providers
(`api/app/services/answer.py`), so they are identical for all of them.

**Data governance.**
- With `ollama-host`, a question never leaves the machine: no third party sees it.
  Choosing `hosted` sends the question and the retrieved passages (public documents) to
  that provider; say so to users before enabling it.
- Turns (request id, question, whether it was answered, chunk ids) are stored in Redis
  under `rahbar:turns`, **capped at 1,000 and expiring after 7 days**. Cached answers
  expire after `CACHE_TTL_SECONDS` (24 h). No IP addresses or user identifiers are stored.
- The API's logs carry the request id, chunk ids and scores, never the question or the
  answer text. The question travels in the POST body, which the Ingress does not log.
- The key is never logged: `Settings` hides it from `repr`, and provider errors carry the
  URL only (`api/tests/test_providers.py`).
- Model output is untrusted: it is schema-validated, its citations are checked against
  what was retrieved, it is never passed to `eval`, a shell or a query language.

## Options considered
- **`ollama-incluster`.** Needs 8 GB for minikube plus the model inside it, which leaves too
  little on a 16 GB laptop. CI does run Ollama in the cluster, for embeddings only
  (`k8s/overlays/ci/ollama.yaml`).
- **A hosted free tier.** Faster, but every student question leaves the machine, and a key
  must be managed. Implemented and tested against a mock, not the default.
- **A larger local model (7-8B).** Twice the memory and half the speed on this CPU.

## Consequences
- Easier: no key to leak; no per-request cost; questions stay local.
- Harder: a 3B model needs help. It copies clause numbers instead of headings into
  citations and over-reads a passage that merely mentions a topic. Both were found by the
  evaluation and handled in code (PIPELINE.md, "Generation").
- Watch: `rahbar_generation_seconds` and `rahbar_fallbacks_total`.
