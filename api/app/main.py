"""The API process. It reads the index; it never builds it (section 4.2).

Run locally with stubs:   make dev     (then open http://localhost:8000)
Run in the container:     uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from .config import Settings, get_settings
from .logjson import configure_logging, request_id_var
from .providers.llm import get_provider
from .retrieval import get_retriever
from .routes import ask, health, metrics
from .services.readiness import ReadinessChecker

log = logging.getLogger("rahbar.api")


def _redis_client(settings: Settings) -> object | None:
    """Building a redis-py client opens no connection; the first command does."""
    if not settings.redis_host:
        return None
    import redis

    return redis.Redis(host=settings.redis_host, port=settings.redis_port,
                       socket_connect_timeout=1.0, socket_timeout=1.0)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.settings = settings
    # Neither of these connects to anything: Chroma and Redis may be down at startup,
    # and that must make the pod NOT READY, never crash it (section 4.3).
    app.state.retriever = get_retriever(settings)
    app.state.provider = get_provider(settings)
    app.state.redis = _redis_client(settings)
    app.state.readiness = ReadinessChecker(settings, app.state.retriever, app.state.redis)
    # This process only reads the index; the indexer Job writes it (section 4.2).
    # Check the embedding pin once now, so a mismatch is in the log from the first
    # second; /readyz keeps re-checking it, because a re-index can change it later.
    ready, body = app.state.readiness.check(force=True)
    log.info(
        "api started",
        extra={"retriever": settings.retriever, "llm_provider": settings.llm_provider,
               "embed_model": settings.embed_model, "top_k": settings.top_k,
               "ready": ready, "checks": body["checks"]},
    )
    yield


app = FastAPI(title="RAHBAR API", lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = request_id_var.set(rid)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")   # route template, not the raw URL
        elapsed = time.perf_counter() - start
        metrics.REQUESTS.labels(path=path, status=str(status)).inc()
        metrics.LATENCY.labels(path=path).observe(elapsed)
        if path not in ("/healthz", "/readyz", "/metrics"):
            log.info("request", extra={"path": path, "status": status,
                                       "ms": round(elapsed * 1000, 1)})
        request_id_var.reset(token)


app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(ask.router)

# Development only: `make dev` serves the chat UI from this process, so you can try it
# without a cluster. On Kubernetes the ui Deployment serves it and this stays unset.
_ui_dir = os.environ.get("SERVE_UI_DIR")
if _ui_dir:
    app.mount("/", StaticFiles(directory=_ui_dir, html=True), name="ui")
