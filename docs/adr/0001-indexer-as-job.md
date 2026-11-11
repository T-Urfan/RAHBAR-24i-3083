# ADR 0001: The indexer is a Kubernetes Job, and the API only reads

## Status
Accepted · 11 November 2026

## Context
RAHBAR has two workloads with opposite shapes. Indexing reads 279 PDF pages, OCRs 41 of
them, embeds 501 chunks and writes them to Chroma: minutes of CPU, a few hundred MB of
memory, and it only needs to happen when the documents change. Answering a question takes
one embedding, one vector query and one LLM call, on every request, from three API
replicas.

The obvious design runs indexing in the API's startup hook. With three replicas and one
Chroma volume, that breaks in four ways:

1. **Three writers race one volume.** Each replica would be half-way through a different
   document at the same moment, and the collection would end up holding an index that no
   single run produced: duplicate chunks, or chunks from a run that later failed.
2. **Every restart re-indexes.** A rolling update, an OOMKill, a node drain or a readiness
   blip would pay the full extraction, OCR and embedding cost again (208 s on a laptop).
3. **Readiness lies.** The pod is either ready before the index exists (it serves empty
   retrievals) or it takes minutes to become ready and the startup probe kills it half-way.
4. **No re-index without a redeploy.** New documents would need a rollout.

## Decision
- The indexer is its own image and runs as a **Job** (`k8s/base/indexer.yaml`) with
  `restartPolicy: OnFailure` and `backoffLimit: 2`, plus a weekly **CronJob** `reindex`
  with the same image and arguments. A manual re-index is one command with no redeploy:
  `kubectl create job --from=cronjob/reindex manual-reindex-1 -n rahbar`.
- **The API cannot write.** Its Chroma client is wrapped in a read-only proxy
  (`api/app/retrieval/chroma.py`, `ReadOnlyClient`) that raises `PermissionError` on
  `add`, `upsert`, `delete`, `modify` or `create_collection`, and a test scans the API's
  source for write calls (`api/tests/test_api_readonly.py`).
- **Safe swap.** Each run writes a new collection `rahbar-v<timestamp>-<hex>`, checks the
  stored count, and only then repoints the pointer collection `rahbar-active`
  (`indexer/pipeline/store.py`). The API follows the pointer. A failed run never changes
  what is served, and the previous index stays for `--rollback`.
- **Readiness waits for the index.** `/readyz` returns 503 until the live collection exists,
  is non-empty and was built with the same `EMBED_MODEL` (`api/app/services/readiness.py`),
  so Kubernetes withholds the pod from the Service until there is something to retrieve.

## Options considered
- **Index in API startup.** Rejected for the four reasons above.
- **An init container on the API.** Still runs once per replica and per restart (problems 1
  and 2), and it makes a rollout as slow as an index build.
- **A Deployment running an indexing loop.** Holds memory around the clock for work that
  happens weekly, and Kubernetes would restart it after it finished, which is the wrong
  life cycle. A Job's "run to completion" is the shape of the work.

## Consequences
- Easier: the API's memory and probes are sized for queries only (requests 160Mi), and the
  indexer's limit is sized from its own measured peak (ENGINEERING-NOTES.md, question 4).
- Harder: the first deployment needs a step (wait for the Job) before the API becomes
  ready; the README's apply order says so. A Job's pod template is immutable, so changing
  the image on an existing cluster means `kubectl delete job indexer` before re-applying.
- Watch: the CronJob uses `concurrencyPolicy: Forbid`, so two index runs never overlap.
