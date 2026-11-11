"""Hosted free-tier provider (Groq, Google AI Studio, OpenRouter): OpenAI-compatible.

POST {HOSTED_BASE_URL}/chat/completions with a JSON-schema response_format. The key
arrives as LLM_API_KEY from a Kubernetes Secret created on the command line; it is
sent only in the Authorization header and never logged (Settings hides it from repr).
"""

from __future__ import annotations

import time

import httpx

from ...config import Settings
from ...routes.metrics import GENERATION_LATENCY, TOKENS
from ...schemas import Answer, Passage
from ...services.prompt import build_messages
from .base import ProviderError


class HostedProvider:
    name = "hosted"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not settings.hosted_base_url:
            raise ValueError("LLM_PROVIDER=hosted needs HOSTED_BASE_URL")
        self.settings = settings
        self._client = client or httpx.Client(
            base_url=settings.hosted_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=5.0))

    def generate(self, question: str, passages: list[Passage],
                 retry_reason: str | None = None) -> Answer:
        payload = {
            "model": self.settings.llm_model,
            "messages": build_messages(question, passages, retry_reason),
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.llm_max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "Answer", "schema": Answer.model_json_schema()}},
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        started = time.perf_counter()
        try:
            response = self._client.post("/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            # str(exc) of an httpx error names the URL, never the headers or the key
            raise ProviderError(f"hosted {self.settings.llm_model}: "
                                f"{type(exc).__name__}: {exc}") from exc
        finally:
            GENERATION_LATENCY.labels(provider=self.name).observe(time.perf_counter() - started)
        usage = body.get("usage") or {}
        TOKENS.labels(direction="in").observe(int(usage.get("prompt_tokens") or 0))
        TOKENS.labels(direction="out").observe(int(usage.get("completion_tokens") or 0))
        return Answer.model_validate_json(content)
