"""The Ollama and hosted providers, against a mock HTTP transport (no network, no
model): the request carries the schema and the temperature, the reply is validated,
and transport failures become ProviderError without leaking the API key."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.providers.llm import get_provider
from app.providers.llm.base import ProviderError
from app.providers.llm.hosted import HostedProvider
from app.providers.llm.ollama import OllamaProvider
from app.schemas import Answer, Passage

PASSAGE = Passage(chunk_id="c1", text="Attendance must be 80%.", source="rules.pdf",
                  section="1.21 Attendance", page=3, score=0.8)
GOOD = {"reasoning": "passage 1", "answer": "80%.", "answered_from_context": True,
        "confidence": "high",
        "citations": [{"source": "rules.pdf", "section": "1.21 Attendance", "page": 3}]}


def transport(handler):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)
    return httpx.MockTransport(wrapped), seen


def ollama(handler, **kw):  # type: ignore[no-untyped-def]
    t, seen = transport(handler)
    settings = Settings(temperature=0.1, llm_model="llama3.2:3b", **kw)
    return OllamaProvider(settings, httpx.Client(base_url="http://ollama", transport=t)), seen


def test_ollama_sends_the_answer_schema_as_format_at_low_temperature() -> None:
    provider, seen = ollama(lambda r: httpx.Response(200, json={
        "message": {"content": json.dumps(GOOD)}, "prompt_eval_count": 900, "eval_count": 60}))
    answer = provider.generate("What attendance is needed?", [PASSAGE])
    assert answer == Answer.model_validate(GOOD)
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/api/chat"
    assert body["format"] == Answer.model_json_schema()
    assert body["options"]["temperature"] == 0.1
    assert body["stream"] is False and body["model"] == "llama3.2:3b"
    assert body["messages"][0]["role"] == "system"
    assert "1.21 Attendance" in body["messages"][1]["content"]


def test_ollama_prose_reply_raises_validation_error() -> None:
    provider, _ = ollama(lambda r: httpx.Response(200, json={
        "message": {"content": "Sure! Attendance is 80%."}}))
    with pytest.raises(ValidationError):
        provider.generate("q?", [PASSAGE])


def test_ollama_reply_missing_a_field_raises_validation_error() -> None:
    partial = {k: v for k, v in GOOD.items() if k != "answered_from_context"}
    provider, _ = ollama(lambda r: httpx.Response(200, json={
        "message": {"content": json.dumps(partial)}}))
    with pytest.raises(ValidationError):
        provider.generate("q?", [PASSAGE])


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(500, text="model crashed"),
    lambda r: httpx.Response(404, text='{"error":"model not found"}'),
])
def test_ollama_http_errors_become_provider_errors(handler) -> None:  # type: ignore[no-untyped-def]
    provider, _ = ollama(handler)
    with pytest.raises(ProviderError):
        provider.generate("q?", [PASSAGE])


def test_ollama_timeout_becomes_a_provider_error() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)
    provider, _ = ollama(slow)
    with pytest.raises(ProviderError, match="ReadTimeout"):
        provider.generate("q?", [PASSAGE])


def test_the_retry_reason_is_sent_to_the_model() -> None:
    provider, seen = ollama(lambda r: httpx.Response(200, json={
        "message": {"content": json.dumps(GOOD)}}))
    provider.generate("q?", [PASSAGE], retry_reason="schema: not JSON")
    messages = json.loads(seen[0].content)["messages"]
    assert "schema: not JSON" in messages[-1]["content"]


def test_ollama_client_uses_the_configured_timeout() -> None:
    provider = OllamaProvider(Settings(llm_timeout_seconds=42.0))
    assert provider._client.timeout.read == 42.0


SECRET = "test-key-not-real-0123456789"


def hosted(handler):  # type: ignore[no-untyped-def]
    t, seen = transport(handler)
    settings = Settings(llm_provider="hosted", hosted_base_url="https://llm.example/v1",
                        llm_api_key=SECRET, llm_model="m")
    client = httpx.Client(base_url="https://llm.example/v1", transport=t)
    return HostedProvider(settings, client), seen


def test_hosted_sends_a_json_schema_response_format_and_the_key_as_a_header() -> None:
    provider, seen = hosted(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(GOOD)}}],
        "usage": {"prompt_tokens": 800, "completion_tokens": 50}}))
    assert provider.generate("q?", [PASSAGE]).answered_from_context is True
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/chat/completions"
    assert body["response_format"]["json_schema"]["schema"] == Answer.model_json_schema()
    assert seen[0].headers["Authorization"] == f"Bearer {SECRET}"


def test_hosted_errors_never_contain_the_key() -> None:
    provider, _ = hosted(lambda r: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(ProviderError) as info:
        provider.generate("q?", [PASSAGE])
    assert SECRET not in str(info.value)


def test_hosted_malformed_body_is_a_provider_error() -> None:
    provider, _ = hosted(lambda r: httpx.Response(200, json={"choices": []}))
    with pytest.raises(ProviderError):
        provider.generate("q?", [PASSAGE])


def test_the_key_is_hidden_from_settings_repr() -> None:
    assert SECRET not in repr(Settings(llm_api_key=SECRET))


def test_hosted_requires_a_base_url() -> None:
    with pytest.raises(ValueError, match="HOSTED_BASE_URL"):
        HostedProvider(Settings(llm_provider="hosted"))


@pytest.mark.parametrize("name, cls", [("ollama-host", OllamaProvider),
                                       ("ollama-incluster", OllamaProvider)])
def test_provider_is_selected_by_config(name: str, cls: type) -> None:
    assert isinstance(get_provider(Settings(llm_provider=name)), cls)


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown LLM_PROVIDER"):
        get_provider(Settings(llm_provider="gpt"))
