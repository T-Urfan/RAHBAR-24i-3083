"""Ollama provider: ollama-host or ollama-incluster (section 3.5).

POST {OLLAMA_URL}/api/chat with the Answer JSON schema as ``format``: Ollama then
constrains decoding to that schema (the strongest enforcement it offers). The reply
is still validated with Pydantic, because structure can be guaranteed but the content
of each field cannot.
"""

from __future__ import annotations

import time

import httpx

from ...config import Settings
from ...routes.metrics import GENERATION_LATENCY, TOKENS
from ...schemas import Answer, Passage
from ...services.prompt import build_messages
from .base import ProviderError


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(
            base_url=settings.ollama_url,
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=5.0))

    def generate(self, question: str, passages: list[Passage],
                 retry_reason: str | None = None) -> Answer:
        payload = {
            "model": self.settings.llm_model,
            "messages": build_messages(question, passages, retry_reason),
            "format": Answer.model_json_schema(),
            "stream": False,
            "options": {"temperature": self.settings.temperature,
                        "num_ctx": self.settings.llm_num_ctx,
                        "num_predict": self.settings.llm_max_tokens},
        }
        started = time.perf_counter()
        try:
            response = self._client.post("/api/chat", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"ollama {self.settings.llm_model}: "
                                f"{type(exc).__name__}: {exc}") from exc
        finally:
            GENERATION_LATENCY.labels(provider=self.name).observe(time.perf_counter() - started)
        TOKENS.labels(direction="in").observe(int(body.get("prompt_eval_count") or 0))
        TOKENS.labels(direction="out").observe(int(body.get("eval_count") or 0))
        content = (body.get("message") or {}).get("content") or ""
        return Answer.model_validate_json(content)
