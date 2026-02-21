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
