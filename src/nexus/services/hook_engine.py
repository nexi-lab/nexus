"""Scoped hook engine — per-agent scoping + verified execution (Issue #1257).

Wrapping layer over ``AsyncHookEngine`` (Mechanism 2: same-Protocol wrapping)
per NEXUS-LEGO-ARCHITECTURE §4.3.

Features:
    - **Per-agent scoping**: Hooks can target a specific agent via ``HookSpec.agent_scope``.
    - **Verified execution**: ``HookCapabilities`` are enforced at runtime (veto override,
      context modification override, per-handler timeout).
    - **Dual-index O(1) lookup**: Separate indexes for global and agent-scoped hooks.
    - **Sequential PRE / concurrent POST**: PRE hooks run sequentially (veto chain),
      POST hooks run concurrently via ``asyncio.gather()``.
    - **Lock-free fire()**: No lock on the read path; lock only on register/unregister.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §4.3 (Recursive Wrapping)
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §7 (eBPF / BPF LSM analogy)
    - Issue #1257: Hook engine per-agent scoping and verified execution
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nexus.services.protocols.hook_engine import (
    HookContext,
    HookId,
    HookResult,
    HookSpec,
)

if TYPE_CHECKING:
    from nexus.plugins.async_hooks import AsyncHookEngine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal entry for indexed hooks
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ScopedEntry:
    """Internal mutable entry for a registered hook."""

    hook_id: HookId
    spec: HookSpec
    handler: Callable[..., Awaitable[HookResult]]


# ---------------------------------------------------------------------------
# Phase classification
# ---------------------------------------------------------------------------

_PRE_PHASES = frozenset(
    {
        "pre_read",
        "pre_write",
        "pre_delete",
        "pre_mkdir",
        "pre_copy",
        "pre_mount",
        "pre_unmount",
    }
)


def _is_pre_phase(phase: str) -> bool:
    """Return True if *phase* is a PRE (sequential, can-veto) phase."""
    return phase in _PRE_PHASES or phase.startswith("pre_")


# ---------------------------------------------------------------------------
# ScopedHookEngine
# ---------------------------------------------------------------------------


class ScopedHookEngine:
    """Wrapping layer over AsyncHookEngine adding agent scoping + verified execution.

    Follows NEXUS-LEGO-ARCHITECTURE §4.3 Recursive Wrapping pattern.
    Satisfies ``HookEngineProtocol`` via duck typing.

    Thread-safety: ``fire()`` is lock-free (cooperative asyncio, no concurrent
    mutation between awaits).  ``register_hook`` / ``unregister_hook`` acquire
    ``_lock`` to protect index mutations.
    """

    def __init__(
        self,
        inner: "AsyncHookEngine",
        *,
        default_timeout_ms: int = 5000,
    ) -> None:
        self._inner = inner
        self._default_timeout_ms = default_timeout_ms

        # Dual index for O(1) lookup
        self._global_hooks: dict[str, list[_ScopedEntry]] = {}  # phase → entries
        self._agent_hooks: dict[
            tuple[str, str], list[_ScopedEntry]
        ] = {}  # (phase, agent_id) → entries
        self._id_to_entry: dict[str, _ScopedEntry] = {}  # hook_id.id → entry
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register_hook(
        self,
        spec: HookSpec,
        handler: Callable[..., Awaitable[HookResult]],
    ) -> HookId:
        hook_id = HookId(id=uuid.uuid4().hex)
        entry = _ScopedEntry(hook_id=hook_id, spec=spec, handler=handler)

        async with self._lock:
            self._id_to_entry[hook_id.id] = entry

            if spec.agent_scope is None:
                # Global hook
                hooks_list = self._global_hooks.setdefault(spec.phase, [])
                hooks_list.append(entry)
                hooks_list.sort(key=lambda e: e.spec.priority, reverse=True)
            else:
                # Agent-scoped hook
                key = (spec.phase, spec.agent_scope)
                hooks_list = self._agent_hooks.setdefault(key, [])
                hooks_list.append(entry)
                hooks_list.sort(key=lambda e: e.spec.priority, reverse=True)

        logger.debug(
            "[HOOK] Registered %r (phase=%s, scope=%s, priority=%d)",
            spec.handler_name,
            spec.phase,
            spec.agent_scope or "global",
            spec.priority,
        )
        return hook_id

    async def unregister_hook(self, hook_id: HookId) -> bool:
        async with self._lock:
            entry = self._id_to_entry.pop(hook_id.id, None)
            if entry is None:
                return False

            spec = entry.spec
            if spec.agent_scope is None:
                hooks_list = self._global_hooks.get(spec.phase, [])
                self._global_hooks[spec.phase] = [
                    e for e in hooks_list if e.hook_id.id != hook_id.id
                ]
            else:
                key = (spec.phase, spec.agent_scope)
                hooks_list = self._agent_hooks.get(key, [])
                self._agent_hooks[key] = [e for e in hooks_list if e.hook_id.id != hook_id.id]

        logger.debug("[HOOK] Unregistered %s", hook_id.id)
        return True

    # ------------------------------------------------------------------
    # Fire — lock-free read path
    # ------------------------------------------------------------------

    async def fire(self, phase: str, context: HookContext) -> HookResult:
        """Fire hooks for a given phase and context.

        - Merges global hooks + agent-scoped hooks (if context.agent_id matches).
        - PRE phases: sequential execution (higher priority first), can veto.
        - POST phases: concurrent execution via asyncio.gather().
        """
        # Collect applicable hooks (no lock needed — cooperative asyncio)
        entries = list(self._global_hooks.get(phase, []))

        if context.agent_id is not None:
            agent_entries = self._agent_hooks.get((phase, context.agent_id), [])
            if agent_entries:
                entries = sorted(
                    entries + list(agent_entries),
                    key=lambda e: e.spec.priority,
                    reverse=True,
                )

        if not entries:
            return HookResult(proceed=True, modified_context=None, error=None)

        if _is_pre_phase(phase):
            return await self._fire_sequential(entries, context)
        else:
            return await self._fire_concurrent(entries, context)

    async def _fire_sequential(
        self,
        entries: list[_ScopedEntry],
        context: HookContext,
    ) -> HookResult:
        """Execute hooks sequentially (PRE phases). Respects veto chain."""
        last_modified: dict[str, object] | None = None

        for entry in entries:
            result = await self._execute_with_enforcement(entry, context)

            if not result.proceed:
                return result

            if result.modified_context is not None:
                last_modified = result.modified_context

        return HookResult(proceed=True, modified_context=last_modified, error=None)

    async def _fire_concurrent(
        self,
        entries: list[_ScopedEntry],
        context: HookContext,
    ) -> HookResult:
        """Execute hooks concurrently (POST phases). No veto possible."""

        async def _run_one(entry: _ScopedEntry) -> HookResult:
            return await self._execute_with_enforcement(entry, context)

        results = await asyncio.gather(
            *(_run_one(e) for e in entries),
            return_exceptions=True,
        )

        # POST hooks don't veto; collect any modifications
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("[HOOK] POST hook failed: %s", r)
                continue

        return HookResult(proceed=True, modified_context=None, error=None)

    # ------------------------------------------------------------------
    # Capability enforcement
    # ------------------------------------------------------------------

    async def _execute_with_enforcement(
        self,
        entry: _ScopedEntry,
        context: HookContext,
    ) -> HookResult:
        """Execute a single hook handler with capability enforcement.

        - Timeout: asyncio.wait_for with spec.capabilities.max_timeout_ms
        - Veto override: if can_veto=False, override proceed=False to True
        - Modify override: if can_modify_context=False, strip modified_context
        """
        caps = entry.spec.capabilities
        timeout_s = caps.max_timeout_ms / 1000.0

        try:
            result = await asyncio.wait_for(
                entry.handler(context),
                timeout=timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "[HOOK] Handler %r timed out after %dms — skipping",
                entry.spec.handler_name,
                caps.max_timeout_ms,
            )
            return HookResult(proceed=True, modified_context=None, error=None)
        except Exception as exc:
            logger.warning(
                "[HOOK] Handler %r raised %s — skipping",
                entry.spec.handler_name,
                exc,
            )
            return HookResult(proceed=True, modified_context=None, error=None)

        # Enforce capabilities
        proceed = result.proceed
        modified_context = result.modified_context
        error = result.error

        if not caps.can_veto and not proceed:
            logger.warning(
                "[HOOK] Handler %r declared can_veto=False but returned proceed=False — overriding",
                entry.spec.handler_name,
            )
            proceed = True
            error = None

        if not caps.can_modify_context and modified_context is not None:
            logger.warning(
                "[HOOK] Handler %r declared can_modify_context=False but returned modified_context — stripping",
                entry.spec.handler_name,
            )
            modified_context = None

        return HookResult(proceed=proceed, modified_context=modified_context, error=error)

    # ------------------------------------------------------------------
    # Agent cleanup
    # ------------------------------------------------------------------

    async def cleanup_agent_hooks(self, agent_id: str) -> int:
        """Remove all hooks scoped to a specific agent.

        Called when an agent disconnects (transitions to IDLE/SUSPENDED).
        Returns the number of hooks removed.
        """
        removed = 0
        async with self._lock:
            # Find all (phase, agent_id) keys for this agent
            keys_to_remove = [key for key in self._agent_hooks if key[1] == agent_id]
            for key in keys_to_remove:
                entries = self._agent_hooks.pop(key, [])
                for entry in entries:
                    self._id_to_entry.pop(entry.hook_id.id, None)
                    removed += 1

        if removed:
            logger.info("[HOOK] Cleaned up %d hooks for agent %r", removed, agent_id)
        return removed

    def describe(self) -> str:
        """Describe the hook engine chain for debugging."""
        global_count = sum(len(v) for v in self._global_hooks.values())
        agent_count = sum(len(v) for v in self._agent_hooks.values())
        return (
            f"ScopedHookEngine(global={global_count}, agent_scoped={agent_count}) "
            f"→ {self._inner.__class__.__name__}"
        )


# ---------------------------------------------------------------------------
# Factory: agent state → hook cleanup handler
# ---------------------------------------------------------------------------

_CLEANUP_STATES = frozenset({"IDLE", "SUSPENDED"})


def create_agent_cleanup_handler(
    engine: ScopedHookEngine,
) -> Callable[..., Awaitable[None]]:
    """Create an ``AgentStateHandler`` that cleans up hooks on agent disconnect.

    Returns a coroutine function compatible with ``AgentStateEmitter.add_handler()``.
    Triggers ``cleanup_agent_hooks`` when an agent transitions to IDLE or SUSPENDED.
    """

    async def _handler(event: object) -> None:
        # Duck-type: event has agent_id and new_state attrs
        new_state: str = getattr(event, "new_state", "")
        if new_state not in _CLEANUP_STATES:
            return
        agent_id: str = getattr(event, "agent_id", "")
        if agent_id:
            await engine.cleanup_agent_hooks(agent_id)

    return _handler
