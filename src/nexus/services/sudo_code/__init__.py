"""sudo-code service — gRPC surface for sudowork → nexusd session lifecycle.

Spawn / cancel / liveness only. Prompt and event flow uses the existing
chat-with-me VFS surface (`/agents/{name}/chat-with-me` writes go
through agent_chat resolver + mailbox stamping; reads use sys_watch).
See ``docs/architecture/...`` and the sudowork integration design for
the broader path layout.

The runtime task that backs each session is registered into the
``AgentRuntimeRegistry`` slot at module init by the in-process
sudo-code crate (a Rust crate linked into nexusd, parallel to
``NativeInterceptHook`` registration). When no runtime is registered
for an agent name, ``SudoCodeRPCService.start_session`` still creates
the AgentRegistry record so a follow-up runtime install can pick it
up — the gRPC contract is decoupled from the runtime wire-up timing.
"""

from nexus.services.sudo_code.runtime_registry import (
    AgentRuntime,
    AgentRuntimeRegistry,
)
from nexus.services.sudo_code.sudo_code_rpc_service import SudoCodeRPCService

__all__ = ["AgentRuntime", "AgentRuntimeRegistry", "SudoCodeRPCService"]
