"""Liveness and readiness (section 4.3). They answer different questions."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Is the process alive? Must never touch Chroma, Redis or the LLM.

    `async def` on purpose: it runs on the event loop, not in the worker thread pool
    that blocking /api/ask calls use. If slow LLM calls filled that pool, a sync
    liveness handler would queue behind them, time out, and the kubelet would kill a
    pod that was merely busy."""
    return {"status": "alive"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, object]:
    """Can this pod take traffic now? 200, or 503 naming each failed dependency.
    The checks themselves are in services/readiness.py."""
    ok, body = request.app.state.readiness.check()
    if not ok:
        response.status_code = 503
    return body
