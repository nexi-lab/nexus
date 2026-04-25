"""ReadAuditWriter — auth_profile_reads row per credential resolution (#3818).

Sampling: 100% on cache-miss (real KMS unwrap → real credential access),
1% on cache-hit (operational telemetry, not access). Sampling rule documented
in deployment guide; deviation should be a deliberate operator choice.
"""

from __future__ import annotations

import logging
import random
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.consumer_metrics import READ_AUDIT_WRITES

logger = logging.getLogger(__name__)

_PURPOSE_MAX_LEN = 256


class ReadAuditWriter:
    """Inserts ``auth_profile_reads`` rows. Caller passes RLS-set engine.

    Cache-miss writes are fail-closed: a failure raises ``AuditWriteFailed``
    so the caller never returns a credential that has no durable audit
    trail. Cache-hit writes (1% sampled) are best-effort — we log + swallow
    so operational telemetry blips don't block a credential resolution.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        hit_sample_rate: float = 0.01,
        rng: random.Random | None = None,
    ) -> None:
        self._engine = engine
        self._hit_sample_rate = hit_sample_rate
        self._rng = rng or random.Random()

    def write(
        self,
        *,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        auth_profile_id: str,
        caller_machine_id: uuid.UUID,
        caller_kind: str,
        provider: str,
        purpose: str,
        cache_hit: bool,
        kek_version: int,
    ) -> None:
        """Atomically check-then-audit the credential issuance.

        The transaction:
          1. SELECT ... FOR SHARE on the caller's daemon_machines row,
             scoped to ``revoked_at IS NULL``. The SHARE lock blocks any
             concurrent revoke (which needs ROW EXCLUSIVE) until we commit.
          2. INSERT the audit row.
          3. COMMIT, releasing the SHARE lock.

        With this layout, a revoke transaction can only commit BEFORE
        step 1 (we see no row → raise MachineUnknownOrRevoked, no credential
        returned) or AFTER our COMMIT (the audit row exists for a permitted
        read — that's correct, the read was permitted at the moment it
        happened — and the next request from the now-revoked daemon will
        be denied at its own step 1). The race window codex flagged in
        F26 (revoke between assert#3 and JSONResponse send) collapses to
        zero because the revocation gate now lives inside the audit tx.

        Cache-miss writes are fail-closed: a failure raises
        ``AuditWriteFailed`` (audit table problem) or
        ``MachineUnknownOrRevoked`` (revoke detected). Cache-hit writes
        (sampled) still observe the revocation gate but swallow audit
        insert failures so operational telemetry blips don't block reads.
        """
        # Sampling decision — but the revocation gate STILL runs for
        # sampled-out cache hits. Without that, the 99% of hits that
        # don't get an audit row would also skip the SHARE-lock SELECT
        # and be vulnerable to the F26 race the gate exists to close.
        sampled_out = cache_hit and self._rng.random() >= self._hit_sample_rate

        truncated_purpose = purpose[:_PURPOSE_MAX_LEN]
        # Local imports — read_audit is imported at module load by the
        # consumer, so top-level imports would create a cycle.
        from nexus.bricks.auth.consumer import AuditWriteFailed, MachineUnknownOrRevoked

        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text("SET LOCAL app.current_tenant = :t"),
                    {"t": str(tenant_id)},
                )
                # Atomic revocation gate (F26). FOR SHARE so concurrent
                # reads for the same machine don't serialize against each
                # other; only revoke (which takes ROW EXCLUSIVE) blocks.
                row = conn.execute(
                    text(
                        "SELECT 1 FROM daemon_machines "
                        "WHERE tenant_id = :t AND principal_id = :p AND id = :m "
                        "  AND revoked_at IS NULL "
                        "FOR SHARE"
                    ),
                    {
                        "t": str(tenant_id),
                        "p": str(principal_id),
                        "m": str(caller_machine_id),
                    },
                ).fetchone()
                if row is None:
                    raise MachineUnknownOrRevoked.from_row(
                        tenant_id=tenant_id,
                        principal_id=principal_id,
                        provider=provider,
                        cause="machine_revoked",
                    )
                if sampled_out:
                    # Gate passed; skip the audit INSERT since this hit
                    # was sampled out. The SHARE lock is released on
                    # COMMIT below — same atomicity guarantee, no audit row.
                    return
                conn.execute(
                    text(
                        "INSERT INTO auth_profile_reads "
                        "(tenant_id, principal_id, auth_profile_id, caller_machine_id, "
                        " caller_kind, provider, purpose, cache_hit, kek_version) "
                        "VALUES (:t, :p, :ap, :cm, :ck, :prov, :pur, :hit, :kv)"
                    ),
                    {
                        "t": str(tenant_id),
                        "p": str(principal_id),
                        "ap": auth_profile_id,
                        "cm": str(caller_machine_id),
                        "ck": caller_kind,
                        "prov": provider,
                        "pur": truncated_purpose,
                        "hit": cache_hit,
                        "kv": kek_version,
                    },
                )
                READ_AUDIT_WRITES.labels(cache="hit" if cache_hit else "miss").inc()
        except MachineUnknownOrRevoked:
            # Don't wrap — the revocation gate is its own well-defined
            # failure mode, distinct from an audit-table failure.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "auth_profile_reads insert failed tenant=%s principal=%s provider=%s",
                tenant_id,
                principal_id,
                provider,
            )
            if not cache_hit:
                raise AuditWriteFailed.from_row(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    provider=provider,
                    cause=f"{type(exc).__name__}",
                ) from exc
