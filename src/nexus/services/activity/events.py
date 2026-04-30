"""ActivityEvent schema for issue #3791 foundation slice.

Frozen dataclasses + enums. No I/O, no side effects — safe to import from
any layer. ``actor.token_hash`` is the SHA256[:16] of the raw bearer token
(matches ``bricks/mcp/middleware_audit.py``); raw tokens are NEVER stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    SEARCH = "search"
    FETCH = "fetch"
    MCP_TOOL_CALL = "mcp_tool_call"
    ZONE_ACCESS = "zone_access"
    POLICY_BLOCK = "policy_block"
    APPROVAL = "approval"


class Result(StrEnum):
    OK = "ok"
    BLOCKED = "blocked"
    PENDING_APPROVAL = "pending_approval"


@dataclass(frozen=True, slots=True)
class Actor:
    token_hash: str | None = None
    agent: str | None = None
    user: str | None = None


@dataclass(frozen=True, slots=True)
class Subject:
    zone: str | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    id: str
    ts: str
    kind: EventKind
    result: Result
    latency_ms: int | None = None
    trace_id: str | None = None
    actor: Actor = field(default_factory=Actor)
    subject: Subject = field(default_factory=Subject)
    meta: dict[str, Any] | None = None
