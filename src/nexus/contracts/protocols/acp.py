"""AcpServiceProtocol — stateless coding agent CLI caller contract.

Defines the contract for the ACP (Agent CLI Protocol) service,
which calls coding agent CLIs (Claude Code, Gemini CLI, Codex, etc.)
as stateless one-shot subprocesses tracked by the kernel AgentRegistry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.process_types import AgentDescriptor


@runtime_checkable
class AcpServiceProtocol(Protocol):
    """Stateless coding agent CLI caller — one-shot subprocess calls."""

    async def call_agent(
        self,
        agent_id: str,
        prompt: str,
        owner_id: str,
        zone_id: str,
        *,
        cwd: str = ".",
        timeout: float = 300.0,
        labels: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> Any: ...

    def list_agents(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
    ) -> list[AgentDescriptor]: ...

    def kill_agent(self, pid: str) -> AgentDescriptor: ...

    def close_all(self) -> None: ...
