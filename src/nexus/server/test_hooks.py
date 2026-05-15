"""Disabled-by-default FastAPI routes for end-to-end stack verification.

These routes are registered only when ``NEXUS_TEST_HOOKS=true``. They are
still admin-gated because test stacks often run on shared developer machines.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from nexus.server.dependencies import require_admin
from nexus.server.zone_execution import run_zone_scoped


def build_test_hooks_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/test-hooks",
        tags=["test-hooks"],
        dependencies=[Depends(require_admin)],
    )

    def _zone_registry(request: Request) -> Any:
        registry = getattr(request.app.state, "zone_registry", None)
        if registry is None:
            raise HTTPException(status_code=503, detail="Zone registry is not initialized")
        return registry

    @router.get("/zone-runners")
    async def list_zone_runners(request: Request) -> dict[str, Any]:
        registry = _zone_registry(request)
        return {
            "runners": [
                {"zone_id": runner.zone_id, "is_alive": runner.is_alive}
                for runner in registry.all()
            ],
        }

    @router.get("/zone-runners/{zone_id}/ping")
    async def ping_zone_runner(request: Request, zone_id: str) -> dict[str, Any]:
        registry = _zone_registry(request)

        async def _work() -> dict[str, Any]:
            return _runner_payload(zone_id, 0.0)

        return await run_zone_scoped(registry, zone_id, _work)

    @router.post("/zone-runners/{zone_id}/sleep")
    async def sleep_zone_runner(
        request: Request,
        zone_id: str,
        delay_ms: int = Query(default=100, ge=0, le=10_000),
    ) -> dict[str, Any]:
        registry = _zone_registry(request)

        async def _work() -> dict[str, Any]:
            start = time.perf_counter()
            await asyncio.sleep(delay_ms / 1000)
            return _runner_payload(zone_id, (time.perf_counter() - start) * 1000)

        return await run_zone_scoped(registry, zone_id, _work)

    return router


def _runner_payload(zone_id: str, elapsed_ms: float) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    return {
        "zone_id": zone_id,
        "thread_name": threading.current_thread().name,
        "loop_id": id(loop),
        "elapsed_ms": elapsed_ms,
    }
