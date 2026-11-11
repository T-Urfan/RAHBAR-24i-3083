from __future__ import annotations

import sys
from pathlib import Path

import pytest

# make tests/fakes.py and tests/pdfgen.py importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Every variable the indexer reads. A value left in the developer's shell (CHUNK_SIZE
# from a tuning run, say) must not change what a test checks.
INDEXER_ENV = (
    "EMBED_MODEL", "CHROMA_HOST", "CHROMA_PORT", "CHROMA_COLLECTION", "CORPUS_DIR",
    "CHUNK_SIZE", "CHUNK_OVERLAP", "OLLAMA_URL", "EMBED_BATCH_SIZE", "EMBED_TIMEOUT_SECONDS",
    "EMBED_RETRIES", "OCR_ENABLED", "OCR_DPI", "OCR_LANG", "TESSERACT_CMD", "MIN_TEXT_CHARS",
    "CHUNK_STRATEGY", "ALLOW_PARTIAL_INDEX",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in INDEXER_ENV:
        if name != "TESSERACT_CMD":      # kept: tells the real-OCR test where tesseract is
            monkeypatch.delenv(name, raising=False)
