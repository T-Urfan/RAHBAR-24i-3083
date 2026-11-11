"""Readiness (section 4.3): may this pod receive traffic right now?

Ready only when Chroma answers, the live collection exists and is non-empty, its
embedding model matches EMBED_MODEL, and Redis answers. Otherwise /readyz returns
503 naming each dependency that failed, and Kubernetes takes the pod out of the
Service without restarting it. Liveness (/healthz) never calls any of this.

Results are reused for READINESS_TTL_SECONDS, and a failure is logged once when it
starts (and once when it clears), not on every probe.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from ..config import Settings
from ..retrieval.chroma import ChromaRetriever
from .pin import check_pin

log = logging.getLogger("rahbar.readiness")


class ReadinessChecker:
    def __init__(self, settings: Settings, retriever: Any, redis_client: Any | None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.retriever = retriever
        self.redis = redis_client
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, bool, dict[str, Any]] | None = None
        self._last_failed: tuple[str, ...] = ()

    def check(self, force: bool = False) -> tuple[bool, dict[str, Any]]:
        now = self._clock()
        with self._lock:
            cached = self._cached
        if not force and cached and now - cached[0] < self.settings.readiness_ttl_seconds:
            return cached[1], cached[2]
        ok, body = self._run()
        with self._lock:
            self._cached = (now, ok, body)
        self._log_transition(body)
        return ok, body

    def _run(self) -> tuple[bool, dict[str, Any]]:
        checks: dict[str, str] = {}
        failed: list[str] = []

        if isinstance(self.retriever, ChromaRetriever):
            status = self.retriever.describe()
            if not status.reachable:
                failed.append("chroma")
                checks["chroma"] = f"unreachable: {status.error}"
            else:
                checks["chroma"] = "ok"
                if status.active is None:
                    failed.append("collection_missing")
                    checks["collection"] = status.error or "no active index"
                elif status.count == 0:
                    failed.append("collection_empty")
                    checks["collection"] = f"{status.active} is empty"
                else:
                    checks["collection"] = f"{status.active}: {status.count} chunks"
                if status.active is not None:
                    pin = check_pin(self.settings.embed_model, status)
                    checks["embed_model"] = pin.message if not pin.ok else f"ok: {pin.message}"
                    if not pin.ok:
                        failed.append("embed_model_mismatch")
        else:
            checks["chroma"] = f"skipped (RETRIEVER={self.settings.retriever})"

        if self.redis is None:
            checks["redis"] = "skipped (REDIS_HOST is not set)"
        else:
            try:
                self.redis.ping()
                checks["redis"] = "ok"
            except Exception as exc:
                failed.append("redis")
                checks["redis"] = f"unreachable: {type(exc).__name__}: {exc}"

        if failed:
            return False, {"status": "not ready", "failed": failed, "checks": checks}
        return True, {"status": "ready", "checks": checks}

    def _log_transition(self, body: dict[str, Any]) -> None:
        failed = tuple(body.get("failed", ()))
        if failed == self._last_failed:
            return
        self._last_failed = failed
        if failed:
            # One line naming exactly what conflicts: the section 4.4 demonstration.
            log.warning("not ready", extra={"failed": list(failed), "checks": body["checks"]})
        else:
            log.info("ready", extra={"checks": body["checks"]})
