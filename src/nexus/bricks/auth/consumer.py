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

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from nexus.bricks.auth.consumer_metrics import (
    CROSS_MACHINE_READS,
    TOKEN_EXCHANGE_LATENCY,
    TOKEN_EXCHANGE_REQUESTS,
)

_logger = logging.getLogger(__name__)

# Mirror ResolvedCredCache's _REFRESH_HEADROOM_SECONDS — a credential whose
# expires_at is inside this window from now is treated as already-expired
# for the purpose of returning to the caller. Avoids handing back tokens
# that the upstream provider will start rejecting before the client can
# meaningfully use them.
_MATERIALIZED_REFRESH_HEADROOM_SECONDS = 60

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

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


class MachineUnknownOrRevoked(ConsumerError):
    """Daemon machine row absent or has revoked_at set.

    Distinct from a JWT verification failure — the JWT is cryptographically
    valid but the daemon has been revoked (or its row deleted) since the
    token was issued. Router maps this to 401 invalid_token so a compromised
    daemon stops minting upstream creds the moment its row is revoked.
    """


class MultipleProfilesForProvider(ConsumerError):
    """More than one envelope row exists for (tenant, principal, provider).

    The wire contract today maps ``resource=urn:nexus:provider:<name>`` to a
    single provider name with no profile_id discriminator, so we cannot
    deterministically pick one row. Fail closed instead of silently picking
    the most-recently-updated row, which could hand back the wrong account's
    credentials. Operators must collapse to one active envelope per provider
    or upgrade the wire contract.
    """


class AuditWriteFailed(ConsumerError):
    """The mandatory cache-miss audit row could not be written.

    Resolving a credential without a durable record of the read would
    create a forensics blind spot exactly when something went wrong in
    the database. Fail closed: the caller gets a 503, no credential
    leaves the server, the operator sees both the audit-table failure
    AND the missing read in metrics.
    """


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
        encryption: "EncryptionProvider",
        dek_cache: "DEKCache",
        cred_cache: "ResolvedCredCache",
        adapters: dict[str, "ProviderAdapter"],
        audit: "ReadAuditWriter",
        store: "PostgresAuthProfileStore | None" = None,
        engine: "Engine | None" = None,
    ) -> None:
        # Either ``store`` (single-tenant, used by tests that pre-bind a tenant)
        # OR ``engine`` (multi-tenant, used by the server which scopes per-request
        # via JWT claims). When both are provided, ``store`` wins — tests get
        # deterministic behaviour without having to drop the engine.
        if store is None and engine is None:
            raise ValueError("CredentialConsumer requires either store=... or engine=...")
        self._store = store
        self._engine = engine
        self._encryption = encryption
        self._dek_cache = dek_cache
        self._cred_cache = cred_cache
        self._adapters = adapters
        self._audit = audit

    def _get_store(self, claims: "DaemonClaims") -> "PostgresAuthProfileStore":
        """Return a store scoped to ``claims.tenant_id``.

        - If a pre-bound store was supplied at construction (test path), return
          it as-is. Tests bind the store to a known tenant; we don't second-guess.
        - Otherwise, construct a fresh store using ``claims.tenant_id`` so the
          underlying ``decrypt_profile`` SQL filters by the JWT-verified tenant
          rather than a boot-time placeholder.
        """
        if self._store is not None:
            return self._store
        # Local import to avoid a circular dep at module load time.
        from nexus.bricks.auth.postgres_profile_store import PostgresAuthProfileStore

        return PostgresAuthProfileStore(
            "",
            tenant_id=claims.tenant_id,
            principal_id=claims.principal_id,
            engine=self._engine,
        )

    def _enforce_cross_machine(
        self,
        *,
        writer_machine_id: "uuid.UUID | None",
        claims: "DaemonClaims",
        provider: str,
    ) -> None:
        """Reject cross-machine reads unless the operator opted in.

        Raises ``MachineUnknownOrRevoked(cross_machine_read_disallowed)``
        when reader != writer and ``NEXUS_AUTH_ALLOW_CROSS_MACHINE_READ``
        is not enabled. Always logs + metrics on cross-machine reads
        (allowed or denied) so implicit sharing is visible.
        """
        if writer_machine_id is None or writer_machine_id == claims.machine_id:
            return
        _logger.warning(
            "cross_machine_read tenant=%s principal=%s provider=%s reader=%s writer=%s",
            claims.tenant_id,
            claims.principal_id,
            provider,
            claims.machine_id,
            writer_machine_id,
        )
        CROSS_MACHINE_READS.labels(provider=provider).inc()
        if os.environ.get("NEXUS_AUTH_ALLOW_CROSS_MACHINE_READ", "").lower() not in (
            "1",
            "true",
            "yes",
        ):
            raise MachineUnknownOrRevoked.from_row(
                tenant_id=claims.tenant_id,
                principal_id=claims.principal_id,
                provider=provider,
                cause="cross_machine_read_disallowed",
            )

    def resolve(
        self,
        *,
        claims: "DaemonClaims",
        provider: str,
        purpose: str,
        profile_id: str | None = None,
        force_refresh: bool = False,
    ) -> MaterializedCredential:
        """Resolve a provider credential for ``claims``.

        ``profile_id`` selects a specific envelope row when the principal has
        more than one profile for ``provider`` (e.g. two GitHub accounts).
        Without it, the call falls back to the legacy "single active row"
        contract and raises ``MultipleProfilesForProvider`` if more than one
        active row exists for the triple.
        """
        import time
        from datetime import UTC, datetime, timedelta

        from nexus.bricks.auth.postgres_profile_store import ProfileFingerprint, ProfileNotFound

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

            # machine_id is part of the key so a warm cache cannot return
            # another daemon's credential under cross-machine read enforcement.
            # Without this, machine A pushes + caches; machine B's request
            # passes assert_machine_active + assert_profile_active and gets
            # A's plaintext from cache without ever reaching the
            # writer_machine_id check on the decrypt path.
            #
            # profile_id is in the key so a multi-account principal's two
            # profiles do not share a cache slot — also keeps the implicit
            # "default" form (profile_id=None) separate from any explicit
            # selection of the same row.
            cache_key = (
                str(claims.tenant_id),
                str(claims.principal_id),
                provider,
                str(claims.machine_id),
                profile_id or "",
            )
            now = datetime.now(UTC)

            # Revocation gate (BEFORE cache lookup): a JWT minted before the
            # daemon row was revoked must not keep handing out cached
            # credentials until natural expiry. Per-request indexed lookup —
            # one row, one column, one ms.
            self._get_store(claims).assert_machine_active(
                principal_id=claims.principal_id,
                machine_id=claims.machine_id,
            )

            # All policy gates run BEFORE decrypt. Without this order, a
            # disallowed cross-machine read or a stale row would still
            # cause the server to KMS-unwrap the DEK and AES-GCM decrypt
            # the payload — pulling another daemon's plaintext into
            # process memory just to throw it away. Move stale + cross-
            # machine to the front so the rejection happens against
            # ciphertext + indexed metadata only.
            try:
                fp_pre = self._get_store(claims).assert_profile_active(
                    principal_id=claims.principal_id,
                    provider=provider,
                    profile_id=profile_id,
                )
            except ProfileNotFound as exc:
                # Cache may have an entry pointing at the now-missing row.
                self._cred_cache.evict(cache_key)
                raise ProfileNotFoundForCaller.from_row(
                    tenant_id=claims.tenant_id,
                    principal_id=claims.principal_id,
                    provider=provider,
                    cause="no_active_profile",
                ) from exc
            except (MultipleProfilesForProvider, StaleSource):
                # Either condition invalidates whatever was cached; evict
                # before bubbling so the next request goes through the
                # full miss path against any new state.
                self._cred_cache.evict(cache_key)
                raise

            self._enforce_cross_machine(
                writer_machine_id=fp_pre.writer_machine_id,
                claims=claims,
                provider=provider,
            )

            if not force_refresh:
                entry = self._cred_cache.get(cache_key, now=now)
                if entry is not None:
                    cached_fp = entry.fingerprint
                    # Detect a row rewrite since the cache was primed: a
                    # different profile_id (deleted + replaced), a different
                    # writer machine (another daemon overwrote the same
                    # provider/account), or a bumped last_synced_at (daemon
                    # pushed a fresh envelope, possibly rotating the upstream
                    # credential). Any difference → cached plaintext is stale.
                    cached_cred = entry.cred
                    cached_expired = cached_cred.expires_at is not None and (
                        cached_cred.expires_at
                        <= now + timedelta(seconds=_MATERIALIZED_REFRESH_HEADROOM_SECONDS)
                    )
                    if (
                        fp_pre.profile_id != cached_fp.profile_id
                        or fp_pre.writer_machine_id != cached_fp.writer_machine_id
                        or fp_pre.last_synced_at != cached_fp.last_synced_at
                        or cached_expired
                    ):
                        # Row rewrite OR the cached materialized credential's
                        # own expiry has caught up to the refresh-headroom.
                        # Evict and fall through to the decrypt path so we
                        # serve a fresh credential (or surface StaleSource
                        # if the underlying envelope is also stale).
                        self._cred_cache.evict(cache_key)
                    else:
                        # F25 + F26: revocation enforcement now lives
                        # inside audit.write (SELECT FOR SHARE on
                        # daemon_machines + audit INSERT in one tx).
                        # Concurrent revoke either commits BEFORE the
                        # audit's SHARE-lock SELECT (we get
                        # MachineUnknownOrRevoked + nothing returned) or
                        # AFTER our audit COMMIT (we returned this one
                        # credential, next request denied). No race window.
                        cache_label = "hit"
                        self._audit.write(
                            tenant_id=claims.tenant_id,
                            principal_id=claims.principal_id,
                            auth_profile_id=cached_fp.profile_id,
                            caller_machine_id=claims.machine_id,
                            caller_kind="daemon",
                            provider=provider,
                            purpose=purpose,
                            cache_hit=True,
                            kek_version=cached_fp.kek_version,
                        )
                        return cached_cred

            try:
                # F27: scope decrypt by the precheck's matched profile_id,
                # not the user-provided one. Without this, decrypt_profile
                # re-queries with provider-only filtering and sees the
                # active row + any stale siblings (which assert_profile_active
                # already filtered out) — raising MultipleProfilesForProvider
                # when the request is actually unambiguous. Using fp_pre's
                # profile_id pins the decrypt query to the exact row we
                # validated, even if the caller passed profile_id=None.
                decrypted = self._get_store(claims).decrypt_profile(
                    principal_id=claims.principal_id,
                    provider=provider,
                    profile_id=fp_pre.profile_id,
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

            try:
                materialized = adapter.materialize(decrypted.plaintext)
            except (ValueError, KeyError) as exc:
                raise AdapterMaterializeFailed.from_row(
                    tenant_id=claims.tenant_id,
                    principal_id=claims.principal_id,
                    provider=provider,
                    cause=f"{type(exc).__name__}",
                ) from exc

            # Defense-in-depth: re-apply cross-machine policy against the
            # decrypted row's writer_machine_id. Catches the (rare) race
            # where the row was rewritten between assert_profile_active
            # and decrypt_profile — the pre-check passed against the OLD
            # writer but the row we actually decrypted has a different one.
            self._enforce_cross_machine(
                writer_machine_id=decrypted.writer_machine_id,
                claims=claims,
                provider=provider,
            )

            # The materialized credential's own expires_at is independent of
            # last_synced_at: AWS STS could mint a 15-min token that's
            # already past mid-life by the time the daemon pushed it, and
            # fine-grained PATs may carry an explicit expiry that's already
            # in the refresh-headroom window. Returning ``expires_in: 0`` to
            # the caller would cause downstream 401 loops instead of a
            # deterministic refresh signal — fail closed with StaleSource so
            # the daemon re-pushes a fresh credential before we serve it.
            if materialized.expires_at is not None:
                refresh_deadline = now + timedelta(seconds=_MATERIALIZED_REFRESH_HEADROOM_SECONDS)
                if materialized.expires_at <= refresh_deadline:
                    raise StaleSource.from_row(
                        tenant_id=claims.tenant_id,
                        principal_id=claims.principal_id,
                        provider=provider,
                        cause="materialized_credential_expired_or_near_expiry",
                    )

            # F25 + F26: revocation enforcement is atomic with the audit
            # write — audit.write does SELECT FOR SHARE on daemon_machines
            # before INSERTing the audit row, in one transaction. A
            # concurrent revoke either commits BEFORE our SHARE-lock
            # SELECT (we get MachineUnknownOrRevoked, no credential
            # cached or returned) or AFTER our audit COMMIT (this one
            # credential is returned + cached; next request denied). No
            # race window between the gate and the cache.put / return.
            #
            # IMPORTANT: write audit BEFORE caching so a failed mandatory
            # cache-miss audit doesn't poison the cache. Without this order,
            # an audit failure raises AuditWriteFailed → router 503, but the
            # credential is already in cred_cache and the next request hits
            # cache (no audit attempted) → silent forensics gap.
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

            self._cred_cache.put(
                cache_key,
                materialized,
                now=now,
                fingerprint=ProfileFingerprint(
                    profile_id=decrypted.profile_id,
                    writer_machine_id=decrypted.writer_machine_id,
                    kek_version=decrypted.kek_version,
                    last_synced_at=decrypted.last_synced_at,
                ),
            )
            return materialized

        except (ProfileNotFoundForCaller, ProviderNotConfigured, MachineUnknownOrRevoked):
            result_label = "denied"
            raise
        except MultipleProfilesForProvider:
            result_label = "ambiguous"
            raise
        except StaleSource:
            result_label = "stale"
            raise
        except AdapterMaterializeFailed:
            result_label = "envelope_error"
            raise
        except AuditWriteFailed:
            result_label = "audit_write_failed"
            raise
        except Exception:
            # F30: catch-all so EnvelopeError (KMS unwrap failure, AAD
            # mismatch, ciphertext corruption — all bubble to the router
            # as 500 envelope_error) is counted as a failure in the
            # primary token-exchange metric, not as ``ok``. Without this,
            # a KMS outage would show 100% success in
            # nexus_token_exchange_requests_total even while every
            # client was getting 500s.
            result_label = "envelope_error"
            raise
        finally:
            TOKEN_EXCHANGE_LATENCY.labels(provider=provider, cache=cache_label).observe(
                time.monotonic() - start
            )
            TOKEN_EXCHANGE_REQUESTS.labels(provider=provider, result=result_label).inc()
