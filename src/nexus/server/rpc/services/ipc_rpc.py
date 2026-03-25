"""IPC RPC Service — inter-agent message passing.

Covers all ipc.py endpoints except SSE streaming.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class IpcRPCService:
    """RPC surface for IPC message operations."""

    def __init__(self, nexus_fs: Any, ipc_provisioner: Any | None = None) -> None:
        self._nexus_fs = nexus_fs
        self._ipc_provisioner = ipc_provisioner

    @rpc_expose(description="Send an IPC message to an agent")
    async def ipc_send(
        self,
        to_agent: str,
        content: str,
        from_agent: str = "system",
        message_type: str = "text",
        zone_id: str = "root",
    ) -> dict[str, Any]:
        from nexus.contracts.types import OperationContext

        ctx = OperationContext(
            user_id=from_agent,
            groups=[],
            is_admin=True,
            is_system=True,
            zone_id=zone_id,
        )
        inbox_path = f"/{zone_id}/ipc/{to_agent}/inbox"
        import json
        import uuid
        from datetime import UTC, datetime

        envelope = {
            "message_id": str(uuid.uuid4()),
            "from": from_agent,
            "to": to_agent,
            "type": message_type,
            "content": content,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        filename = f"{envelope['message_id']}.json"
        await self._nexus_fs.write(
            f"{inbox_path}/{filename}",
            json.dumps(envelope).encode(),
            context=ctx,
        )
        return {"message_id": envelope["message_id"], "delivered": True}

    @rpc_expose(description="List messages in an agent's inbox")
    async def ipc_inbox(self, agent_id: str, zone_id: str = "root") -> dict[str, Any]:
        inbox_path = f"/{zone_id}/ipc/{agent_id}/inbox"
        try:
            entries = await self._nexus_fs.sys_readdir(inbox_path)
            return {"agent_id": agent_id, "messages": [str(e) for e in entries]}
        except Exception:
            return {"agent_id": agent_id, "messages": []}

    @rpc_expose(description="Provision IPC directories for an agent", admin_only=True)
    async def ipc_provision(self, agent_id: str, zone_id: str = "root") -> dict[str, Any]:
        if self._ipc_provisioner is None:
            return {"error": "IPC provisioner not available"}
        result = await self._ipc_provisioner.provision(agent_id, zone_id=zone_id)
        return {"agent_id": agent_id, "provisioned": True, "inbox": result.inbox_path}
