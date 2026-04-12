"""TokenResolver seam between ``TokenManager`` and higher-level credential backends.

Pre-refactor for the unified auth-profile store (Issue #3722 / #3737). Pure
seam extraction: defines the minimal contract for "given a provider + user,
return a valid access token, refreshing if needed" without touching the RFC
9700 rotation / reuse detection machinery that lives inside ``TokenManager``.

Phase 1 of the epic (#3738) introduces a ``CredentialBackend`` protocol keyed
by an opaque ``backend_key: str``; ``NexusTokenManagerBackend`` will translate
that opaque key into the ``(provider, user_email, zone_id)`` tuple this
protocol expects and delegate here. Keeping the compound key at the
``TokenResolver`` layer avoids pushing key-format parsing into ``TokenManager``.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from nexus.contracts.constants import ROOT_ZONE_ID


@dataclass(frozen=True, slots=True)
class ResolvedToken:
    """A freshly resolved OAuth access token plus minimal metadata.

    This is intentionally narrower than ``OAuthCredential``: callers of the
    ``TokenResolver`` seam never need the refresh token, client_id, token_uri,
    encrypted forms, or audit metadata. The resolver owns those internals.
    """

    access_token: str
    expires_at: datetime | None
    scopes: tuple[str, ...]


@runtime_checkable
class TokenResolver(Protocol):
    """Contract for the refresh-aware read path.

    Implementations must guarantee that a successful ``resolve()`` returns a
    token that is valid right now — refreshing, rotating, and updating the
    underlying store as needed. Any rate limiting, reuse detection, or audit
    logging is the implementation's responsibility and MUST NOT leak through
    this interface.
    """

    async def resolve(
        self,
        provider: str,
        user_email: str,
        *,
        zone_id: str = ROOT_ZONE_ID,
    ) -> ResolvedToken: ...
