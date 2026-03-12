"""Tier-neutral access manifest types for MCP tool scoping (Issue #1754).

Frozen dataclasses with zero nexus imports — safe for any tier.
Manifests declare per-agent tool access with first-match-wins semantics.
"""

from dataclasses import dataclass
from enum import StrEnum


class ToolPermission(StrEnum):
    """Permission decision for a tool access request."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """A single rule in an access manifest.

    Entries are evaluated in order (first-match-wins).
    Patterns use fnmatch glob syntax (e.g., "nexus_*", "*").

    Attributes:
        tool_pattern: Glob pattern for tool names (case-insensitive).
        permission: ALLOW or DENY.
        max_calls_per_minute: Optional rate limit (None = unlimited).
    """

    tool_pattern: str
    permission: ToolPermission
    max_calls_per_minute: int | None = None


@dataclass(frozen=True, slots=True)
class EvaluationTraceEntry:
    """A single step in the evaluation trace (proof tree).

    Records whether a manifest entry matched the tool name during
    first-match-wins evaluation.

    Attributes:
        index: Position of this entry in the manifest (0-based).
        tool_pattern: The glob pattern from the manifest entry.
        permission: The permission this entry would grant.
        matched: Whether this entry matched the tool name.
        max_calls_per_minute: Rate limit from the entry (None = unlimited).
    """

    index: int
    tool_pattern: str
    permission: ToolPermission
    matched: bool
    max_calls_per_minute: int | None = None


@dataclass(frozen=True, slots=True)
class EvaluationTrace:
    """Full evaluation trace (proof tree) for a tool permission check.

    Records the decision process: which entries were checked, which one
    matched (if any), and the final decision.

    Attributes:
        tool_name: The tool that was evaluated.
        decision: Final permission decision (ALLOW or DENY).
        matched_index: Index of the first matching entry (-1 if no match).
        entries: Ordered trace of each manifest entry checked.
        default_applied: True if no entry matched (default DENY applied).
    """

    tool_name: str
    decision: ToolPermission
    matched_index: int
    entries: tuple["EvaluationTraceEntry", ...]
    default_applied: bool


@dataclass(frozen=True, slots=True)
class AccessManifest:
    """Declarative tool access manifest for an agent.

    Attributes:
        id: Manifest identifier (UUID).
        agent_id: Agent this manifest applies to.
        zone_id: Zone scope.
        name: Human-readable name.
        entries: Ordered rules, first-match-wins.
        status: Current lifecycle status (reuses CredentialStatus).
        valid_from: ISO 8601 start of validity.
        valid_until: ISO 8601 end of validity (None = no expiry).
        created_by: DID or agent_id of the creator.
        credential_id: Optional VC backing this manifest.
    """

    id: str
    agent_id: str
    zone_id: str
    name: str
    entries: tuple[ManifestEntry, ...]
    status: str  # CredentialStatus value (avoid cross-import)
    valid_from: str
    valid_until: str | None
    created_by: str
    credential_id: str | None = None
