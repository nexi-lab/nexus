"""Thin Python adapter for the Rust AcpService.

After the cutover, the Python ``services.acp`` package is gone. The
dispatch consumer + any other in-process callers that need to fire an
ACP call go through this adapter, which talks to the Rust
``AcpService`` through the kernel's gRPC ``_call`` dispatch primitive.

The dispatch path goes through the nexus-cluster process where the Rust
``call_agent`` runs the subprocess + ACP session.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AcpCallResult:
    """Mirror of the Rust ``AcpResult`` shape (commit 20)."""

    pid: str = ""
    agent_id: str = ""
    exit_code: int = 0
    response: str = ""
    raw_stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class AcpAdapter:
    """Calls into the Rust AcpService via ``nx_kernel_dispatch_rust_call``."""

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel

    async def call_agent(
        self,
        *,
        agent_id: str,
        prompt: str,
        owner_id: str,
        zone_id: str,
        cwd: str = ".",
        timeout: float = 300.0,
        session_id: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> AcpCallResult:
        del labels  # rust side fills service/agent_id labels itself
        payload = json.dumps(
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "cwd": cwd,
                "timeout": timeout,
                "session_id": session_id,
                "context": {"zone_id": zone_id, "user_id": owner_id},
            }
        ).encode()
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, self._dispatch, "acp_call", payload)
        body = json.loads(raw)
        return AcpCallResult(
            pid=body.get("pid", ""),
            agent_id=body.get("agent_id", ""),
            exit_code=int(body.get("exit_code", 0)),
            response=body.get("response", ""),
            raw_stdout=body.get("raw_stdout", ""),
            stderr=body.get("stderr", ""),
            timed_out=bool(body.get("timed_out", False)),
            metadata=body.get("metadata") or {},
        )

    def _dispatch(self, method: str, payload: bytes) -> Any:
        # Dispatch via the kernel's gRPC Call RPC. The kernel client
        # routes to the same Kernel::dispatch_rust_call lookup that the
        # tonic Call handler uses.
        _call = getattr(self._kernel, "_call", None)
        if _call is None:
            raise RuntimeError("ACP dispatch requires nexus-cluster gRPC — use KernelClient._call")
        result = _call(f"acp/{method}", payload)
        if result is None:
            raise RuntimeError("AcpService not installed on the cluster process")
        return result
