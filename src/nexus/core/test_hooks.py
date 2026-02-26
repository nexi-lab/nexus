"""Server-side test hooks for E2E hook testing.

Registered only when NEXUS_TEST_HOOKS=true.
Provides three test hooks and a REST router for querying hook state.

Contract constants (must match tests/hooks/conftest.py in nexus-test):
    HOOK_BLOCKED_PREFIX  = "/blocked/"
    HOOK_TEST_ENDPOINT   = "/api/test-hooks"
    CHAIN_EXPECTED_ORDER = "BA"
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from nexus.contracts.exceptions import AuditLogError

if TYPE_CHECKING:
    from nexus.core.kernel_dispatch import KernelDispatch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state (in-process only — not persisted)
# ---------------------------------------------------------------------------

_audit_markers: dict[str, dict[str, Any]] = {}
_chain_traces: dict[str, dict[str, Any]] = {}

BLOCKED_PREFIX = "/blocked/"


def _path_hash(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Test hook implementations
# ---------------------------------------------------------------------------


class AuditMarkerHook:
    """Records audit markers (path, timestamp, size) on every write."""

    @property
    def name(self) -> str:
        return "AuditMarkerHook"

    def on_post_write(self, ctx: Any) -> None:
        ph = _path_hash(ctx.path)
        content = ctx.content
        size = len(content) if isinstance(content, (bytes, str)) else 0
        _audit_markers[ph] = {
            "found": True,
            "path": ctx.path,
            "timestamp": time.time(),
            "size": size,
        }


class BlockedPathHook:
    """Raises AuditLogError for paths under /blocked/."""

    @property
    def name(self) -> str:
        return "BlockedPathHook"

    def on_post_write(self, ctx: Any) -> None:
        # Handle both unscoped (/blocked/...) and zone-scoped
        # (/zone/{zone_id}/blocked/...) paths.
        if BLOCKED_PREFIX in ctx.path:
            raise AuditLogError(f"Write to blocked path: {ctx.path}")


class ChainOrderHook:
    """Records execution order as a trace string."""

    def __init__(self, label: str) -> None:
        self._label = label

    @property
    def name(self) -> str:
        return f"ChainOrderHook({self._label})"

    def on_post_write(self, ctx: Any) -> None:
        ph = _path_hash(ctx.path)
        entry = _chain_traces.get(ph)
        if entry is None:
            _chain_traces[ph] = {"found": True, "trace": self._label}
        else:
            entry["trace"] += self._label


# ---------------------------------------------------------------------------
# Registration (called by factory/orchestrator.py)
# ---------------------------------------------------------------------------


def register_test_hooks(dispatch: KernelDispatch) -> None:
    """Register test hooks on the KernelDispatch instance."""
    dispatch.register_intercept_write(AuditMarkerHook())
    dispatch.register_intercept_write(BlockedPathHook())
    # B registered before A → expected trace "BA"
    dispatch.register_intercept_write(ChainOrderHook("B"))
    dispatch.register_intercept_write(ChainOrderHook("A"))
    logger.info("Test hooks registered (AuditMarker, BlockedPath, ChainOrder)")


# ---------------------------------------------------------------------------
# REST router (called by server/fastapi_server.py)
# ---------------------------------------------------------------------------


def build_test_hooks_router() -> APIRouter:
    """Build the /api/test-hooks/* REST router."""
    router = APIRouter(prefix="/api/test-hooks", tags=["test-hooks"])

    @router.get("/state")
    def hook_state() -> dict[str, Any]:
        return {"available": True}

    @router.get("/audit/{path_hash}")
    def get_audit(path_hash: str) -> dict[str, Any]:
        marker = _audit_markers.get(path_hash)
        if marker is None:
            return {"found": False}
        return marker

    @router.get("/chain/{path_hash}")
    def get_chain(path_hash: str) -> dict[str, Any]:
        trace = _chain_traces.get(path_hash)
        if trace is None:
            return {"found": False}
        return trace

    return router
