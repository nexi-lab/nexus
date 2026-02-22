"""Scoped hook engine — per-agent scoping + verified execution (Issue #1257, #2064).

Wrapping layer over ``AsyncHookEngine`` (Mechanism 2: same-Protocol wrapping)
per NEXUS-LEGO-ARCHITECTURE Section 4.3.

Features:
    - **Per-agent scoping**: Hooks can target a specific agent via ``HookSpec.agent_scope``.
    - **Verified execution**: ``HookCapabilities`` are enforced at runtime (veto override,
      context modification override, per-handler timeout).
    - **Dual-index O(1) lookup**: Separate indexes for global and agent-scoped hooks.
    - **Mutating/Validating phases** (Issue #2064): Within PRE phases, mutating hooks
      run first (sequential, can modify + veto) then validating hooks (sequential,
      can veto only, sees final mutated context). Modeled after Kubernetes admission
      controllers.
    - **Failure policies** (Issue #2064): Per-hook ``FailurePolicy.FAIL`` or ``IGNORE``
      determines behavior on handler error/timeout.
    - **Context threading**: Mutating hooks thread ``modified_context`` through the chain
      so each hook sees the result of previous mutations.
    - **Lock-free fire()**: No lock on the read path; lock only on register/unregister.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md Section 4.3 (Recursive Wrapping)
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md Section 7 (eBPF / BPF LSM analogy)
    - Issue #1257: Hook engine per-agent scoping and verified execution
    - Issue #2064: Mutating/validating phases with failure policies
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from nexus.services.protocols.hook_engine import (
    FailurePolicy,
    HookContext,
    HookId,
    HookPhaseType,
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


def _effective_phase_type(entry: _ScopedEntry) -> HookPhaseType:
    """Determine the effective phase type for an entry.

    If ``HookSpec.phase_type`` is set, use it. Otherwise default to
    MUTATING (backward compatible with pre-#2064 hooks).
    """
    if entry.spec.phase_type is not None:
        return entry.spec.phase_type
    # Legacy hooks without phase_type default to MUTATING
    return HookPhaseType.MUTATING


_PROCEED = HookResult(proceed=True, modified_context=None, error=None)

# ---------------------------------------------------------------------------
# ScopedHookEngine
# ---------------------------------------------------------------------------


class ScopedHookEngine:
    """Wrapping layer over AsyncHookEngine adding agent scoping + verified execution.

    Follows NEXUS-LEGO-ARCHITECTURE Section 4.3 Recursive Wrapping pattern.
    Satisfies ``HookEngineProtocol`` via duck typing.

    Issue #2064 additions:
        - PRE phases partition hooks into MUTATING (runs first) and VALIDATING
          (runs second, sees final mutated context).
        - Failure policies: ``FailurePolicy.FAIL`` on error/timeout returns
          ``proceed=False`` instead of silently continuing.
        - Context threading: mutating hooks thread ``modified_context`` through
          the chain via ``dataclasses.replace()``.

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
        self._global_hooks: dict[str, list[_ScopedEntry]] = {}  # phase -> entries
        self._agent_hooks: dict[
            tuple[str, str], list[_ScopedEntry]
        ] = {}  # (phase, agent_id) -> entries
        self._id_to_entry: dict[str, _ScopedEntry] = {}  # hook_id.id -> entry
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

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[HOOK] Registered %r (phase=%s, scope=%s, priority=%d, phase_type=%s, failure_policy=%s)",
                spec.handler_name,
                spec.phase,
                spec.agent_scope or "global",
                spec.priority,
                spec.phase_type or "auto",
                spec.capabilities.failure_policy,
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
    # Fire -- lock-free read path
    # ------------------------------------------------------------------

    async def fire(self, phase: str, context: HookContext) -> HookResult:
        """Fire hooks for a given phase and context.

        - Merges global hooks + agent-scoped hooks (if context.agent_id matches).
        - PRE phases (Issue #2064): partitions into MUTATING (sequential, runs
          first, can modify + veto, context threading) then VALIDATING
          (sequential, can veto only, sees final mutated context).
        - POST phases: concurrent execution via asyncio.gather().
        """
        # Collect applicable hooks (no lock needed -- cooperative asyncio)
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
            return _PROCEED

        if _is_pre_phase(phase):
            return await self._fire_pre_phase(entries, context)
        return await self._fire_concurrent(entries, context)

    async def _fire_pre_phase(
        self,
        entries: list[_ScopedEntry],
        context: HookContext,
    ) -> HookResult:
        """Execute PRE phase hooks: MUTATING first, then VALIDATING (Issue #2064).

        Mutating hooks:
          - Sequential in priority order.
          - Can modify context (threaded via dataclasses.replace).
          - Can veto.

        Validating hooks:
          - Sequential in priority order (after all mutating hooks).
          - Can veto but CANNOT modify context.
          - Sees the final mutated context from the mutating phase.
        """
        # Partition entries by phase type (pre-sorted by priority within each group)
        mutating: list[_ScopedEntry] = []
        validating: list[_ScopedEntry] = []

        for entry in entries:
            pt = _effective_phase_type(entry)
            if pt == HookPhaseType.VALIDATING:
                validating.append(entry)
            else:
                mutating.append(entry)

        # Phase 1: Run MUTATING hooks sequentially with context threading
        current_context = context
        last_modified: dict[str, object] | None = None

        for entry in mutating:
            result = await self._execute_with_enforcement(entry, current_context)

            if not result.proceed:
                return result

            if result.modified_context is not None:
                last_modified = result.modified_context
                # Thread modified context to next hook (Issue #2064 / Issue #8)
                current_context = replace(
                    current_context,
                    payload=dict(result.modified_context),
                )

        # Phase 2: Run VALIDATING hooks sequentially (sees final mutated context)
        for entry in validating:
            result = await self._execute_with_enforcement(entry, current_context)

            if not result.proceed:
                return result

            # Validating hooks cannot modify — enforced by _execute_with_enforcement
            # but double-check here (belt + suspenders)
            if result.modified_context is not None and logger.isEnabledFor(logging.WARNING):
                logger.warning(
                    "[HOOK] Validating hook %r returned modified_context — stripping",
                    entry.spec.handler_name,
                )

        return HookResult(proceed=True, modified_context=last_modified, error=None)

    async def _fire_concurrent(
        self,
        entries: list[_ScopedEntry],
        context: HookContext,
    ) -> HookResult:
        """Execute hooks concurrently (POST phases).

        Issue #2064: POST hooks with ``failure_policy=FAIL`` that error
        cause the result to indicate failure. Structured logging for all
        POST hook results.
        """

        async def _run_one(entry: _ScopedEntry) -> tuple[_ScopedEntry, HookResult]:
            result = await self._execute_with_enforcement(entry, context)
            return (entry, result)

        raw_results = await asyncio.gather(
            *(_run_one(e) for e in entries),
            return_exceptions=True,
        )

        # Check for failure-policy violations and log results
        for r in raw_results:
            if isinstance(r, BaseException):
                # This shouldn't happen since _execute_with_enforcement catches
                # exceptions, but handle defensively.
                logger.warning("[HOOK] POST hook gather failed: %s", r)
                continue

            entry, result = r
            if not result.proceed:
                # A POST hook with failure_policy=FAIL returned proceed=False
                if logger.isEnabledFor(logging.WARNING):
                    logger.warning(
                        "[HOOK] POST hook %r failed (failure_mode=%s): %s",
                        entry.spec.handler_name,
                        result.failure_mode,
                        result.error,
                    )
                return result

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[HOOK] POST hook %r completed (modified=%s)",
                    entry.spec.handler_name,
                    result.modified_context is not None,
                )

        return _PROCEED

    # ------------------------------------------------------------------
    # Capability enforcement (Issue #1257 + #2064)
    # ------------------------------------------------------------------

    async def _execute_with_enforcement(
        self,
        entry: _ScopedEntry,
        context: HookContext,
    ) -> HookResult:
        """Execute a single hook handler with capability enforcement.

        - Timeout: asyncio.wait_for with spec.capabilities.max_timeout_ms
        - Failure policy (Issue #2064): FAIL -> proceed=False on error/timeout;
          IGNORE -> proceed=True (backward compatible).
        - Veto override: if can_veto=False, override proceed=False to True
        - Modify override: if can_modify_context=False, strip modified_context
        """
        caps = entry.spec.capabilities
        timeout_s = caps.max_timeout_ms / 1000.0
        policy = caps.failure_policy

        try:
            result = await asyncio.wait_for(
                entry.handler(context),
                timeout=timeout_s,
            )
        except TimeoutError:
            if policy == FailurePolicy.FAIL:
                logger.warning(
                    "[HOOK] Handler %r timed out after %dms (failure_policy=FAIL) — aborting",
                    entry.spec.handler_name,
                    caps.max_timeout_ms,
                )
                return HookResult(
                    proceed=False,
                    modified_context=None,
                    error=f"Hook '{entry.spec.handler_name}' timed out after {caps.max_timeout_ms}ms",
                    failure_mode="timeout",
                )
            logger.warning(
                "[HOOK] Handler %r timed out after %dms — skipping",
                entry.spec.handler_name,
                caps.max_timeout_ms,
            )
            return _PROCEED
        except Exception as exc:
            if policy == FailurePolicy.FAIL:
                logger.warning(
                    "[HOOK] Handler %r raised %s (failure_policy=FAIL) — aborting",
                    entry.spec.handler_name,
                    exc,
                )
                return HookResult(
                    proceed=False,
                    modified_context=None,
                    error=f"Hook '{entry.spec.handler_name}' failed: {exc}",
                    failure_mode="error",
                )
            logger.warning(
                "[HOOK] Handler %r raised %s — skipping",
                entry.spec.handler_name,
                exc,
            )
            return _PROCEED

        # Enforce capabilities
        proceed = result.proceed
        modified_context = result.modified_context
        error = result.error
        failure_mode = result.failure_mode

        if not caps.can_veto and not proceed:
            logger.warning(
                "[HOOK] Handler %r declared can_veto=False but returned proceed=False — overriding",
                entry.spec.handler_name,
            )
            proceed = True
            error = None
            failure_mode = None

        if not caps.can_modify_context and modified_context is not None:
            logger.warning(
                "[HOOK] Handler %r declared can_modify_context=False but returned modified_context — stripping",
                entry.spec.handler_name,
            )
            modified_context = None

        # Enforce phase_type constraints (Issue #2064)
        if entry.spec.phase_type == HookPhaseType.VALIDATING and modified_context is not None:
            logger.warning(
                "[HOOK] Validating hook %r returned modified_context — stripping per phase_type",
                entry.spec.handler_name,
            )
            modified_context = None

        # Set failure_mode for explicit vetoes
        if not proceed and failure_mode is None:
            failure_mode = "veto"

        return HookResult(
            proceed=proceed,
            modified_context=modified_context,
            error=error,
            failure_mode=failure_mode,
        )

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
            f"-> {self._inner.__class__.__name__}"
        )


# ---------------------------------------------------------------------------
# Factory: agent state -> hook cleanup handler
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
