"""Indexer configuration, from the same ConfigMap the API reads (section 4.1).

Empty values mean "use the default"; values that cannot be parsed stop the Job at
start with a message naming the variable (the same rules as api/app/config.py).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


class ConfigError(ValueError):
    """A configuration variable holds a value the indexer cannot use."""


def _str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or not value.strip() else value.strip()


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = _str(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not an integer") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name}={value} must be at least {minimum}")
    return value


def _float(name: str, default: float, minimum: float | None = None) -> float:
    raw = _str(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name}={value} must be at least {minimum}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _str(name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean (true/false)")


@dataclass(frozen=True)
class IndexerSettings:
    embed_model: str        # EMBED_MODEL: must equal the API's value (section 4.4)
    chroma_host: str
    chroma_port: int
    collection: str
    corpus_dir: str         # where the documents and _index.json sit inside the Job's pod
    chunk_size: int         # in words (see docs/PIPELINE.md for why words)
    chunk_overlap: int      # in words
    ollama_url: str
    # Embedding (stage 4)
    embed_batch_size: int = 16          # texts per Ollama request: bounds memory and request size
    embed_timeout_seconds: float = 120.0  # first call also loads the model into RAM on CPU
    embed_retries: int = 3              # extra attempts on timeouts, connection errors and 5xx
    # Extraction (stage 1)
    ocr_enabled: bool = True            # false only for failure-analysis stage A
    ocr_dpi: int = 300
    ocr_lang: str = "eng"
    tesseract_cmd: str = ""             # optional explicit path (Windows installs off PATH)
    min_text_chars: int = 25            # fewer extractable characters than this = no text layer
    # Chunking (stage 3)
    chunk_strategy: str = "structure"   # structure | fixed (fixed only for failure stage B)
    # Run policy
    allow_partial_index: bool = False   # a failed document blocks activation unless true


def get_settings() -> IndexerSettings:
    settings = IndexerSettings(
        embed_model=_str("EMBED_MODEL", "nomic-embed-text"),
        chroma_host=_str("CHROMA_HOST", "chroma"),
        chroma_port=_int("CHROMA_PORT", 8000, minimum=1),
        collection=_str("CHROMA_COLLECTION", "rahbar"),
        corpus_dir=_str("CORPUS_DIR", "/corpus"),
        chunk_size=_int("CHUNK_SIZE", 400, minimum=20),
        chunk_overlap=_int("CHUNK_OVERLAP", 50, minimum=0),
        ollama_url=_str("OLLAMA_URL", "http://host.minikube.internal:11434"),
        embed_batch_size=_int("EMBED_BATCH_SIZE", 16, minimum=1),
        embed_timeout_seconds=_float("EMBED_TIMEOUT_SECONDS", 120.0, minimum=1.0),
        embed_retries=_int("EMBED_RETRIES", 3, minimum=0),
        ocr_enabled=_bool("OCR_ENABLED", True),
        ocr_dpi=_int("OCR_DPI", 300, minimum=72),
        ocr_lang=_str("OCR_LANG", "eng"),
        tesseract_cmd=_str("TESSERACT_CMD", ""),
        min_text_chars=_int("MIN_TEXT_CHARS", 25, minimum=1),
        chunk_strategy=_str("CHUNK_STRATEGY", "structure").lower(),
        allow_partial_index=_bool("ALLOW_PARTIAL_INDEX", False),
    )
    # Chroma names are 3-63 characters of [A-Za-z0-9._-]; a version collection adds 30
    # characters ("-v" + 21-character timestamp + "-" + 6 hex) to this base name.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,31}[A-Za-z0-9]", settings.collection):
        raise ConfigError(f"CHROMA_COLLECTION={settings.collection!r} must be 3-33 characters "
                          "of letters, digits, '.', '_' or '-', starting and ending with a "
                          "letter or digit")
    if settings.chunk_overlap >= settings.chunk_size:
        raise ConfigError(f"CHUNK_OVERLAP={settings.chunk_overlap} must be smaller than "
                          f"CHUNK_SIZE={settings.chunk_size}")
    if settings.chunk_strategy not in {"structure", "fixed"}:
        raise ConfigError(f"CHUNK_STRATEGY={settings.chunk_strategy!r} must be structure or fixed")
    return settings
