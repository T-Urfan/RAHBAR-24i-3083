"""The indexer Job's entry point. It runs once and exits (section 4.2).

    python -m pipeline.run --dummy    # week 1: three fake chunks, no model needed
    python -m pipeline.run            # the real pipeline: extract, clean, chunk, embed, store
    python -m pipeline.run --rollback # repoint the API at the previous index (ADR 0001)

Exit code 0 means the index was written; anything else fails the Job, and
Kubernetes retries it up to the Job's backoffLimit. A failed run never touches
the index the API is serving (see store.py).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import ConfigError, IndexerSettings, get_settings
from .embed import EmbeddingError
from .log import log
from .store import IndexBuildError


def _front_matter(path: Path) -> dict[str, str]:
    """Read `key: value` lines between leading '---' markers (used by eval/fixtures)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta


def _as_bool(value: object) -> bool:
    """JSON true/false, or the strings "true"/"false" (bool("false") would be True)."""
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def discover(corpus_dir: str) -> list[tuple[Path, str, bool]]:
    """List (path, doc_version, superseded) for every document to index.

    corpus/fetch.py writes _index.json next to the files it downloads; use it when
    present. Otherwise read Markdown front matter (the CI fixtures work this way).
    """
    root = Path(corpus_dir)
    index = root / "_index.json"
    if index.exists():
        entries = json.loads(index.read_text(encoding="utf-8"))
        listed = []
        base = root.resolve()
        for e in entries:
            path = (root / str(e["file"])).resolve()
            if not path.is_relative_to(base):      # "../secret" or an absolute path
                raise SystemExit(f"_index.json names a file outside {corpus_dir}: {e['file']}")
            listed.append((path, str(e.get("doc_version", "unknown")),
                           _as_bool(e.get("superseded", False))))
        return listed
    found = []
    for path in sorted(root.glob("*")):
        if path.suffix.lower() not in {".pdf", ".md", ".html", ".txt"}:
            continue
        if path.name.lower().startswith("readme"):
            continue
        meta = _front_matter(path) if path.suffix.lower() == ".md" else {}
        found.append((path, meta.get("doc_version", "unknown"),
                      meta.get("superseded", "false").lower() == "true"))
    return found


def _peak_rss_mb() -> float | None:
    """Peak resident memory of this process in MB, on Linux (the Job's container).
    Compare it with `kubectl top pod` when choosing the Job's memory limit."""
    if sys.platform == "win32":
        return None
    import resource

    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def run_pipeline(settings: IndexerSettings, client: Any = None,
                 embedder: Callable[[list[str]], list[list[float]]] | None = None) -> int:
    """Extract, clean, chunk, embed and store every document into a NEW collection,
    then activate it. Streams: one document at a time, one embedding batch at a time,
    so memory holds one document's text and one batch of vectors, not the corpus.

    A document that cannot be extracted, or yields no chunks, is a bad document: it is
    logged, and activation is refused (the live index keeps serving) unless
    ALLOW_PARTIAL_INDEX=true. A provider failure (Ollama down) aborts the run."""
    from .chunk import chunk, count_words, source_words
    from .clean import clean
    from .embed import document_input, embed, http_client
    from .extract import ExtractionError, extract
    from .store import IndexBuild, activate, get_client

    documents = discover(settings.corpus_dir)
    if not documents:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    build = IndexBuild(client if client is not None else get_client(settings), settings,
                       embed_model=settings.embed_model,
                       extra_metadata={"chunk_size": settings.chunk_size,
                                       "chunk_overlap": settings.chunk_overlap,
                                       "chunk_unit": "words",
                                       "chunk_strategy": settings.chunk_strategy})
    totals: Counter[str] = Counter()
    failed: list[str] = []
    http = http_client(settings)
    embed_batch = embedder or (lambda texts: embed(texts, settings, client=http))
    try:
        for path, doc_version, superseded in documents:
            started = time.perf_counter()
            try:
                doc = clean(extract(str(path), doc_version, superseded, settings))
                pieces = chunk(doc, settings.chunk_size, settings.chunk_overlap,
                               strategy=settings.chunk_strategy)
                if not pieces:
                    raise ExtractionError(f"{path.name}: no text to index (a scanned file "
                                          "with OCR_ENABLED=false, or an empty document)")
            except ExtractionError as exc:
                failed.append(path.name)
                log("document failed", source=path.name, error=str(exc))
                continue
            for start in range(0, len(pieces), settings.embed_batch_size):
                batch = pieces[start:start + settings.embed_batch_size]
                build.add(batch, embed_batch([document_input(c.section, c.text) for c in batch]))
            words = source_words(doc)
            stored = sum(count_words(c.text) for c in pieces)
            totals.update(doc.stats)
            totals.update({"documents": 1, "chunks": len(pieces), "source_words": words,
                           "stored_words": stored})
            log("document indexed", source=path.name, doc_version=doc_version,
                superseded=superseded, chunks=len(pieces), source_words=words,
                stored_words=stored, seconds=round(time.perf_counter() - started, 1),
                peak_rss_mb=_peak_rss_mb(), **doc.stats)
        if failed and not settings.allow_partial_index:
            raise IndexBuildError(f"{len(failed)} document(s) failed ({', '.join(failed)}); "
                                  "set ALLOW_PARTIAL_INDEX=true to index the rest anyway")
        if failed:
            log("activating a partial index", failed=failed)
        amplification = round(totals["stored_words"] / totals["source_words"], 3) \
            if totals["source_words"] else 0.0
        stats: dict[str, str | int | float | bool] = {
            "documents": totals["documents"], "failed_documents": len(failed),
            "source_words": totals["source_words"], "stored_words": totals["stored_words"],
            "amplification": amplification, "ocr_pages": totals["ocr_pages"],
            "tables": totals["tables"]}
        build.finalize(stats)
        result = activate(build)
    except BaseException:
        build.discard()          # never leave a half-built collection behind
        raise
    finally:
        http.close()
    log("index summary", **stats, chunks=result.count, active=result.active,
        peak_rss_mb=_peak_rss_mb())
    return result.count


def _terminate(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)          # 143 for SIGTERM, as Kubernetes expects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the RAHBAR index, then exit.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dummy", action="store_true", help="write three fake chunks")
    mode.add_argument("--rollback", action="store_true",
                      help="repoint the API at the previous index, then exit")
    args = parser.parse_args(argv)
    # Kubernetes stops a pod with SIGTERM (Job deadline, eviction, deletion). Python's
    # default handler exits without unwinding, which would skip the cleanup that
    # discards a half-built collection; turn it into an exception instead.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _terminate)
    try:
        settings = get_settings()
    except ConfigError as exc:
        log("indexer configuration is invalid", error=str(exc))
        return 1
    log("indexer starting", dummy=args.dummy, rollback=args.rollback,
        embed_model=settings.embed_model, collection=settings.collection,
        chroma=f"{settings.chroma_host}:{settings.chroma_port}",
        pod=os.environ.get("HOSTNAME", "-"))
    started = time.perf_counter()
    try:
        if args.rollback:
            from .store import get_client, rollback

            count = rollback(get_client(settings), settings).count
        elif args.dummy:
            from .dummy import write_dummy_index
            from .store import get_client

            count = write_dummy_index(settings, get_client(settings))
        else:
            count = run_pipeline(settings)
    except NotImplementedError as exc:
        log("indexer stage not implemented yet", error=str(exc))
        return 2
    except IndexBuildError as exc:
        log("indexer refused to activate; the live index is unchanged", error=str(exc))
        return 1
    except EmbeddingError as exc:
        log("embedding failed; the live index is unchanged", error=str(exc))
        return 1
    log("indexer finished", chunks=count, seconds=round(time.perf_counter() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
