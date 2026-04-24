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
from typing import TYPE_CHECKING

from nexus.bricks.auth.consumer_metrics import (
    TOKEN_EXCHANGE_LATENCY,
    TOKEN_EXCHANGE_REQUESTS,
)

if TYPE_CHECKING:
    from nexus.bricks.auth.consumer_cache import ResolvedCredCache
    from nexus.bricks.auth.consumer_providers.base import ProviderAdapter
    from nexus.bricks.auth.envelope import DEKCache, EncryptionProvider
    from nexus.bricks.auth.postgres_profile_store import PostgresAuthProfileStore
    from nexus.bricks.auth.read_audit import ReadAuditWriter
    from nexus.server.api.v1.jwt_signer import DaemonClaims


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
    """Orchestrates: cache lookup → decrypt → materialize → cache write → audit.

    All errors raised by this class are subclasses of ``ConsumerError`` and
    carry no plaintext / token bytes in repr.
    """

    def __init__(
        self,
        *,
        store: "PostgresAuthProfileStore",
        encryption: "EncryptionProvider",
        dek_cache: "DEKCache",
        cred_cache: "ResolvedCredCache",
        adapters: dict[str, "ProviderAdapter"],
        audit: "ReadAuditWriter",
    ) -> None:
        self._store = store
        self._encryption = encryption
        self._dek_cache = dek_cache
        self._cred_cache = cred_cache
        self._adapters = adapters
        self._audit = audit

    def resolve(
        self,
        *,
        claims: "DaemonClaims",
        provider: str,
        purpose: str,
        force_refresh: bool = False,
    ) -> MaterializedCredential:
        import time
        from datetime import UTC, datetime, timedelta

        from nexus.bricks.auth.postgres_profile_store import ProfileNotFound

        start = time.monotonic()
        cache_label = "miss"
        result_label = "ok"
        try:
            adapter = self._adapters.get(provider)
            if adapter is None:
                raise ProviderNotConfigured.from_row(
                    tenant_id=claims.tenant_id,
                    principal_id=claims.principal_id,
                    provider=provider,
                    cause="adapter_not_registered",
                )

            cache_key = (
                str(claims.tenant_id),
                str(claims.principal_id),
                provider,
            )
            now = datetime.now(UTC)

            if not force_refresh:
                cached = self._cred_cache.get(cache_key, now=now)
                if cached is not None:
                    # Cache hit — log sampled audit row, no decrypt
                    cache_label = "hit"
                    self._audit.write(
                        tenant_id=claims.tenant_id,
                        principal_id=claims.principal_id,
                        auth_profile_id="cached",
                        caller_machine_id=claims.machine_id,
                        caller_kind="daemon",
                        provider=provider,
                        purpose=purpose,
                        cache_hit=True,
                        kek_version=0,  # unknown on cache hit; sentinel value
                    )
                    return cached

            try:
                decrypted = self._store.decrypt_profile(
                    principal_id=claims.principal_id,
                    provider=provider,
                    encryption=self._encryption,
                    dek_cache=self._dek_cache,
                )
            except ProfileNotFound as exc:
                raise ProfileNotFoundForCaller.from_row(
                    tenant_id=claims.tenant_id,
                    principal_id=claims.principal_id,
                    provider=provider,
                    cause="no_envelope_row",
                ) from exc

            # Stale-source check: last_synced_at + sync_ttl must be in the future.
            ttl_window = timedelta(seconds=decrypted.sync_ttl_seconds)
            if decrypted.last_synced_at + ttl_window < now:
                raise StaleSource.from_row(
                    tenant_id=claims.tenant_id,
                    principal_id=claims.principal_id,
                    provider=provider,
                    cause=f"last_synced_at_age={int((now - decrypted.last_synced_at).total_seconds())}s",
                )

            try:
                materialized = adapter.materialize(decrypted.plaintext)
            except (ValueError, KeyError) as exc:
                raise AdapterMaterializeFailed.from_row(
                    tenant_id=claims.tenant_id,
                    principal_id=claims.principal_id,
                    provider=provider,
                    cause=f"{type(exc).__name__}",
                ) from exc

            self._cred_cache.put(cache_key, materialized, now=now)
            self._audit.write(
                tenant_id=claims.tenant_id,
                principal_id=claims.principal_id,
                auth_profile_id=decrypted.profile_id,
                caller_machine_id=claims.machine_id,
                caller_kind="daemon",
                provider=provider,
                purpose=purpose,
                cache_hit=False,
                kek_version=decrypted.kek_version,
            )
            return materialized

        except (ProfileNotFoundForCaller, ProviderNotConfigured):
            result_label = "denied"
            raise
        except StaleSource:
            result_label = "stale"
            raise
        except AdapterMaterializeFailed:
            result_label = "envelope_error"
            raise
        finally:
            TOKEN_EXCHANGE_LATENCY.labels(provider=provider, cache=cache_label).observe(
                time.monotonic() - start
            )
            TOKEN_EXCHANGE_REQUESTS.labels(provider=provider, result=result_label).inc()
