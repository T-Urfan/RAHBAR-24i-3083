# Work log

A running record of what changed, which commands were run, and what the tests said.
Solo project: every commit is by the repository owner. AI assistance is disclosed in
[AI-USAGE.md](AI-USAGE.md); entries marked *(AI-assisted)* were written with Claude Code
and reviewed before committing.

## 2026-11-11 · P0 · Environment and repository bootstrap *(AI-assisted)*

**Inspected:** no `.git`; `gh` CLI, minikube and Ollama not installed; Docker daemon not
running; Python 3.12.10 present; 15.9 GB RAM; C: 32 GB free (Docker and Ollama data go to E:).

**Commands run**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r api/requirements-dev.txt -r indexer/requirements-dev.txt
cd api;     ..\.venv\Scripts\python.exe -m pytest -q      # 7 passed   (starter baseline)
cd indexer; ..\.venv\Scripts\python.exe -m pytest -q      # 4 passed   (starter baseline)
ruff check api indexer eval corpus; mypy app; mypy pipeline   # all clean
git init -b main
git add -A -- . ':(exclude)*.pdf'
git commit -m "chore: import RAHBAR starter kit"          # 7dfd415, verbatim starter, no PDF
git checkout -b dev
```

**Files**

| File | Change | Why |
| --- | --- | --- |
| `.gitattributes` | new | Force LF in the repo; CRLF breaks scripts inside Linux containers |
| `.gitignore` | edited | Exclude the assignment handout PDF and coverage output |
| `scripts/dev.ps1` | new | PowerShell versions of the Makefile targets (no `make` on Windows) |
| `api/requirements-dev.txt`, `indexer/requirements-dev.txt` | edited | `pytest-cov==6.1.1` for the 60% coverage gate |

**Pending, done by the owner:** install `gh` and run `gh auth login --web`; install minikube and
Ollama; set the Ollama environment variables; pull `nomic-embed-text` and `llama3.2:3b`;
start Docker Desktop with its disk image on E:. No remote exists yet; nothing has been pushed.

## 2026-11-11 · P1 · Index contract, API foundations, resilience *(AI-assisted)*

**Verified in the installed chromadb-client 1.0.12 source before designing**

- `chromadb.HttpClient()` contacts the server inside its constructor and raises
  `ValueError("Could not connect to a Chroma server...")` when it is down
  (`chromadb/api/client.py`, `Client.__init__` → `_validate_tenant_database`).
  Building it at API startup would crash the API whenever Chroma is late.
- `collection.modify(metadata=...)` replaces the metadata dict and rejects `hnsw:space`.
- `get_collection` / `create_collection` default to Chroma's ONNX embedding function;
  we always pass `embedding_function=None` and supply our own vectors.

**Files**

| File | Change |
| --- | --- |
| `indexer/pipeline/store.py` | Implemented: build a new `rahbar-v<version>` collection, verify it, repoint `rahbar-active`, keep the previous one, garbage-collect older ones, `rollback()` |
| `indexer/pipeline/dummy.py` | Uses the same build-then-activate path; never deletes the live index |
| `indexer/pipeline/run.py` | `--rollback`; refused activation exits 1 and leaves the live index unchanged |
| `indexer/pipeline/log.py` | new: JSON log helper, moved out of `run.py` |
| `api/app/config.py` | Empty values mean default; bad values raise `ConfigError` naming the variable; new keys `REDIS_HOST`, `REDIS_PORT`, `POINTER_TTL_SECONDS`, `READINESS_TTL_SECONDS` |
| `api/app/retrieval/chroma.py` | Lazy client, reconnect after errors, read-only proxy, pointer resolution, `describe()`; `retrieve` still a stub (P6) |
| `api/app/services/pin.py` | new: embedding-pin check |
| `api/app/services/readiness.py` | new: Chroma, non-empty collection, pin, Redis; 503 names every failure; TTL cache; logs transitions |
| `api/app/routes/health.py` | `/healthz` is `async`; `/readyz` is real |
| `api/app/main.py` | Builds Redis client and readiness checker without connecting; startup pin check logged, never fatal |
| `indexer/tests/test_starter_dummy.py` | Starter test adapted: fake client moved to `fakes.py`, collection found via the pointer; assertions unchanged |
| `api/tests/*`, `indexer/tests/*` | new: fakes, conftest, 7 new test modules |

**Bug found by the new tests:** two index builds within one Windows clock tick produced the
same collection name. Fixed by adding a random suffix to `index_version`.

**Results**

```text
api      43 passed   coverage 92.64%  (gate 60%)
indexer  15 passed   coverage 84%
ruff     All checks passed!
mypy     api: no issues in 23 files · indexer: no issues in 11 files
checker  53 ok · 12 warn · 49 FAIL   (audit: 44 ok · 11 warn · 53 FAIL)
```

## 2026-11-11 · P4 + P5 · Corpus and the indexing subsystem *(AI-assisted)*

**Environment:** the repo-local Git identity is set to T-Urfan
(`188089897+T-Urfan@users.noreply.github.com`; the global config is unchanged). The P1 work
was committed as five local commits. The attempt to install Ollama and Tesseract with
winget failed: winget cannot run from the agent's sandbox, and running it outside the
sandbox was refused by the permission system. **Both still need installing by the owner.**
The `OLLAMA_*` user environment variables were set.

**Corpus:** `corpus/sources.yaml` (from the instructor's list, unchanged), 9 files fetched,
`corpus/MANIFEST.md` written. Profiled: 279 PDF pages; the Aug 2023 rules are 24/26 pages
without a text layer (vector outlines), so OCR is essential for the version pair.

**Files**

| File | Change |
| --- | --- |
| `indexer/pipeline/extract.py` | Implemented: visible-box filter, column/gutter layout, ruled tables, size/weight headings, pdfium text-layer check, Tesseract OCR with pixel cap and timeout, HTML (nested tables, colspan, omitted end tags) and Markdown |
| `indexer/pipeline/clean.py` | Implemented: 7 rules (see PIPELINE.md "Cleaning") |
| `indexer/pipeline/chunk.py` | Implemented: structure-aware chunking with sentence overlap; `fixed` strategy for failure analysis |
| `indexer/pipeline/embed.py` | Implemented: batched Ollama `/api/embed`, prefixes, validation, retries |
| `indexer/pipeline/run.py` | Streaming `run_pipeline`; bad-document policy; SIGTERM → exit 143; safe `_index.json` paths |
| `indexer/pipeline/store.py` | `finalize`, `discard` (never after activation), duplicate-id check, exact-pattern gc that keeps newer builds, µs-ordered versions |
| `indexer/pipeline/config.py`, `models.py` | New settings with validation; `Chunk.extra` metadata |
| `indexer/requirements*.txt` | `pdf2image` removed (pdfium renders); `fpdf2` added for test PDFs |
| `indexer/tests/*` | `pdfgen.py` plus 6 new test modules; the fake Chroma client made faithful to chromadb 1.0.12 |
| `docs/CORPUS.md`, `docs/PIPELINE.md`, `docs/AI-USAGE.md` | Written from measurements |

**Bugs found on the real corpus, before the tests:** hidden off-page text (every handbook
clause duplicated), a missed 9 pt gutter, 183 chart values deleted as "page numbers",
running headers marked as headings, and 118 s spent parsing vector outlines.

**Independent review (two read-only agents):** 14 + 14 findings. Fixed: the confirmed
critical one (a failure after activation could delete the live collection), gc prefix
matching, header removal deleting a sentence, hyphenated compounds, TOC rule deleting fee
lists and grade weights, HTML table losses, false headings, short overlap, oversized table
rows, SIGTERM cleanup, OCR timeout and pixel cap, `_index.json` path traversal, faithful
fakes, test environment isolation. Each has a regression test.

**Results**

```text
indexer  127 passed, 1 skipped (real OCR: tesseract not installed)   coverage 96%
api       43 passed                                                   coverage 92.64%
ruff     All checks passed!     mypy: no issues (api 23 files, indexer 11 files)
corpus   469 chunks · 57,427 source words · amplification 1.024 · peak memory 119 MB
```

## 2026-11-11 · P2-P13 · Query pipeline, cache, evaluation, images, Kubernetes, CI/CD *(AI-assisted)*

**Environment:** Ollama 0.40.16 (CPU: the GTX 1050 Ti driver is too old for its CUDA build,
so `CUDA_VISIBLE_DEVICES=-1`), Tesseract 5.4, minikube, gh, kubeconform, a Chroma 1.0.12
server and Redis, all under `E:\tools`. **Docker Desktop and minikube cannot run here:** this
is Windows build 21996 (pre-release) and current WSL fails to create any VM
(`HCS_E_INVALID_JSON`, reproduced with `wsl --import`). The real corpus was indexed and
evaluated against native processes; the cluster runs on kind in GitHub Actions.

**Files:** `api/app/services/{embedding,versioning,prompt,citations,cache,answer}.py`,
`api/app/providers/llm/{base,ollama,hosted,stub}.py`, `api/app/retrieval/chroma.py`,
`api/app/routes/{ask,metrics}.py`, `eval/run_eval.py`, `eval/golden.yaml`,
`eval/abstention_probes.yaml`, `eval/{baseline,results}.json`, `eval/fixtures/baseline.json`,
three Dockerfiles, `k8s/base/*`, `k8s/overlays/{dev,prod,ci}`, `.github/workflows/{ci,cd}.yml`,
all docs.

**Runs (each recorded in PIPELINE.md and FAILURE-ANALYSIS.md):**

```text
index, full corpus, OCR       501 chunks, 41 OCR pages, 208 s, peak 215 MB
retrieval only, 400/50        recall@5 0.912 (v1) -> 0.971 (clause lookup)
chunking 200/30, 400/50, 600/80   recall@5 1.000 / 0.971 / 0.971; probes 8/10 vs 10/10 -> ship 400/50
full eval v1 -> v3            fallbacks 4 -> 0; abstention 75% -> 100%; probes 10/10
cache threshold               near-miss pairs up to 0.930 -> ship 0.95
failure stages A, B, C, E, F  see FAILURE-ANALYSIS.md
api 139 passed (98%) · indexer 128 passed · eval 11 passed · kubeconform 30/30/33 valid
```
