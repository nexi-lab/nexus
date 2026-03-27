"""Verifiable Credential types for agent capability attestation (Issue #1753).

Pure value objects for the JWT-VC capability model.  Zero runtime dependencies
beyond stdlib — safe to import from any tier (kernel, services, bricks).

Design:
    - UCAN-inspired URI + abilities + caveats capability model
    - JWT-VC format (W3C VC-JOSE-COSE profile, EdDSA signing)
    - Frozen dataclasses for immutability and hashability
    - Constants for limits and defaults

References:
    - W3C VC Data Model 2.0: https://www.w3.org/TR/vc-data-model-2.0/
    - W3C VC-JOSE-COSE: https://www.w3.org/TR/vc-jose-cose/
    - UCAN Specification: https://ucan.xyz/specification/
"""

import types
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum capabilities per credential (keeps JWT <2KB).
MAX_BACKEND_FEATURES_PER_CREDENTIAL: int = 20

#: Maximum delegation depth (root=0, first delegate=1, …).
MAX_DELEGATION_DEPTH: int = 3

#: Default credential TTL in seconds (1 hour).
DEFAULT_CREDENTIAL_TTL: int = 3600

#: Minimum credential TTL in seconds (1 minute).
MIN_CREDENTIAL_TTL: int = 60

#: Maximum credential TTL in seconds (24 hours).
MAX_CREDENTIAL_TTL: int = 86400

#: W3C VC context URIs included in every JWT-VC.
VC_CONTEXT: tuple[str, ...] = (
    "https://www.w3.org/ns/credentials/v2",
    "https://nexus.example/ns/capabilities/v1",
)

#: W3C VC types included in every capability credential.
VC_TYPES: tuple[str, ...] = (
    "VerifiableCredential",
    "AgentCapabilityCredential",
)

# ---------------------------------------------------------------------------
# Ability enum
# ---------------------------------------------------------------------------


class Ability(StrEnum):
    """Standard abilities for agent capabilities (UCAN-inspired).

    Abilities follow a hierarchy where ``ADMIN`` (``*``) is a superset of
    all other abilities.  This enables clean attenuation: a delegator
    with ``ADMIN`` can grant any subset to a delegate.

    Attributes:
        READ: Read access to the resource.
        WRITE: Write/mutate access to the resource.
        EXECUTE: Execute/invoke access (e.g. run a brick action).
        DELEGATE: Permission to delegate this capability to others.
        ADMIN: Wildcard — superset of all abilities.
    """

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DELEGATE = "delegate"
    ADMIN = "*"


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Capability:
    """A single capability: what an agent can do to a resource.

    Resources use URI-style identifiers:
        - ``nexus:brick:search`` — the search brick
        - ``nexus:brick:cache`` — the cache brick
        - ``nexus:zone:acme`` — the "acme" zone
        - ``*`` — wildcard (all resources)

    Abilities define what actions are permitted on that resource.
    Caveats provide additional constraints (e.g. ``max_results``, ``zone_id``).

    Attributes:
        resource: URI identifying the target resource.
        abilities: Tuple of permitted abilities.
        caveats: Immutable mapping of constraint key-value pairs.
    """

    resource: str
    abilities: tuple[Ability, ...]
    caveats: types.MappingProxyType[str, Any] = field(
        default_factory=lambda: types.MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if not self.resource:
            raise ValueError("Capability resource must be non-empty")
        if not self.abilities:
            raise ValueError("Capability must have at least one ability")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JWT claims."""
        result: dict[str, Any] = {
            "resource": self.resource,
            "abilities": [a.value for a in self.abilities],
        }
        if self.caveats:
            result["caveats"] = dict(self.caveats)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Capability":
        """Deserialize from a JWT claims dict.

        Args:
            data: Dict with ``resource``, ``abilities``, and optional ``caveats``.

        Returns:
            Capability instance.

        Raises:
            KeyError: If required fields are missing.
            ValueError: If ability values are invalid.
        """
        caveats = data.get("caveats", {})
        return cls(
            resource=data["resource"],
            abilities=tuple(Ability(a) for a in data["abilities"]),
            caveats=types.MappingProxyType(caveats) if caveats else types.MappingProxyType({}),
        )

    def is_subset_of(self, parent: "Capability") -> bool:
        """Check if this capability is a valid attenuation of *parent*.

        A capability is a subset if:
        1. The resource matches (or parent has wildcard ``*``).
        2. All abilities are present in parent (or parent has ``ADMIN``).

        Caveats are NOT checked here — they are application-specific.

        Args:
            parent: The parent capability to check against.

        Returns:
            True if this capability is a valid subset of *parent*.
        """
        # Resource match
        if parent.resource != "*" and parent.resource != self.resource:
            return False

        # Ability match
        if Ability.ADMIN in parent.abilities:
            return True
        return all(a in parent.abilities for a in self.abilities)


# ---------------------------------------------------------------------------
# Credential Claims
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CredentialClaims:
    """Parsed claims from a verified JWT-VC capability credential.

    This is the domain-level representation of a credential after
    verification.  The raw JWT string is NOT stored here — it lives
    in the ``jwt_token`` field of ``CredentialStatus``.

    Attributes:
        issuer_did: DID of the credential issuer.
        subject_did: DID of the credential subject (the agent).
        credential_id: Unique credential identifier (URN UUID).
        capabilities: Tuple of granted capabilities.
        issued_at: Unix timestamp when the credential was issued.
        expires_at: Unix timestamp when the credential expires.
        parent_credential_id: ID of the parent credential (for delegations).
        delegation_depth: Depth in the delegation chain (0 = root).
    """

    issuer_did: str
    subject_did: str
    credential_id: str
    capabilities: "tuple[Capability, ...]"
    issued_at: float
    expires_at: float
    parent_credential_id: str | None = None
    delegation_depth: int = 0


# ---------------------------------------------------------------------------
# Credential Status (DB-level view)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """Status snapshot of an issued credential.

    Returned by the CredentialService for listing and status queries.

    Attributes:
        credential_id: Unique credential identifier.
        issuer_did: DID of the issuer.
        subject_did: DID of the subject agent.
        subject_agent_id: Agent ID for the subject.
        is_active: Whether the credential is currently valid.
        created_at: When the credential was issued.
        expires_at: When the credential expires.
        revoked_at: When the credential was revoked (None if active).
        delegation_depth: Depth in the delegation chain.
        parent_credential_id: Parent credential ID (None for root).
    """

    credential_id: str
    issuer_did: str
    subject_did: str
    subject_agent_id: str
    is_active: bool
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    delegation_depth: int = 0
    parent_credential_id: str | None = None
