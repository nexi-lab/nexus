"""CredentialConsumer: server-side read path for envelope-encrypted auth profiles (#3818).

The consumer is the orchestrator that ties together:
  - PostgresAuthProfileStore.decrypt_profile() — envelope → plaintext
  - ProviderAdapter.materialize() — plaintext → MaterializedCredential
  - ResolvedCredCache — TTL = min(300, expires_at - 60)
  - ReadAuditWriter — auth_profile_reads row per resolve

Callers: ``/v1/auth/token-exchange`` router (wire path), and any in-process
server-side agent that needs to act as a user.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class MaterializedCredential:
    """Provider-native credential ready for the wire / in-process use.

    ``access_token`` is the time-bounded part (AWS session_token, GitHub PAT).
    For multi-part credentials (AWS), ``metadata`` carries the static parts
    (access_key_id, secret_access_key, region, account_id) — the wire response
    surfaces these under ``nexus_credential_metadata``.

    ``__repr__`` masks ``access_token`` to keep it out of logs / tracebacks.
    """

    provider: str
    access_token: str
    expires_at: datetime | None
    metadata: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"MaterializedCredential(provider={self.provider!r}, "
            f"access_token='***', expires_at={self.expires_at!r}, "
            f"metadata_keys={sorted(self.metadata)!r})"
        )


# ---------------------------------------------------------------------------
# Error taxonomy — no plaintext / token bytes ever in repr
# ---------------------------------------------------------------------------


class ConsumerError(Exception):
    """Root of every CredentialConsumer error."""

    def __init__(
        self,
        message: str,
        *,
        tenant_id: uuid.UUID | None = None,
        principal_id: uuid.UUID | None = None,
        provider: str | None = None,
        cause: str | None = None,
    ) -> None:
        super().__init__(message)
        self.tenant_id = tenant_id
        self.principal_id = principal_id
        self.provider = provider
        self.cause = cause

    @classmethod
    def from_row(
        cls,
        *,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        provider: str,
        cause: str,
    ) -> "ConsumerError":
        return cls(
            f"{cls.__name__} tenant={tenant_id} principal={principal_id} "
            f"provider={provider} cause={cause}",
            tenant_id=tenant_id,
            principal_id=principal_id,
            provider=provider,
            cause=cause,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(tenant_id={self.tenant_id!s}, "
            f"principal_id={self.principal_id!s}, provider={self.provider!r}, "
            f"cause={self.cause!r})"
        )


class ProfileNotFoundForCaller(ConsumerError):
    """Tenant/principal/provider triple has no envelope row."""


class ProviderNotConfigured(ConsumerError):
    """No ProviderAdapter registered for this provider name."""


class StaleSource(ConsumerError):
    """Envelope row's ``last_synced_at`` is past ``sync_ttl_seconds`` —
    daemon is offline; caller should retry once daemon catches up.
    """


class AdapterMaterializeFailed(ConsumerError):
    """Provider adapter raised while decoding the envelope payload."""


# ---------------------------------------------------------------------------
# CredentialConsumer (orchestrator) — implementation in Task 8
# ---------------------------------------------------------------------------


class CredentialConsumer:
    """Implementation lands in Task 8. Type-only declaration here so other
    modules can import the symbol without circular references.
    """
