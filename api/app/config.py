"""Runtime configuration, read once from environment variables.

On the cluster these variables come from your ConfigMap and Secret (section 4.1).
Nothing here is baked into the image, so one image runs in every environment.

An empty value (``TOP_K: ""`` in a ConfigMap) means "use the default". A value that
cannot be parsed stops the process at startup with a message naming the variable,
which is far easier to diagnose than a wrong number silently in use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(ValueError):
    """A configuration variable holds a value the API cannot use."""


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


def _float(name: str, default: float, low: float | None = None,
           high: float | None = None) -> float:
    raw = _str(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if (low is not None and value < low) or (high is not None and value > high):
        raise ConfigError(f"{name}={value} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class Settings:
    # Which implementations to load. "stub" needs no cluster, no model, no network.
    retriever: str = "stub"          # RETRIEVER: stub | chroma
    llm_provider: str = "stub"       # LLM_PROVIDER: stub | ollama-host | ollama-incluster | hosted

    # The embedding pin (section 4.4). The indexer reads the same ConfigMap key.
    embed_model: str = "nomic-embed-text"

    # Vector store
    chroma_host: str = "chroma"
    chroma_port: int = 8000
    collection: str = "rahbar"
    pointer_ttl_seconds: float = 10.0   # how long a resolved active-index name is reused

    # Semantic cache. Empty REDIS_HOST means "no Redis" (local stub runs); the
    # ConfigMap sets it, and then readiness requires Redis to answer.
    redis_host: str = ""
    redis_port: int = 6379

    # Readiness results are reused this long, so a probe every 10 s from each pod
    # costs Chroma and Redis at most one round-trip each.
    readiness_ttl_seconds: float = 5.0

    # Retrieval and generation
    top_k: int = 5
    temperature: float = 0.2
    llm_model: str = "llama3.2:3b"
    ollama_url: str = "http://host.minikube.internal:11434"
    hosted_base_url: str = ""        # e.g. an OpenAI-compatible endpoint
    llm_api_key: str = field(default="", repr=False)   # from the Secret; never log it

    log_level: str = "INFO"


def get_settings() -> Settings:
    return Settings(
        retriever=_str("RETRIEVER", "stub"),
        llm_provider=_str("LLM_PROVIDER", "stub"),
        embed_model=_str("EMBED_MODEL", "nomic-embed-text"),
        chroma_host=_str("CHROMA_HOST", "chroma"),
        chroma_port=_int("CHROMA_PORT", 8000, minimum=1),
        collection=_str("CHROMA_COLLECTION", "rahbar"),
        pointer_ttl_seconds=_float("POINTER_TTL_SECONDS", 10.0, low=0.0),
        redis_host=_str("REDIS_HOST", ""),
        redis_port=_int("REDIS_PORT", 6379, minimum=1),
        readiness_ttl_seconds=_float("READINESS_TTL_SECONDS", 5.0, low=0.0),
        top_k=_int("TOP_K", 5, minimum=1),
        temperature=_float("TEMPERATURE", 0.2, low=0.0, high=2.0),
        llm_model=_str("LLM_MODEL", "llama3.2:3b"),
        ollama_url=_str("OLLAMA_URL", "http://host.minikube.internal:11434"),
        hosted_base_url=_str("HOSTED_BASE_URL", ""),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        log_level=_str("LOG_LEVEL", "INFO"),
    )
