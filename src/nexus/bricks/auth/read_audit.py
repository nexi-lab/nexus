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

logger = logging.getLogger(__name__)

_PURPOSE_MAX_LEN = 256


class ReadAuditWriter:
    """Inserts ``auth_profile_reads`` rows. Caller passes RLS-set engine.

    Failures are logged and swallowed. We never block a credential resolution
    on audit-row insert — losing a single row is preferable to a blocked
    caller, and the cache-miss path will retry naturally on the next resolve.
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
        if cache_hit and self._rng.random() >= self._hit_sample_rate:
            return  # sampled out

        truncated_purpose = purpose[:_PURPOSE_MAX_LEN]

        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text("SET LOCAL app.current_tenant = :t"),
                    {"t": str(tenant_id)},
                )
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
        except Exception:  # noqa: BLE001
            logger.exception(
                "auth_profile_reads insert failed tenant=%s principal=%s provider=%s",
                tenant_id,
                principal_id,
                provider,
            )
