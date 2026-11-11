"""Configuration comes from the environment only (section 4.1); bad values fail loudly."""

from __future__ import annotations

import pytest

from app.config import ConfigError, get_settings


def test_defaults_without_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TOP_K", "TEMPERATURE", "REDIS_HOST", "EMBED_MODEL"):
        monkeypatch.delenv(name, raising=False)
    s = get_settings()
    assert (s.top_k, s.temperature, s.redis_host, s.embed_model) == \
        (5, 0.2, "", "nomic-embed-text")


def test_empty_configmap_values_mean_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOP_K", "")
    monkeypatch.setenv("TEMPERATURE", "  ")
    monkeypatch.setenv("EMBED_MODEL", "")
    s = get_settings()
    assert (s.top_k, s.temperature, s.embed_model) == (5, 0.2, "nomic-embed-text")


def test_values_are_read_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOP_K", " 8 ")
    monkeypatch.setenv("REDIS_HOST", "redis")
    monkeypatch.setenv("TEMPERATURE", "0.1")
    s = get_settings()
    assert (s.top_k, s.redis_host, s.temperature) == (8, "redis", 0.1)


@pytest.mark.parametrize(("name", "value", "match"), [
    ("TOP_K", "five", "TOP_K='five' is not an integer"),
    ("TOP_K", "0", "TOP_K=0 must be at least 1"),
    ("TEMPERATURE", "hot", "TEMPERATURE='hot' is not a number"),
    ("TEMPERATURE", "3", "TEMPERATURE=3.0 must be between"),
    ("CHROMA_PORT", "-1", "CHROMA_PORT=-1"),
])
def test_bad_values_name_the_variable(monkeypatch: pytest.MonkeyPatch, name: str, value: str,
                                      match: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=match):
        get_settings()


def test_api_key_is_not_in_the_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "not-a-real-key-123")
    assert "not-a-real-key-123" not in repr(get_settings())
