"""ActivityEvent schema for issue #3791 foundation slice."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class EventKind(str, enum.Enum):
    SEARCH = "search"
    FETCH = "fetch"
    MCP_TOOL_CALL = "mcp_tool_call"
    ZONE_ACCESS = "zone_access"
    POLICY_BLOCK = "policy_block"
    APPROVAL = "approval"


class Result(str, enum.Enum):
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
