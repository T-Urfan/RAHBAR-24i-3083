# AI usage

<!-- STARTER-TODO: list every AI tool you used. A specific disclosure carries no penalty. -->

| Tool | What it wrote or shaped | What we changed afterwards, and why |
| --- | --- | --- |
| Claude Code (Claude Opus 5.5) | Repository audit and the phased plan (P0–P13). P1: the safe re-index pointer (`indexer/pipeline/store.py`), the lazy read-only Chroma client, `/readyz` and the embedding-pin check (`api/app/`), and their tests. Commits containing its code carry a `Co-Authored-By: Claude` trailer. | Pending the owner's review before submission. |
| Claude Code (Claude Opus 5.5) | The indexing subsystem: `extract.py`, `clean.py`, `chunk.py`, `embed.py`, the streaming `run_pipeline`, and every test in `indexer/tests/` except the starter's; the first drafts of `docs/CORPUS.md` and `docs/PIPELINE.md`. Two independent read-only review agents (test quality, code correctness) checked the result, and their confirmed findings were fixed: for example, a failure after activation could delete the live index, and the TOC rule deleted real fee lists. | Pending the owner's review. Every design choice is explained in the code docstrings and in PIPELINE.md, so it can be checked and defended. |
