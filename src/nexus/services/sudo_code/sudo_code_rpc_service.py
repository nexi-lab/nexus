"""SudoCodeRPCService — gRPC handler for sudowork ↔ nexusd session lifecycle.

Surface (matches `proto/nexus/grpc/sudo_code/sudo_code.proto`):

  start_session(agent, repos, model)        → {session_id, agent_id, workspace_path}
  cancel(session_id, mode)                  → {cancelled}
  get_session(session_id)                   → {agent_id, agent, workspace_path,
                                               state}

Reachable from sudowork via the kernel's gRPC `Call(method, payload)` generic
dispatch (port 2028) — `@rpc_expose` registers each method into the dispatch
table that NexusVFSService consults on every Call.

The narrow surface is deliberate: prompt + event flow uses the chat-with-me
VFS surface, not gRPC methods that would duplicate it.

Layering: this RPC class owns the `session_id ↔ pid` map and AgentRegistry
calls. The actual sudo-code agent loop is a separate Rust crate registered
into nexus's `AgentRuntimeRegistry` (kernel-knows trait DI, parallel to
`AcpService` for external ACP backends but in-process — sudo-code is our
code in our process, no stdio bind, no JSON-RPC). A registered runtime is
required: `start_session` reaps the AgentRegistry pid and raises
`RuntimeError` when the registry has nothing for the agent name. Returning
a session_id with no runtime driving it would be silent failure (sudowork
would wait for responses that never come).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from nexus.contracts.process_types import AgentKind
from nexus.contracts.rpc import rpc_expose
from nexus.services.sudo_code.runtime_registry import AgentRuntimeRegistry

logger = logging.getLogger(__name__)


class SudoCodeRPCService:
    """RPC surface for sudo-code session lifecycle.

    Spawn / cancel / liveness only — prompt and event flow uses the chat-with-me
    VFS surface (sudowork writes to `/proc/{pid}/chat-with-me`, reads from
    its own `/agents/{user}/chat-with-me` via `sys_watch`).

    A registered ``AgentRuntime`` for the requested ``agent`` is required —
    ``start_session`` reaps the AgentRegistry pid and raises ``RuntimeError``
    when the runtime registry has nothing for the agent name. The earlier
    "best-effort" fallback was removed: returning a session_id when no
    runtime is driving the pid is silent failure (sudowork would wait for
    responses that never come).
    """

    def __init__(
        self,
        agent_registry: Any,
        runtime_registry: AgentRuntimeRegistry | None = None,
        zone_id: str = "root",
    ) -> None:
        self._agent_registry = agent_registry
        # name → AgentRuntime slot (parallel to NativeInterceptHook
        # registration). Defaults to an empty registry so callers that
        # pass `None` still get a real object — but every spawn against
        # an empty/unregistered slot raises (see start_session below).
        self._runtime_registry = (
            runtime_registry if runtime_registry is not None else AgentRuntimeRegistry()
        )
        self._zone_id = zone_id
        # session_id is sudowork's stable handle across reconnects; pid is the
        # nexus AgentRegistry id. The map carries both directions so cancel /
        # get_session can reach AgentRegistry without sudowork having to track
        # the pid itself.
        self._sessions: dict[str, _Session] = {}
        logger.debug("SudoCodeRPCService created")

    # ------------------------------------------------------------------
    # Context helpers — same shape AcpRPCService uses
    # ------------------------------------------------------------------

    def _zone_id_for(self, context: Any | None) -> str:
        if context is None:
            return self._zone_id
        zid = (
            context.get("zone_id")
            if isinstance(context, dict)
            else getattr(context, "zone_id", None)
        )
        return str(zid) if zid else self._zone_id

    @staticmethod
    def _owner_id_for(context: Any | None) -> str:
        if context is None:
            return "system"
        uid = (
            context.get("user_id")
            if isinstance(context, dict)
            else getattr(context, "user_id", None)
        )
        return str(uid) if uid else "system"

    # ------------------------------------------------------------------
    # Public RPC methods
    # ------------------------------------------------------------------

    @rpc_expose(description="Spawn a sudo-code session in-process")
    async def sudo_code_start_session(
        self,
        agent: str,
        repos: list[dict] | None = None,
        model: str = "",
        context: dict | None = None,
    ) -> dict:
        """Spawn a sudo-code session against the named agent.

        Allocates a pid via AgentRegistry (single authority over identity),
        stores the session_id ↔ pid map, and asks the AgentRuntimeRegistry to
        start the in-process runtime task. Returns the identity tuple sudowork
        uses for follow-up cancel / get_session calls and chat-with-me writes.
        """
        if not agent:
            raise ValueError("sudo_code_start_session: 'agent' is required")

        zone_id = self._zone_id_for(context)
        owner_id = self._owner_id_for(context)
        labels: dict[str, str] = {"service": "sudo_code", "agent": agent}
        if model:
            labels["model"] = model

        desc = self._agent_registry.spawn(
            name=agent,
            owner_id=owner_id,
            zone_id=zone_id,
            kind=AgentKind.MANAGED,
            labels=labels,
        )
        pid = desc.pid
        workspace_path = f"/proc/{pid}/workspace/"
        session_id = f"sess-{uuid.uuid4().hex[:12]}"

        repos_list = list(repos) if repos else []

        # Runtime dispatch. The runtime registry is the kernel-knows slot
        # the sudo-code crate registers into at module init. A missing
        # runtime is a misconfiguration — we reap the AgentRegistry pid
        # and raise so sudowork sees a hard error instead of a session
        # that looks alive but has nothing driving it.
        try:
            runtime = self._runtime_registry.get(agent)
        except Exception as exc:  # registry surface is open — be defensive
            self._agent_registry.kill(pid, exit_code=-1)
            logger.error(
                "sudo_code_start_session: runtime registry lookup failed for %s: %s",
                agent,
                exc,
            )
            raise RuntimeError(
                f"sudo_code runtime registry lookup failed for {agent!r}: {exc}"
            ) from exc
        if runtime is None:
            self._agent_registry.kill(pid, exit_code=-1)
            logger.error(
                "sudo_code_start_session: no runtime registered for %s — "
                "the sudo-code crate must register an AgentRuntime under this "
                "name at module init",
                agent,
            )
            raise RuntimeError(f"sudo_code: no runtime registered for agent {agent!r}")
        try:
            runtime.spawn(
                pid=pid,
                workspace_path=workspace_path,
                repos=repos_list,
                model=model,
            )
        except Exception as exc:
            # Spawn failure leaves the agent in REGISTERED — reap the pid
            # so the AgentRegistry record does not drift.
            logger.error(
                "sudo_code_start_session: runtime.spawn failed for pid=%s: %s",
                pid,
                exc,
            )
            self._agent_registry.kill(pid, exit_code=-1)
            raise RuntimeError(f"sudo_code runtime for {agent!r} failed to spawn: {exc}") from exc

        self._sessions[session_id] = _Session(
            session_id=session_id,
            pid=pid,
            agent=agent,
            model=model,
            workspace_path=workspace_path,
        )
        logger.info(
            "sudo_code_start_session: session=%s pid=%s agent=%s",
            session_id,
            pid,
            agent,
        )
        return {
            "session_id": session_id,
            "agent_id": pid,
            "workspace_path": workspace_path,
        }

    @rpc_expose(description="Cancel a sudo-code turn or session")
    async def sudo_code_cancel(
        self,
        session_id: str,
        mode: str = "cancel_session",
        context: dict | None = None,
    ) -> dict:
        """Cancel an in-flight turn or terminate the entire session.

        ``mode`` is one of ``cancel_turn`` (abort current generation, keep
        the agent loop alive) or ``cancel_session`` (terminate the agent and
        unregister it from AgentRegistry).
        """
        del context
        sess = self._sessions.get(session_id)
        if sess is None:
            raise LookupError(f"sudo_code_cancel: unknown session_id {session_id!r}")

        normalized = (mode or "cancel_session").lower()
        if normalized not in {"cancel_turn", "cancel_session"}:
            raise ValueError(
                f"sudo_code_cancel: mode must be 'cancel_turn' or 'cancel_session', got {mode!r}"
            )

        cancelled = False
        try:
            runtime = self._runtime_registry.get(sess.agent)
        except Exception as exc:
            runtime = None
            logger.warning(
                "sudo_code_cancel: runtime registry lookup failed for %s: %s",
                sess.agent,
                exc,
            )
        if runtime is not None:
            try:
                runtime.cancel(pid=sess.pid, mode=normalized)
                cancelled = True
            except Exception as exc:
                logger.error(
                    "sudo_code_cancel: runtime.cancel failed for pid=%s: %s",
                    sess.pid,
                    exc,
                )

        if normalized == "cancel_session":
            try:
                self._agent_registry.kill(sess.pid, exit_code=0)
                cancelled = True
            except Exception as exc:
                # AgentRegistry.kill on an already-terminated pid is a no-op
                # in the shim, but defend against missing-pid races so sudowork
                # gets a clean response even if state already drifted.
                logger.warning(
                    "sudo_code_cancel: AgentRegistry.kill failed for pid=%s: %s",
                    sess.pid,
                    exc,
                )
            self._sessions.pop(session_id, None)

        return {"cancelled": cancelled}

    @rpc_expose(description="Get a sudo-code session's liveness snapshot")
    async def sudo_code_get_session(
        self,
        session_id: str,
        context: dict | None = None,
    ) -> dict:
        """Snapshot the session's identity + AgentRegistry state.

        Cheap by design — for the live message flow callers should sys_watch
        the chat-with-me path, not poll this RPC.
        """
        del context
        sess = self._sessions.get(session_id)
        if sess is None:
            raise LookupError(f"sudo_code_get_session: unknown session_id {session_id!r}")

        desc = self._agent_registry.get(sess.pid)
        state = (
            desc.state.value
            if desc and hasattr(desc.state, "value")
            else str(desc.state)
            if desc
            else "terminated"
        )
        return {
            "session_id": sess.session_id,
            "agent_id": sess.pid,
            "agent": sess.agent,
            "workspace_path": sess.workspace_path,
            "model": sess.model,
            "state": state,
        }


class _Session:
    """Per-session bookkeeping carried by SudoCodeRPCService.

    Lightweight value type — no methods. The kernel-side AgentTable is the
    state SSOT; this struct only carries the surface sudowork addresses
    (session_id, agent name, workspace) that the RPC handlers need to
    reach AgentRegistry on follow-up calls.
    """

    __slots__ = ("session_id", "pid", "agent", "model", "workspace_path")

    def __init__(
        self,
        session_id: str,
        pid: str,
        agent: str,
        model: str,
        workspace_path: str,
    ) -> None:
        self.session_id = session_id
        self.pid = pid
        self.agent = agent
        self.model = model
        self.workspace_path = workspace_path
