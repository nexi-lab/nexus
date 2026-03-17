"""ACP RPC Service — JSON-RPC facade for the ACP coding agent caller.

Exposes ``AcpService`` operations via ``@rpc_expose`` so they are
discoverable by the RPC server and callable over HTTP JSON-RPC.

Follows the same pattern as ``AgentRPCService``.
"""

import dataclasses
import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class AcpRPCService:
    """RPC surface for ACP coding agent operations."""

    def __init__(self, acp_service: Any) -> None:
        self._acp = acp_service
        logger.debug("AcpRPCService created")

    # ------------------------------------------------------------------
    # Context helpers
    # ------------------------------------------------------------------

    def _zone_id(self, context: Any | None) -> str:
        fallback: str = self._acp.default_zone_id
        if context is None:
            return fallback
        zid = (
            context.get("zone_id")
            if isinstance(context, dict)
            else getattr(context, "zone_id", None)
        )
        return str(zid) if zid else fallback

    @staticmethod
    def _owner_id(context: Any | None) -> str:
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

    @rpc_expose(description="Call a coding agent via ACP")
    async def acp_call(
        self,
        agent_id: str,
        prompt: str,
        cwd: str = ".",
        timeout: float = 300.0,
        session_id: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Call a coding agent and return the result."""
        result = await self._acp.call_agent(
            agent_id=agent_id,
            prompt=prompt,
            owner_id=self._owner_id(context),
            zone_id=self._zone_id(context),
            cwd=cwd,
            timeout=timeout,
            session_id=session_id,
        )
        return dataclasses.asdict(result)

    @rpc_expose(description="List available ACP agent configurations")
    def acp_list_agents(self, context: dict | None = None) -> list[dict]:  # noqa: ARG002
        """List built-in and registered agent configs."""
        agents = self._acp.agent_configs
        return [
            {
                "agent_id": cfg.agent_id,
                "name": cfg.name,
                "command": cfg.command,
                "enabled": cfg.enabled,
            }
            for cfg in agents.values()
        ]

    @rpc_expose(description="List running ACP processes")
    def acp_list_processes(self, context: dict | None = None) -> list[dict]:
        """List ACP-managed processes from the ProcessTable."""
        procs = self._acp.list_agents(
            zone_id=self._zone_id(context),
        )
        return [
            {
                "pid": p.pid,
                "name": p.name,
                "owner_id": p.owner_id,
                "zone_id": p.zone_id,
                "state": p.state.value if hasattr(p.state, "value") else str(p.state),
                "labels": dict(p.labels) if p.labels else {},
            }
            for p in procs
        ]

    @rpc_expose(description="Kill a running ACP agent process")
    async def acp_kill(self, pid: str, context: dict | None = None) -> dict:  # noqa: ARG002
        """Kill a running ACP agent by PID."""
        desc = self._acp.kill_agent(pid)
        return {
            "pid": desc.pid,
            "name": desc.name,
            "state": desc.state.value if hasattr(desc.state, "value") else str(desc.state),
        }

    @rpc_expose(description="Set system prompt for an ACP agent")
    async def acp_set_system_prompt(
        self,
        agent_id: str,
        content: str,
        context: dict | None = None,
    ) -> dict:
        """Set the system prompt for a coding agent."""
        zone_id = self._zone_id(context)
        await self._acp.set_system_prompt(agent_id, content, zone_id=zone_id)
        return {"agent_id": agent_id, "length": len(content)}

    @rpc_expose(description="Get system prompt for an ACP agent")
    async def acp_get_system_prompt(
        self,
        agent_id: str,
        context: dict | None = None,
    ) -> dict:
        """Get the system prompt for a coding agent."""
        zone_id = self._zone_id(context)
        prompt = await self._acp.get_system_prompt(agent_id, zone_id=zone_id)
        return {"agent_id": agent_id, "content": prompt}

    @rpc_expose(description="Set enabled skills for an ACP agent")
    async def acp_set_enabled_skills(
        self,
        agent_id: str,
        skills: list[dict],
        context: dict | None = None,
    ) -> dict:
        """Set the enabled skills for a coding agent."""
        zone_id = self._zone_id(context)
        await self._acp.set_enabled_skills(agent_id, skills, zone_id=zone_id)
        return {"agent_id": agent_id, "skills": skills}

    @rpc_expose(description="Get enabled skills for an ACP agent")
    async def acp_get_enabled_skills(
        self,
        agent_id: str,
        context: dict | None = None,
    ) -> dict:
        """Get the enabled skills for a coding agent."""
        zone_id = self._zone_id(context)
        skills = await self._acp.get_enabled_skills(agent_id, zone_id=zone_id)
        return {"agent_id": agent_id, "skills": skills}

    @rpc_expose(description="List ACP call history")
    async def acp_history(
        self,
        limit: int = 50,
        context: dict | None = None,
    ) -> list[dict]:
        """List past ACP call results."""
        zone_id = self._zone_id(context)
        result: list[dict] = await self._acp.get_call_history(zone_id=zone_id, limit=limit)
        return result
