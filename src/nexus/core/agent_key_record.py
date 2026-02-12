"""Agent key records for cryptographic identity (KYA Phase 1, Issue #1355).

Frozen dataclasses for immutable snapshots of agent key data.
Follows the same pattern as agent_record.py (Decision #7A).

AgentKeyRecord: Snapshot of a single Ed25519 key pair.
AgentIdentityInfo: Verification response with agent + key metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AgentKeyRecord:
    """Immutable snapshot of an agent's cryptographic key.

    Attributes:
        key_id: JWK Thumbprint (RFC 7638) — serves as the key identifier.
        agent_id: Agent that owns this key.
        zone_id: Zone/organization for multi-zone isolation.
        algorithm: Key algorithm (always "Ed25519" for Phase 1).
        public_key_jwk: Parsed JWK dict with kty, crv, x fields.
        has_private_key: Whether the encrypted private key is stored.
        created_at: When the key was generated.
        expires_at: Optional expiration time (None = no expiry).
        revoked_at: When the key was revoked (None = active).
    """

    key_id: str
    agent_id: str
    zone_id: str | None
    algorithm: str
    public_key_jwk: dict
    has_private_key: bool
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None

    @property
    def is_active(self) -> bool:
        """Key is active if not revoked and not expired."""
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None:
            from datetime import UTC

            return datetime.now(UTC) < self.expires_at
        return True


@dataclass(frozen=True)
class AgentIdentityInfo:
    """Identity verification response combining agent + key metadata.

    Returned by the POST /agents/{agent_id}/verify endpoint.

    Attributes:
        agent_id: The verified agent's identifier.
        owner_id: User ID who controls this agent.
        zone_id: Zone/organization scope.
        key_id: Active JWK Thumbprint for this agent.
        algorithm: Key algorithm ("Ed25519").
        public_key_jwk: Full public key in JWK format.
        created_at: When the key was generated.
    """

    agent_id: str
    owner_id: str
    zone_id: str | None
    key_id: str
    algorithm: str
    public_key_jwk: dict
    created_at: datetime
