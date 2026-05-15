"""Search delegation credential for cross-zone federated search (Issue #3147, Phase 2).

SearchDelegation is a read-only, short-lived, search-scoped credential
that allows one zone's search daemon to query another zone's search
daemon over gRPC without full VFS access.

Security model:
- Hard method allowlist: ONLY "search" and "semantic_search" permitted.
- Target zone restriction: delegation is scoped to specific zone(s).
- Short TTL (default 30s): limits blast radius of credential leak.
- Validated in servicer BEFORE dispatch — non-search methods never
  reach the dispatch table with this credential type.
"""

import time
from dataclasses import dataclass, field

# Hard allowlist — ONLY these dispatch methods are permitted with SearchDelegation.
# Enforced in servicer.py Call handler BEFORE dispatch is reached.
SEARCH_DELEGATION_METHODS: frozenset[str] = frozenset({"search", "semantic_search"})

DEFAULT_TTL_SECONDS = 30


@dataclass(frozen=True)
class SearchDelegation:
    """Read-only, search-scoped delegation credential.

    Attributes:
        delegation_id: Unique identifier for this delegation.
        source_zone_id: Zone that issued the delegation.
        target_zones: Zones this delegation grants search access to.
        subject: (subject_type, subject_id) of the original requester.
        created_at: Monotonic timestamp when delegation was created.
        ttl_seconds: Time-to-live in seconds (default 30).
    """

    delegation_id: str
    source_zone_id: str
    target_zones: frozenset[str]
    subject: tuple[str, str]
    created_at: float = field(default_factory=time.monotonic)
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    @property
    def expires_at(self) -> float:
        """Monotonic expiry timestamp."""
        return self.created_at + self.ttl_seconds

    def is_expired(self) -> bool:
        """Check if this delegation has exceeded its TTL."""
        return time.monotonic() > self.expires_at

    def is_zone_permitted(self, zone_id: str) -> bool:
        """Check if a zone is within the delegation's scope."""
        return zone_id in self.target_zones

    def is_method_permitted(self, method: str) -> bool:
        """Check if a dispatch method is in the search allowlist."""
        return method in SEARCH_DELEGATION_METHODS

    def validate(self, method: str, target_zone: str) -> None:
        """Validate delegation for a specific method + zone combination.

        Raises:
            PermissionError: If method, zone, or TTL validation fails.
        """
        if not self.is_method_permitted(method):
            raise PermissionError(
                f"SearchDelegation permits only {SEARCH_DELEGATION_METHODS}, got '{method}'"
            )
        if not self.is_zone_permitted(target_zone):
            raise PermissionError(
                f"Zone '{target_zone}' not in delegation scope {self.target_zones}"
            )
        if self.is_expired():
            raise PermissionError(
                f"SearchDelegation {self.delegation_id} expired (TTL={self.ttl_seconds}s)"
            )
