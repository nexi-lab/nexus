"""Sandbox authentication service (Issue #1307).

Orchestrates sandbox creation through the AgentRegistry, enforcing the
Agent OS design principle: *the sandbox is a platform service that USES
kernel primitives, not a kernel component*.

Pipeline: validate agent → check ownership → transition to CONNECTED →
          construct namespace → check budget → create sandbox → record event.

Design decisions:
    - #1A: Thin orchestration layer above SandboxManager
    - #8A: ``agent_id`` is required (non-optional) at this layer
    - #13A: Sync AgentRegistry calls wrapped in ``asyncio.to_thread``
    - #15A: Budget enforcement gated by feature flag
    - #4A / #16C: Events emitted synchronously from this service
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.sandbox.protocols import AgentEventLogProtocol
    from nexus.bricks.sandbox.sandbox_manager import SandboxManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SandboxAuthResult:
    """Immutable result of authenticated sandbox creation."""

    sandbox: dict[str, Any]
    agent_record: Any
    mount_table: list[Any] = field(default_factory=list)  # list[MountEntry]
    budget_checked: bool = False


class SandboxAuthService:
    """Orchestrates sandbox creation through AgentRegistry.

    This is a platform service that uses kernel primitives (AgentRegistry,
    NamespaceManager). The sandbox doesn't bypass the kernel for auth.

    Args:
        agent_registry: Process lifecycle table (required).
        sandbox_manager: Infrastructure-layer sandbox lifecycle manager (required).
        namespace_manager: Per-subject namespace visibility (optional).
        nexus_pay: Budget enforcement SDK (optional).
        event_log: Agent event audit log (optional).
        budget_enforcement: When True AND nexus_pay is provided, checks
            agent budget before sandbox creation.
    """

    def __init__(
        self,
        agent_registry: Any,  # AgentRegistry injected via DI
        sandbox_manager: "SandboxManager",
        namespace_manager: Any = None,
        nexus_pay: Any = None,
        event_log: "AgentEventLogProtocol | None" = None,
        budget_enforcement: bool = False,
    ) -> None:
        self._agent_registry = agent_registry
        self._sandbox_manager = sandbox_manager
        self._namespace_manager = namespace_manager
        self._nexus_pay = nexus_pay
        self._event_log = event_log
        self._budget_enforcement = budget_enforcement

    async def _record_event(
        self,
        agent_id: str,
        event_type: str,
        zone_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Record an audit event (best-effort, never raises).

        Args:
            agent_id: Agent identifier.
            event_type: Event type string (e.g. "sandbox.created").
            zone_id: Optional zone ID.
            payload: Optional event payload dict.
        """
        if self._event_log is None:
            return
        try:
            kwargs: dict[str, Any] = {"agent_id": agent_id, "event_type": event_type}
            if zone_id is not None:
                kwargs["zone_id"] = zone_id
            if payload is not None:
                kwargs["payload"] = payload
            await asyncio.to_thread(self._event_log.record, **kwargs)
        except Exception:
            logger.warning(
                "[SANDBOX-AUTH] Failed to record %s event for agent %s",
                event_type,
                agent_id,
                exc_info=True,
            )

    async def create_sandbox(
        self,
        agent_id: str,
        owner_id: str,
        zone_id: str,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        sandbox_cost: float = 1.0,
    ) -> SandboxAuthResult:
        """Create a sandbox through the AgentRegistry authentication pipeline.

        Pipeline:
            1. Validate agent exists in process table
            2. Verify ownership (agent belongs to owner_id)
            3. Signal SIGCONT to transition agent to CONNECTED (new session)
            4. Construct namespace from grants (if NamespaceManager available)
            5. Check budget (if budget_enforcement enabled)
            6. Delegate sandbox creation to SandboxManager
            7. Record lifecycle event

        Args:
            agent_id: Agent identifier (required — validated against process table).
            owner_id: User who owns the agent.
            zone_id: Zone for multi-zone isolation.
            name: User-friendly sandbox name.
            ttl_minutes: Idle timeout in minutes.
            provider: Sandbox provider ("docker", "e2b").
            template_id: Provider template ID.
            sandbox_cost: Cost to check against budget (default 1.0 credit).

        Returns:
            SandboxAuthResult with sandbox metadata, agent record, and mount table.

        Raises:
            ValueError: If agent not found or budget insufficient.
            PermissionError: If ownership validation fails.
            InvalidTransitionError: If agent cannot transition to CONNECTED.
        """
        # Step 1: Validate agent exists
        agent_record = await asyncio.to_thread(self._agent_registry.get, agent_id)
        if agent_record is None:
            raise ValueError(f"Agent '{agent_id}' not found in process table")

        # Step 2: Verify ownership
        owns = agent_record is not None and agent_record.owner_id == owner_id
        if not owns:
            raise PermissionError(
                f"Ownership validation failed: user '{owner_id}' does not own agent '{agent_id}'"
            )

        # Step 3: Signal agent SIGCONT (transition to CONNECTED)
        from nexus.contracts.process_types import AgentSignal

        connected_record = await asyncio.to_thread(
            self._agent_registry.signal, agent_id, AgentSignal.SIGCONT
        )

        # Step 4: Construct namespace (best-effort — failure doesn't block sandbox)
        mount_table: list[Any] = []
        if self._namespace_manager is not None:
            try:
                mount_table = await asyncio.to_thread(
                    self._namespace_manager.get_mount_table,
                    ("agent", agent_id),
                    zone_id,
                )
            except Exception:
                logger.warning(
                    "[SANDBOX-AUTH] Namespace construction failed for agent %s, "
                    "continuing with empty mount table",
                    agent_id,
                    exc_info=True,
                )

        # Step 5: Check budget (gated by feature flag)
        budget_checked = False
        if self._budget_enforcement and self._nexus_pay is not None:
            can_afford = await self._nexus_pay.can_afford(sandbox_cost)
            if not can_afford:
                raise ValueError(
                    f"Budget insufficient: agent '{agent_id}' cannot afford "
                    f"sandbox creation (cost={sandbox_cost})"
                )
            budget_checked = True

        # Step 6: Delegate to SandboxManager
        sandbox = await self._sandbox_manager.create_sandbox(
            name=name,
            user_id=owner_id,
            zone_id=zone_id,
            agent_id=agent_id,
            ttl_minutes=ttl_minutes,
            provider=provider,
            template_id=template_id,
        )

        # Step 7: Record lifecycle event (best-effort)
        await self._record_event(
            agent_id=agent_id,
            event_type="sandbox.created",
            zone_id=zone_id,
            payload={
                "sandbox_id": sandbox.get("sandbox_id"),
                "name": name,
                "provider": sandbox.get("provider"),
                "mount_paths": [getattr(m, "virtual_path", str(m)) for m in mount_table],
            },
        )

        return SandboxAuthResult(
            sandbox=sandbox,
            agent_record=connected_record,
            mount_table=mount_table,
            budget_checked=budget_checked,
        )

    async def stop_sandbox(
        self,
        sandbox_id: str,
        agent_id: str,
    ) -> dict[str, Any]:
        """Stop a sandbox and transition the agent to IDLE.

        Args:
            sandbox_id: Sandbox to stop.
            agent_id: Agent that owns the sandbox.

        Returns:
            Sandbox metadata dict with updated status.
        """
        # Stop the sandbox
        result = await self._sandbox_manager.stop_sandbox(sandbox_id)

        # Signal agent SIGSTOP (transition to IDLE — best-effort)
        try:
            from nexus.contracts.process_types import AgentSignal

            await asyncio.to_thread(self._agent_registry.signal, agent_id, AgentSignal.SIGSTOP)
        except Exception:
            logger.warning(
                "[SANDBOX-AUTH] Failed to transition agent %s to IDLE after stop",
                agent_id,
                exc_info=True,
            )

        # Record event (best-effort)
        await self._record_event(
            agent_id=agent_id,
            event_type="sandbox.stopped",
            payload={"sandbox_id": sandbox_id},
        )

        return result

    async def connect_sandbox(
        self,
        sandbox_id: str,
        agent_id: str,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
    ) -> dict[str, Any]:
        """Connect and mount Nexus to a sandbox with namespace awareness.

        Args:
            sandbox_id: Sandbox to connect.
            agent_id: Agent that owns the sandbox.
            mount_path: FUSE mount path inside sandbox.
            nexus_url: Nexus server URL.
            nexus_api_key: Nexus API key.

        Returns:
            Connection result dict.
        """
        # Delegate to SandboxManager
        result = await self._sandbox_manager.connect_sandbox(
            sandbox_id=sandbox_id,
            agent_id=agent_id,
            mount_path=mount_path,
            nexus_url=nexus_url,
            nexus_api_key=nexus_api_key,
        )

        # Record event (best-effort)
        await self._record_event(
            agent_id=agent_id,
            event_type="sandbox.connected",
            payload={
                "sandbox_id": sandbox_id,
                "mount_path": mount_path,
            },
        )

        return result
