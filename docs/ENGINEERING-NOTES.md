# Engineering notes

Seven questions from section 7, each answered from this repository. Line numbers are as of
the final commit.

## 1. Indexing in API startup: what breaks with 3 replicas and one Chroma volume?

**The failure: three writers, one index, no owner.** With indexing in the API's startup,
`api.yaml` line 12 (`replicas: 3`) starts three indexers at once. Each extracts and embeds
the corpus independently and writes into the same Chroma collection on the one PVC
(`chroma.yaml`, `volumeClaimTemplates`). The result is an index that no single run
produced: every chunk three times (our ids are deterministic, so the second and third
writer would fail on duplicates, or with `upsert` silently overwrite each other mid-run),
or a mix of a finished run and one that died half-way. Then:

- every restart re-indexes: a rollout of 3 pods costs 3 x 208 s of OCR and embedding
  (measured locally for our corpus) before any pod can serve;
- readiness lies: either the pod is "ready" with an empty index, or startup takes minutes
  and `startupProbe` (`api.yaml` lines 44-47, 30 x 2 s = 60 s) kills it half-way, forever;
- a new document needs a redeploy.

**What prevents it:** the indexer is a **Job** (`k8s/base/indexer.yaml` line 20, `kind: Job`,
`restartPolicy: OnFailure` line 32, `backoffLimit: 2` line 26) and a weekly **CronJob**
(line 87, `concurrencyPolicy: Forbid` line 95, so two runs never overlap). The API cannot
write even by mistake: its client is a read-only proxy (`api/app/retrieval/chroma.py`,
`ReadOnlyClient`, which allows only `heartbeat`, `get_collection`, `list_collections`), and
`api/tests/test_api_readonly.py` fails if any write call appears in the API source. The
indexer builds a new collection and repoints `rahbar-active` only after verifying it
(`indexer/pipeline/store.py`, `activate`), so even one writer never exposes a half-built
index. ADR 0001 has the full reasoning.

## 2. Readiness depends on Chroma, liveness does not. Swap them, and Chroma is slow for 90 s

As configured (`api.yaml` lines 48-55): liveness `/healthz` touches nothing
(`api/app/routes/health.py` line 11, `async`, returns a constant); readiness `/readyz`
calls Chroma, checks the collection is non-empty, checks the pin and pings Redis
(`api/app/services/readiness.py`). If Chroma is slow, `/readyz` fails, each pod leaves the
Service's endpoints after 3 x 10 s, and comes back when Chroma recovers. Nobody restarts.
During the outage the Ingress has no endpoints and returns 503 quickly, which is honest.

**Swapped** (liveness on `/readyz`, readiness on `/healthz`):

- readiness always passes, so the pods stay in the Service and keep taking questions
  that hang on Chroma: users get slow failures instead of a fast 503;
- liveness fails after `failureThreshold: 3 x periodSeconds: 20` = 60 s, and the kubelet
  **kills all three pods**, because they all share the one slow dependency;
- each restart runs `startupProbe` again, then liveness fails again while Chroma is still
  slow: at 90 s of slowness that is one full restart of every replica, and with a longer
  outage it becomes CrashLoopBackOff with growing back-off. When Chroma recovers, the pods
  are in back-off, so the outage lasts longer than Chroma's own, and the restart storm
  also reconnects three clients at once.

A slow dependency became a cluster-wide outage. Readiness protects users (take me out of
rotation); liveness protects the pod (I am wedged, restart me). Only a fault inside the
process itself should fail liveness.

## 3. While the model generates, what does a CPU-based HPA measure?

The HPA (`api.yaml` lines 92-107) computes `average CPU usage / requests.cpu` (100m) across
the pods. While a question waits on Ollama, the API thread is blocked in an HTTP read:
our request log shows a typical question as `embed` 30 ms + `retrieve` 15 ms + `generate`
10,000 to 20,000 ms (`ms` field of the `answered` log line, `api/app/services/answer.py`).
More than 99% of a request's life is spent waiting, using almost no CPU. So the HPA mostly
measures JSON parsing, embedding requests and Python overhead: it can sit at 10% while
every worker thread is busy and requests are queueing. It scales late, or never, exactly
when users are waiting.

**The signal that should scale it is concurrency:** requests in flight per pod (or queue
depth, or p95 latency). The API already exports `rahbar_asks_in_flight`
(`api/app/routes/metrics.py` line 21, incremented and decremented around every question in
`answer.py`). To use it we would need Prometheus to scrape the pods and the **Prometheus
Adapter** (or KEDA) to expose it through the custom metrics API, then an HPA `type: Pods`
metric with a target such as 4 in-flight questions per pod. And since generation happens
in Ollama, more API replicas only help if Ollama has capacity: with one laptop CPU the real
bottleneck is the model, which `OLLAMA_NUM_PARALLEL=1` serialises.

## 4. The indexer's peak memory, its limit, the margin, and the exit code when it is wrong

**Measured peak:** 215 MB working set for the full corpus run with OCR and embeddings on
the local stack (`indexer/pipeline/run.py` under a `GetProcessMemoryInfo` wrapper, 208 s,
41 OCR pages); 119 MB without OCR. In the cluster the CI e2e job records `kubectl top pods
--containers` for the fixture run; that file is committed as
`docs/evidence/kubectl-top.txt`.

**Limit:** `limits.memory: 1Gi`, `requests.memory: 512Mi` (`k8s/base/indexer.yaml`). The
margin over the measured 215 MB is about 4.8x. That is deliberate, not padding: Tesseract
runs as a child process in the same cgroup and its peak depends on page size (we cap a
render at 40 Mpx, about 40 MB of greyscale per page), and a future corpus with a large
scanned document would grow the per-page peak, not the per-run total, because the
pipeline streams one document and one 16-vector batch at a time.

**If it is wrong:** over the memory limit the kernel OOM-kills the container: the pod
shows `OOMKilled`, **exit code 137** (128 + SIGKILL 9), and with `restartPolicy: OnFailure`
the Job retries up to `backoffLimit: 2`, then fails. Because the indexer only repoints the
live index after a successful build, an OOM-killed run leaves the API serving the previous
index. (A SIGTERM, for example from `kubectl delete job`, exits **143** and discards the
unfinished collection: `indexer/pipeline/run.py`, `_terminate`.)

## 5. recall@5 by question type: where is it worst, and why?

Measured on our 21-question golden set (`eval/results.json`), 400/50 chunks:

| Question type | recall@5 before hybrid step | recall@5 shipped |
| --- | --- | --- |
| exact term (4) | **0.75** | 1.00 |
| ordinary (9) | 0.94 | 0.94 |
| version (3) | 1.00 | 1.00 |
| injection (1 with relevant passages) | 1.00 | 1.00 |
| all | 0.912 | 0.971 |

**Worst: exact terms.** "What does clause 1.21 of the academic rules require?" retrieved
five Student Handbook "Code of Conduct" chunks and missed the clause entirely. An embedding
represents what text is *about*; "1.21" and "1.22" are nearly the same point in that
space, and "clause ... of the academic rules" is about rules in general. The number that
matters carries almost no weight. The same blur shows up in the cache experiment: "What
does clause 1.21 require?" and "...clause 1.22 require?" have cosine 0.876.

**The remedy is hybrid retrieval**: combine the vector search with exact keyword matching.
When a question names a clause, `ChromaRetriever.retrieve` (`api/app/retrieval/chroma.py`)
also runs the query with `where_document={"$contains": "1.21 "}` and ranks those chunks
first. Exact-term recall went from 0.75 to 1.00 with no loss elsewhere. A fuller version
would be BM25 fused with vector scores (reciprocal rank fusion); for clause numbers the
exact filter was enough.

## 6. EMBED_MODEL changed on the API only: what does the system do?

The indexer writes `embed_model` and `dimension` into every collection's metadata
(`indexer/pipeline/store.py`, `IndexBuild`, the `embed_model` field, protected from being
overwritten in `finalize`). The API's readiness compares them with its own `EMBED_MODEL`
(`api/app/services/pin.py`, `check_pin`; `readiness.py` adds `embed_model_mismatch`).

Demonstrated (`docs/evidence/pin-mismatch-readyz-503.txt`): with the index built by
`nomic-embed-text` and the API started with `EMBED_MODEL=mxbai-embed-large`, `/readyz`
returns **503** and the API logs once:

```text
"msg": "not ready", "failed": ["embed_model_mismatch"], ... "embed_model": "embedding pin
mismatch: index rahbar-v20261111t134018063650-eea2c6 was built with nomic-embed-text
(dimension 768) but EMBED_MODEL=mxbai-embed-large; refusing to be ready. Re-run the indexer
with this model, or set EMBED_MODEL back."
```

`/healthz` still returns 200, so on the cluster the pod sits at `0/1 READY`: alive, not
restarted, and not in the Service. A rolling update with the bad ConfigMap stalls with
`maxUnavailable: 0`, so the old pods keep serving.

**Without the guardrail** the API would embed questions with a different model and query
768-dimension nomic vectors with them. If the dimensions differ, Chroma errors on every
query; if they match (two 768-dimension models), nothing errors at all: cosine
similarities between unrelated spaces are noise, retrieval returns arbitrary chunks with
plausible scores, the model answers or abstains from the wrong evidence, and every
dashboard stays green.

## 7. The failure that cost us more than an hour

**Symptom.** Docker Desktop started, showed its window, and quit about twenty seconds later.
`docker version` said the daemon pipe did not exist. Nothing else.

**What I wrongly believed first.** That Docker Desktop had simply not finished starting, and
then that it wanted a first-run click (licence or sign-in). I waited, started it again, and
looked for a dialog. Then I suspected the GPU: Ollama on the same machine had just failed
with a CUDA error, so I assumed a driver problem was taking the VM down too.

**What told the truth.** Docker's own backend log, not its UI:

```text
engine linux/wsl failed to start: ... importing WSL distro: running wslexec:
The virtual machine or container JSON document is invalid.
Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_INVALID_JSON
```

and then reproducing it without Docker at all: `wsl --import` of a 3 MB Alpine root
filesystem failed with the same `HCS_E_INVALID_JSON`. `systeminfo` showed a hypervisor
running and `wsl --version` showed WSL 3.0.5 on **Windows build 21996**, a pre-release
Windows 11 build. The current WSL sends the Hyper-V compute service a VM document this old
build cannot parse. A WSL release note confirms the class of bug ("invalid JSON document
error on old Windows Insider builds"). Changing `.wslconfig` features did not help.

**What we did.** No Linux VM can start on this machine, so no Docker and no minikube with
the docker driver. Instead: the real corpus was indexed and evaluated with the same code
against native Windows processes (a Chroma 1.0.12 server from its Python package, a Windows
Redis build, Ollama on CPU), and the Kubernetes deployment runs on kind clusters in GitHub
Actions, where `ci.yml` and `cd.yml` apply the manifests, run the Job, wait for the
rollout and test through the Ingress. The minikube commands in the README are the ones
that work on a supported Windows or Linux machine. The fix on this laptop is a Windows
update to a released build.

**Second place, about an hour:** every clause in the 2026 handbooks was indexed twice, on
the wrong page. `extract_text()` looked right in spot checks. The truth came from
pdfplumber's word boxes: a whole second page of text sat outside the page's visible
box, the facing printed page stored off-page, and `extract_text()` merges it line by line. Fixed by reading only objects
inside the CropBox (`indexer/pipeline/extract.py`, `_visible_box`).
