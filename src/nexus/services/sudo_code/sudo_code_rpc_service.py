"""SudoCodeRPCService — gRPC handler stub for sudowork ↔ nexusd session lifecycle.

Surface (matches `proto/nexus/grpc/sudo_code/sudo_code.proto`):

  start_session(agent, repos, model)        → {session_id, agent_id, workspace_path}
  cancel(session_id, mode)                  → {cancelled}
  get_session(session_id)                   → {agent_id, agent, workspace_path,
                                               model, state}

Reachable from sudowork via the kernel's gRPC `Call(method, payload)` generic
dispatch (port 2028) — `@rpc_expose` registers each method into the dispatch
table that NexusVFSService consults on every Call. The narrow surface is
deliberate: prompt + event flow uses the chat-with-me VFS surface, not gRPC
methods that would duplicate it.

This is a stub. The real implementation is tracked as `sudo-code-grpc-service`
in sudowork's `OPEN-ITEMS.md` and lands once the AcpService persistent-spawn
mode is in place. Today every method raises `NotImplementedError` so a caller
that reaches it sees a clear actionable error rather than a silent zero-effect
return.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class SudoCodeRPCService:
    """RPC surface for sudo-code session lifecycle (stub)."""

    def __init__(self, acp_service: Any, agent_registry: Any) -> None:
        # AcpService — used in the follow-up impl to spawn the `scode serve`
        # subprocess, bind stdio to /proc/{pid}/fd/{0,1,2}, and drive ACP
        # JSON-RPC. Held as a forward reference here so the wiring path is
        # already in place when the stub is replaced.
        self._acp = acp_service
        # AgentRegistry — used in the follow-up impl for state lookup on
        # GetSession, and as the single authority for pid allocation at
        # StartSession time.
        self._agent_registry = agent_registry
        logger.debug("SudoCodeRPCService created (stub)")

    @rpc_expose(description="Spawn a sudo-code session — see OPEN-ITEMS#sudo-code-grpc-service")
    async def sudo_code_start_session(
        self,
        agent: str,
        repos: list[dict] | None = None,
        model: str = "",
        context: dict | None = None,
    ) -> dict:
        """Spawn a `scode serve` subprocess via AcpService and return identity.

        Stub: real implementation delegates to `AcpService.call_agent`-style
        persistent-spawn flow that allocates pid via AgentRegistry, builds
        /proc/{pid}/workspace/ with OS-level symlinks for `repos`, plants
        the chat-with-me DT_LINK, and binds the subprocess stdio to
        /proc/{pid}/fd/{0,1,2}. See sudowork OPEN-ITEMS#sudo-code-grpc-service.
        """
        del agent, repos, model, context
        raise NotImplementedError(
            "sudo_code_start_session: pending — see sudowork OPEN-ITEMS#sudo-code-grpc-service"
        )

    @rpc_expose(description="Cancel a sudo-code turn or session")
    async def sudo_code_cancel(
        self,
        session_id: str,
        mode: str = "cancel_turn",
        context: dict | None = None,
    ) -> dict:
        """Abort the in-flight turn or terminate the entire session.

        Stub: see OPEN-ITEMS#sudo-code-grpc-service.
        """
        del session_id, mode, context
        raise NotImplementedError(
            "sudo_code_cancel: pending — see sudowork OPEN-ITEMS#sudo-code-grpc-service"
        )

    @rpc_expose(description="Get a sudo-code session's liveness snapshot")
    async def sudo_code_get_session(
        self,
        session_id: str,
        context: dict | None = None,
    ) -> dict:
        """Return AgentTable state + workspace path for a session.

        Stub: see OPEN-ITEMS#sudo-code-grpc-service. Cheap by design — for
        the live message flow callers should sys_watch the chat-with-me
        path, not poll this RPC.
        """
        del session_id, context
        raise NotImplementedError(
            "sudo_code_get_session: pending — see sudowork OPEN-ITEMS#sudo-code-grpc-service"
        )
