"""Client for the native daemon's transient agent registry."""

from __future__ import annotations

from typing import Any

from nexus.contracts.exceptions import BackendError
from nexus.remote.grpc_target import resolve_grpc_target
from nexus.remote.rpc_transport import RPCTransport


class AgentClient:
    """Own one authenticated channel; the daemon owns all registry state."""

    def __init__(self, url: str, api_key: str | None = None, *, timeout: float = 30) -> None:
        address, _, tls = resolve_grpc_target(url, trust_local_project=False)
        self._transport = RPCTransport(address, auth_token=api_key, tls_config=tls, timeout=timeout)

    def __enter__(self) -> AgentClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._transport.close()

    @staticmethod
    def _record(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("pid"), str):
            raise BackendError("Invalid native agent record")
        return value

    def register(
        self,
        *,
        name: str,
        owner_id: str | None = None,
        zone_id: str,
        connection_id: str,
        host_pid: int,
        local_id: int | None = None,
        parent_pid: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._record(
            self._transport.call_rpc(
                "agent_register_external",
                {
                    "name": name,
                    "owner_id": owner_id,
                    "zone_id": zone_id,
                    "connection_id": connection_id,
                    "host_pid": host_pid,
                    "local_id": local_id,
                    "parent_pid": parent_pid,
                    "labels": labels or {},
                },
            )
        )

    def list(
        self, *, zone_id: str | None = None, owner_id: str | None = None, state: str | None = None
    ) -> list[dict[str, Any]]:
        result = self._transport.call_rpc(
            "agent_list",
            {
                "zone_id": zone_id,
                "owner_id": owner_id,
                "state": state,
            },
        )
        if not isinstance(result, list):
            raise BackendError("Invalid native agent listing")
        return [self._record(item) for item in result]

    def get(self, pid: str) -> dict[str, Any]:
        result = self._transport.call_rpc("agent_get", {"pid": pid})
        return self._record(result)

    def unregister(self, pid: str) -> bool:
        result = self._transport.call_rpc("agent_unregister_external", {"pid": pid})
        if type(result) is not bool:
            raise BackendError("Invalid native agent unregister receipt")
        return result

    def update_state(self, pid: str, state: str) -> dict[str, Any]:
        return self._record(
            self._transport.call_rpc("agent_update_state", {"pid": pid, "state": state})
        )

    def heartbeat(self, pid: str) -> dict[str, Any]:
        return self._record(self._transport.call_rpc("agent_heartbeat", {"pid": pid}))

    def signal(self, pid: str, signal: str, payload: dict[str, str]) -> dict[str, Any]:
        return self._record(
            self._transport.call_rpc(
                "agent_signal", {"pid": pid, "sig": signal, "payload": payload}
            )
        )
