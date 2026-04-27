"""sudo-code service — gRPC surface for sudowork → nexusd session lifecycle.

Spawn / cancel / liveness only. Prompt and event flow uses the existing
chat-with-me VFS surface (`/agents/{name}/chat-with-me` writes go
through agent_chat resolver + mailbox stamping; reads use sys_watch).
See ``docs/architecture/...`` and the sudowork integration design for
the broader path layout.

The actual subprocess spawn (and ACP JSON-RPC over stdio plumbing) is
delegated to ``nexus.services.acp.service.AcpService`` — sudo-code is
just another ACP backend, same shape claude / codex / codebuddy use.
"""

from nexus.services.sudo_code.sudo_code_rpc_service import SudoCodeRPCService

__all__ = ["SudoCodeRPCService"]
