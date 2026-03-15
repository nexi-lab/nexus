"""AcpServiceProtocol — stateless coding agent CLI caller contract.

Defines the contract for the ACP (Agent CLI Protocol) system service,
which calls coding agent CLIs (Claude Code, Gemini CLI, Codex, etc.)
as stateless one-shot subprocesses tracked by the kernel ProcessTable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.process_types import ProcessDescriptor
    from nexus.system_services.acp.agents import AgentConfig
    from nexus.system_services.acp.service import AcpResult


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
    ) -> AcpResult: ...

    def list_agents(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
    ) -> list[ProcessDescriptor]: ...

    def kill_agent(self, pid: str) -> ProcessDescriptor: ...

    def register_agent(self, config: AgentConfig) -> None: ...

    def close_all(self) -> None: ...
