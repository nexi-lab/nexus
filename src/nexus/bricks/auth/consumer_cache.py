"""ResolvedCredCache: TTL = min(ceiling, expires_at - 60s).

Holds plaintext access_tokens in memory bounded by both a ceiling (default
300s, matching DEKCache) and the upstream credential's own ``expires_at``.
This caps plaintext lifetime regardless of which bound triggers first.

Keyed by ``(tenant_id_str, principal_id_str, provider, machine_id_str)``.
Tenant in the key is belt-and-braces against any future bug that forgets
to ``SET LOCAL app.current_tenant`` before calling the consumer. Machine
id in the key keeps each daemon's view isolated so a warm cache cannot
hand back another machine's plaintext, which would silently bypass the
cross-machine read check that only fires on the decrypt path.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from nexus.bricks.auth.consumer import MaterializedCredential
from nexus.bricks.auth.postgres_profile_store import ProfileFingerprint

_REFRESH_HEADROOM_SECONDS = 60


def _compute_ttl_seconds(*, now: datetime, expires_at: datetime | None) -> int:
    """TTL = min(ceiling, expires_at - 60s). Clamped to >= 0.

    The 60s headroom means we evict before the upstream cred actually expires,
    so callers never see a 401 from the upstream provider mid-call.

    Ceiling is applied by the caller (``ResolvedCredCache.put``) — this helper
    only computes the expires-at-bound. Returns the smaller of the two there.
    """
    if expires_at is None:
        return 10**9  # effectively unbounded; ceiling will dominate
    delta = (expires_at - now).total_seconds() - _REFRESH_HEADROOM_SECONDS
    return max(0, int(delta))


@dataclass(frozen=True)
class _Entry:
    cred: MaterializedCredential
    expires_at_monotonic: float
    # Identity of the source row at the time we cached. The cache-hit branch
    # in CredentialConsumer compares this against the current row's fingerprint
    # so a rewrite (different profile_id, different writer machine, or bumped
    # last_synced_at) evicts and re-runs the decrypt path — which re-applies
    # the cross-machine read policy against the NEW writer.
    fingerprint: ProfileFingerprint


class ResolvedCredCache:
    """Thread-safe TTL+LRU for MaterializedCredentials.

    Tests inject ``now`` for determinism; production calls pass
    ``datetime.now(UTC)``.
    """

    def __init__(self, *, ceiling_seconds: int = 300, max_entries: int = 1024) -> None:
        self._ceiling = ceiling_seconds
        self._max = max_entries
        self._store: OrderedDict[tuple[str, str, str, str], _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def get(
        self,
        key: tuple[str, str, str, str],
        *,
        now: datetime,
    ) -> _Entry | None:
        """Return the full entry (credential + cached fingerprint) or None.

        Returning the entry rather than just the credential lets the caller
        compare cached vs current ``ProfileFingerprint`` and evict on rewrite.
        """
        now_ts = now.timestamp()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if now_ts >= entry.expires_at_monotonic:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return entry

    def put(
        self,
        key: tuple[str, str, str, str],
        cred: MaterializedCredential,
        *,
        now: datetime,
        fingerprint: ProfileFingerprint,
    ) -> None:
        ttl = min(
            self._ceiling,
            _compute_ttl_seconds(now=now, expires_at=cred.expires_at),
        )
        with self._lock:
            self._store[key] = _Entry(
                cred=cred,
                expires_at_monotonic=now.timestamp() + ttl,
                fingerprint=fingerprint,
            )
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def evict(self, key: tuple[str, str, str, str]) -> None:
        """Drop an entry. Called by the consumer when a cache-hit state check
        finds the underlying profile is no longer usable (disabled, cooldown,
        or ambiguous), so the next request goes through the full miss path
        instead of returning the stale cached credential."""
        with self._lock:
            self._store.pop(key, None)
