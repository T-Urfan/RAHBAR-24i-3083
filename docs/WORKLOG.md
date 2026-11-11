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
