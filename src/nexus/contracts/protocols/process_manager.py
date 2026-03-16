"""ProcessManager protocol — agent process lifecycle contract.

Defines the kernel contract for agent process creation,
signaling, and termination. Modeled after Linux kernel/fork.c,
kernel/exit.c, and kernel/signal.c.

Design doc: docs/design/AGENT-PROCESS-ARCHITECTURE.md §5.2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.system_services.agent_runtime.types import (
        AgentProcess,
        AgentProcessConfig,
        AgentSignal,
    )


@runtime_checkable
class ProcessManagerProtocol(Protocol):
    """Kernel contract for agent process lifecycle.

    Linux analogue: kernel/fork.c + kernel/exit.c + kernel/signal.c.

    Manages agent processes: creation (fork/exec), termination (exit),
    and signaling (steering messages).
    """

    async def spawn(
        self,
        owner_id: str,
        zone_id: str,
        *,
        config: AgentProcessConfig,
        parent_pid: str | None = None,
    ) -> AgentProcess:
        """Create a new agent process (fork+exec).

        Allocates PID, creates home directory in NexusFS, writes
        SYSTEM.md and settings.json, and stores process in the
        in-memory process table.
        """
        ...

    async def get_process(self, pid: str) -> AgentProcess | None:
        """Read process descriptor (``/proc/PID/status`` equivalent)."""
        ...

    async def terminate(
        self,
        pid: str,
        *,
        exit_code: int = 0,
    ) -> None:
        """Terminate an agent process.

        Closes all fds, saves final checkpoint, transitions to ZOMBIE,
        and removes from the process table.
        """
        ...

    async def signal(
        self,
        pid: str,
        sig: AgentSignal,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Send a signal to an agent process.

        SIGSTOP -> suspend (transition to STOPPED)
        SIGCONT -> continue (transition to SLEEPING)
        SIGTERM -> graceful shutdown
        SIGKILL -> immediate termination
        SIGUSR1 -> steering message injection
        """
        ...

    async def list_processes(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
    ) -> list[AgentProcess]:
        """List processes (``ps`` equivalent)."""
        ...
