# AI usage

This is a solo project. One AI tool was used throughout: **Claude Code** (Anthropic), running
Claude Opus 5.5 and, briefly, Claude Haiku 4.5, as a coding agent in the terminal and VS Code. It
read the handout and the starter kit, wrote code, ran commands and tests on the development
machine, and drafted documents. Every commit that contains its work carries a
`Co-Authored-By: Claude` trailer, so `git log` shows exactly which commits it shaped. The
only commit without one is the unmodified starter kit.

| Phase | What the AI wrote or shaped | What was changed afterwards, and why |
| --- | --- | --- |
| Audit and plan | A read-only audit of the repository and handout, and the P0-P13 plan | Re-scoped by the owner: solo work, native Windows tooling, Ollama on the host, local commits only until approved, later full autonomy to finish |
| P1 · index contract | Versioned collections and the `rahbar-active` pointer (`indexer/pipeline/store.py`), the lazy read-only Chroma client, `/readyz`, the embedding-pin check, their tests | A bug its own tests found (two builds in one clock tick got the same name) was fixed with a random suffix, then a strictly increasing timestamp |
| P4-P5 · corpus and indexer | `extract.py`, `clean.py`, `chunk.py`, `embed.py`, the streaming `run_pipeline`, the indexer tests, first drafts of CORPUS.md and PIPELINE.md | Two independent read-only review agents (test quality, correctness) found 28 issues; the confirmed ones were fixed, each with a regression test. The most serious: a failure after activation could delete the live index |
| P6-P7 · query pipeline and cache | Query embedding, version-aware retrieval, the prompt, the Ollama and hosted providers, validation with retry and fallback, the citation check, the Redis semantic cache, metrics, and their tests | Changed after measuring the real system: citation repair by clause number (four fallbacks in the first run), the clause keyword step (exact-term recall 0.75), a stricter abstain rule and the confidence gate (a hostel question answered from "a hostel exists") |
| P8 · evaluation | `recall_at_k`, `citation_validity`, `summarise`, `gate` and their tests; the golden set drafted from the extracted document text; the probe set; all measurement runs | The golden set was committed before any tuning (commit `f2a0696`), and later fixes never edited it; the probes were added in a separate file with the reason |
| P2-P3, P9-P10 · images, manifests, CI/CD | The three Dockerfiles, every manifest and overlay, `ci.yml`, `cd.yml` | Validated with `kubeconform -strict`; the YAML anchors that crossed documents were written out after review |
| P11-P13 · evidence and docs | ADRs, ARCHITECTURE.md, ENGINEERING-NOTES.md, FAILURE-ANALYSIS.md, README, this file, the viva guide | One claim drafted before it was measured (the 200/30 recall in ADR 0002) was removed before commit. Every number in the documents comes from a run recorded in WORKLOG.md |

**Where the AI was wrong, and how it was caught:** off-page text it did not expect in the
PDFs; YAML anchors across documents; heredoc
escaping that wrote backspace characters into a regex (caught by reading the file back);
a test it wrote with the wrong expectation. Each was found by running something: a test,
the checker, or a look at the actual output.

**What the owner must be able to explain:** everything. `docs/VIVA-GUIDE.md` walks through
each part with the file to open.
