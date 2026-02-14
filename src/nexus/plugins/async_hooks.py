"""Async wrapper for PluginHooks (Issue #1440).

Adapter that wraps the partially-async ``PluginHooks`` to satisfy
``HookEngineProtocol``.  Handles the type mapping between:

- Protocol phase strings (``pre_write``, ``post_read``, ...) ↔
  PluginHooks ``HookType`` enum (``before_write``, ``after_read``, ...)
- ``HookSpec`` / ``HookContext`` / ``HookResult`` (Protocol) ↔
  ``HookType`` / ``dict`` / ``dict | None`` (PluginHooks)

All methods are **direct calls** (no ``asyncio.to_thread``):
``register`` and ``unregister`` are lightweight in-memory operations,
and ``execute`` is already async.

Thread Safety: All async methods run cooperatively on the event loop.
The ``_registered`` dict is only mutated between ``await`` points, so
no concurrent access can occur in a single-threaded asyncio context.

References:
    - Issue #1440: Async wrappers for 4 sync kernel protocols
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from nexus.plugins.hooks import HookType
from nexus.services.protocols.hook_engine import (
    HookContext,
    HookId,
    HookResult,
    HookSpec,
)

if TYPE_CHECKING:
    from nexus.plugins.hooks import PluginHooks


# ---------------------------------------------------------------------------
# Phase name translation: Protocol ↔ PluginHooks
# ---------------------------------------------------------------------------

_PHASE_TO_HOOK_TYPE: dict[str, HookType] = {
    "pre_read": HookType.BEFORE_READ,
    "post_read": HookType.AFTER_READ,
    "pre_write": HookType.BEFORE_WRITE,
    "post_write": HookType.AFTER_WRITE,
    "pre_delete": HookType.BEFORE_DELETE,
    "post_delete": HookType.AFTER_DELETE,
    "pre_mkdir": HookType.BEFORE_MKDIR,
    "post_mkdir": HookType.AFTER_MKDIR,
    "pre_copy": HookType.BEFORE_COPY,
    "post_copy": HookType.AFTER_COPY,
}


def _phase_to_hook_type(phase: str) -> HookType:
    """Map a protocol phase string to the corresponding ``HookType``.

    Raises:
        ValueError: If *phase* has no known mapping.
    """
    try:
        return _PHASE_TO_HOOK_TYPE[phase]
    except KeyError:
        raise ValueError(f"Unknown hook phase: {phase!r}") from None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AsyncHookEngine:
    """Async adapter for ``PluginHooks`` conforming to ``HookEngineProtocol``.

    Maintains an internal mapping from ``HookId`` to ``(HookType, handler)``
    so that ``unregister_hook`` can locate the correct entry in the inner
    registry.
    """

    def __init__(self, inner: PluginHooks) -> None:
        self._inner = inner
        # HookId.id → (HookType, adapted_handler)
        self._registered: dict[str, tuple[HookType, Callable[..., Any]]] = {}
        # Defensive lock for multi-threaded scenarios (e.g. to_thread callers)
        self._lock = asyncio.Lock()

    async def register_hook(
        self,
        spec: HookSpec,
        handler: Callable[..., Awaitable[HookResult]],
    ) -> HookId:
        hook_type = _phase_to_hook_type(spec.phase)

        # Wrap the protocol-level handler so it speaks PluginHooks' dict convention
        async def _adapted(context: dict[str, Any]) -> dict[str, Any] | None:
            hook_ctx = HookContext(
                phase=spec.phase,
                path=context.get("path"),
                zone_id=context.get("zone_id"),
                agent_id=context.get("agent_id"),
                payload=context,
            )
            result: HookResult = await handler(hook_ctx)
            if not result.proceed:
                return None
            if result.modified_context is not None:
                return result.modified_context
            return context

        self._inner.register(hook_type, _adapted, priority=spec.priority)

        hook_id = HookId(id=uuid.uuid4().hex)
        async with self._lock:
            self._registered[hook_id.id] = (hook_type, _adapted)
        return hook_id

    async def unregister_hook(self, hook_id: HookId) -> bool:
        async with self._lock:
            entry = self._registered.pop(hook_id.id, None)
        if entry is None:
            return False
        hook_type, handler = entry
        self._inner.unregister(hook_type, handler)
        return True

    async def fire(self, phase: str, context: HookContext) -> HookResult:
        hook_type = _phase_to_hook_type(phase)

        # Build dict context for PluginHooks.execute()
        ctx_dict: dict[str, Any] = dict(context.payload) if context.payload else {}
        if context.path is not None:
            ctx_dict["path"] = context.path
        if context.zone_id is not None:
            ctx_dict["zone_id"] = context.zone_id
        if context.agent_id is not None:
            ctx_dict["agent_id"] = context.agent_id

        result = await self._inner.execute(hook_type, ctx_dict)

        if result is None:
            return HookResult(
                proceed=False,
                modified_context=None,
                error=f"Hook vetoed operation in phase '{phase}' (path={context.path})",
            )

        return HookResult(proceed=True, modified_context=result, error=None)
