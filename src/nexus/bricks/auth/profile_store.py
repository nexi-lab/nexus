"""SQLite-backed AuthProfileStore for the slim nexus-fs package.

Implements the AuthProfileStore protocol with:
  - stdlib sqlite3 (no SQLAlchemy, no aiosqlite)
  - WAL journal mode for concurrent readers + single writer
  - busy_timeout=5000 for brief writer contention during migration
  - synchronous=NORMAL (safe with WAL — checkpoints handle durability)
  - LRU-capped in-memory cache (default 64, configurable) fronting reads
  - Direct SQL updates for success/failure counters (multi-process safe)
  - 500-char truncation of raw_error on write (decision 7A)

Architecture: mirrors the _sqlite_meta.py pattern (single connection,
check_same_thread=False, row_factory=sqlite3.Row).
"""

from __future__ import annotations

import builtins
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus.bricks.auth.profile import (
    COOLDOWN_UNCHANGED,
    RAW_ERROR_MAX_LEN,
    AuthProfile,
    AuthProfileFailureReason,
    CooldownUpdate,
    ProfileUsageStats,
    _merge_cooldown_state,
    _merge_usage_stats_for_preserve,
    _normalize_utc_timestamp,
)

logger = logging.getLogger(__name__)

# Default flush interval for dirty success stats (seconds).
_DEFAULT_FLUSH_INTERVAL_S = 30.0

# Default LRU cache capacity (decision 14A).
_DEFAULT_CACHE_SIZE = 64

# ---------------------------------------------------------------------------
# SQL statements
# ---------------------------------------------------------------------------

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS auth_profiles (
    id                TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    account_identifier TEXT NOT NULL,
    backend           TEXT NOT NULL,
    backend_key       TEXT NOT NULL,
    last_synced_at    TEXT,
    sync_ttl_seconds  INTEGER NOT NULL DEFAULT 300,
    last_used_at      TEXT,
    success_count     INTEGER NOT NULL DEFAULT 0,
    failure_count     INTEGER NOT NULL DEFAULT 0,
    cooldown_until    TEXT,
    cooldown_reason   TEXT,
    disabled_until    TEXT,
    raw_error         TEXT
);
"""

_CREATE_INDEX_PROVIDER = """\
CREATE INDEX IF NOT EXISTS idx_auth_profiles_provider
    ON auth_profiles (provider);
"""

_UPSERT = """\
INSERT INTO auth_profiles (
    id, provider, account_identifier, backend, backend_key,
    last_synced_at, sync_ttl_seconds,
    last_used_at, success_count, failure_count,
    cooldown_until, cooldown_reason, disabled_until, raw_error
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    provider           = excluded.provider,
    account_identifier = excluded.account_identifier,
    backend            = excluded.backend,
    backend_key        = excluded.backend_key,
    last_synced_at     = excluded.last_synced_at,
    sync_ttl_seconds   = excluded.sync_ttl_seconds,
    last_used_at       = excluded.last_used_at,
    success_count      = excluded.success_count,
    failure_count      = excluded.failure_count,
    cooldown_until     = excluded.cooldown_until,
    cooldown_reason    = excluded.cooldown_reason,
    disabled_until     = excluded.disabled_until,
    raw_error          = excluded.raw_error;
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt_to_iso(dt: datetime | None) -> str | None:
    normalized = _normalize_utc_timestamp(dt)
    return normalized.isoformat() if normalized else None


def _iso_to_dt(val: Any) -> datetime | None:
    if val is None:
        return None
    return _normalize_utc_timestamp(datetime.fromisoformat(val))


def _reason_to_str(reason: AuthProfileFailureReason | None) -> str | None:
    return reason.value if reason else None


def _str_to_reason(val: Any) -> AuthProfileFailureReason | None:
    if val is None:
        return None
    try:
        return AuthProfileFailureReason(val)
    except ValueError:
        return AuthProfileFailureReason.UNKNOWN


def _row_to_profile(row: sqlite3.Row) -> AuthProfile:
    stats = ProfileUsageStats(
        last_used_at=_iso_to_dt(row["last_used_at"]),
        success_count=row["success_count"],
        failure_count=row["failure_count"],
        cooldown_until=_iso_to_dt(row["cooldown_until"]),
        cooldown_reason=_str_to_reason(row["cooldown_reason"]),
        disabled_until=_iso_to_dt(row["disabled_until"]),
        raw_error=row["raw_error"],
    )
    return AuthProfile(
        id=row["id"],
        provider=row["provider"],
        account_identifier=row["account_identifier"],
        backend=row["backend"],
        backend_key=row["backend_key"],
        last_synced_at=_iso_to_dt(row["last_synced_at"]),
        sync_ttl_seconds=row["sync_ttl_seconds"],
        usage_stats=stats,
    )


def _profile_to_tuple(p: AuthProfile) -> tuple[Any, ...]:
    s = p.usage_stats
    raw_error = s.raw_error
    if raw_error and len(raw_error) > RAW_ERROR_MAX_LEN:
        raw_error = raw_error[:RAW_ERROR_MAX_LEN]
    return (
        p.id,
        p.provider,
        p.account_identifier,
        p.backend,
        p.backend_key,
        _dt_to_iso(p.last_synced_at),
        p.sync_ttl_seconds,
        _dt_to_iso(s.last_used_at),
        s.success_count,
        s.failure_count,
        _dt_to_iso(s.cooldown_until),
        _reason_to_str(s.cooldown_reason),
        _dt_to_iso(s.disabled_until),
        raw_error,
    )


def _merge_profile_for_write(
    existing: AuthProfile | None,
    requested: AuthProfile,
    *,
    preserve_runtime_state: bool,
) -> AuthProfile:
    if existing is None or not preserve_runtime_state:
        return requested
    return AuthProfile(
        id=requested.id,
        provider=requested.provider,
        account_identifier=requested.account_identifier,
        backend=requested.backend,
        backend_key=requested.backend_key,
        last_synced_at=requested.last_synced_at,
        sync_ttl_seconds=requested.sync_ttl_seconds,
        usage_stats=_merge_usage_stats_for_preserve(existing.usage_stats, requested.usage_stats),
        preserve_runtime_state=requested.preserve_runtime_state,
    )


# ---------------------------------------------------------------------------
# SqliteAuthProfileStore
# ---------------------------------------------------------------------------


class SqliteAuthProfileStore:
    """SQLite-backed AuthProfileStore for the slim nexus-fs package.

    Uses a single sqlite3 connection with WAL journal mode. All reads go
    through an LRU in-memory cache. Writes go through to SQLite and evict
    the cache entry. Success/failure counters are updated directly in SQLite
    so concurrent writers never flush stale cooldown state back over newer
    rows.

    Thread-safe: all mutable state is protected by ``_lock``.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        cache_size: int = _DEFAULT_CACHE_SIZE,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL_S,
    ) -> None:
        self._db_path = Path(db_path) if not isinstance(db_path, Path) else db_path
        self._cache_size = cache_size
        self._flush_interval = flush_interval
        self._lock = threading.Lock()

        # LRU cache: OrderedDict with move_to_end on access, popitem on overflow.
        self._cache: OrderedDict[str, AuthProfile] = OrderedDict()
        # Retained for backwards-compatible flush()/close() behavior; currently
        # unused because success/failure writes are immediate.
        self._dirty: set[str] = set()
        self._last_flush_time: float = time.monotonic()

        # Initialize connection + schema
        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_CREATE_TABLE)
        self._conn.execute(_CREATE_INDEX_PROVIDER)
        self._conn.commit()

    # ------------------------------------------------------------------
    # AuthProfileStore protocol
    # ------------------------------------------------------------------

    def list(self, *, provider: str | None = None) -> list[AuthProfile]:
        with self._lock:
            self._maybe_flush()
            # Always query SQLite for list() to avoid returning a partial set
            # from the LRU cache (adversarial finding #4). The cache is an
            # object cache for get(), not a complete-snapshot cache for list().
            if provider is not None:
                rows = self._conn.execute(
                    "SELECT * FROM auth_profiles WHERE provider = ?", (provider,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM auth_profiles").fetchall()
            profiles = []
            for r in rows:
                profile_id = r["id"]
                # Dirty entries are kept for API compatibility, but success/failure
                # updates are now immediate so this branch is normally cold.
                if profile_id in self._dirty and profile_id in self._cache:
                    profiles.append(self._cache[profile_id])
                else:
                    p = _row_to_profile(r)
                    self._cache_put(p)
                    profiles.append(p)
            return profiles

    def get(self, profile_id: str) -> AuthProfile | None:
        with self._lock:
            # Check cache first
            if profile_id in self._cache:
                self._cache.move_to_end(profile_id)
                return self._cache[profile_id]
            # Cache miss — read from SQLite
            row = self._conn.execute(
                "SELECT * FROM auth_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if row is None:
                return None
            profile = _row_to_profile(row)
            self._cache_put(profile)
            return profile

    def upsert(self, profile: AuthProfile, *, preserve_runtime_state: bool = False) -> None:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing_row = self._conn.execute(
                    "SELECT * FROM auth_profiles WHERE id = ?", (profile.id,)
                ).fetchone()
                merged = _merge_profile_for_write(
                    _row_to_profile(existing_row) if existing_row is not None else None,
                    profile,
                    preserve_runtime_state=preserve_runtime_state,
                )
                self._conn.execute(_UPSERT, _profile_to_tuple(merged))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
            # Evict from cache so next read picks up the fresh row.
            # Then re-populate with the in-memory object (which is authoritative).
            self._cache.pop(profile.id, None)
            self._dirty.discard(profile.id)
            self._cache_put(merged)

    def delete(self, profile_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM auth_profiles WHERE id = ?", (profile_id,))
            self._conn.commit()
            self._cache.pop(profile_id, None)
            self._dirty.discard(profile_id)

    def replace_owned_subset(
        self,
        *,
        upserts: "builtins.list[AuthProfile]",
        deletes: "builtins.list[str]",
    ) -> None:
        """Apply upserts + deletes in a single SQLite transaction.

        Concurrent readers see either the pre-state or post-state — never
        the half-applied middle (R4-MEDIUM #3740). With WAL journal mode
        readers do not block, but they always observe the last committed
        snapshot, so wrapping the whole batch in one BEGIN/COMMIT prevents
        the brief window where new rows coexist with stale to-be-tombstoned
        rows.
        """
        if not upserts and not deletes:
            return
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                merged_upserts: list[AuthProfile] = []
                for p in upserts:
                    existing_row = self._conn.execute(
                        "SELECT * FROM auth_profiles WHERE id = ?", (p.id,)
                    ).fetchone()
                    merged = _merge_profile_for_write(
                        _row_to_profile(existing_row) if existing_row is not None else None,
                        p,
                        preserve_runtime_state=True,
                    )
                    self._conn.execute(_UPSERT, _profile_to_tuple(merged))
                    merged_upserts.append(merged)
                for pid in deletes:
                    self._conn.execute("DELETE FROM auth_profiles WHERE id = ?", (pid,))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
            # Refresh cache after the transaction so readers via get() also
            # see the new state. Evict deletes; replace upserts.
            for p in merged_upserts:
                self._cache.pop(p.id, None)
                self._dirty.discard(p.id)
                self._cache_put(p)
            for pid in deletes:
                self._cache.pop(pid, None)
                self._dirty.discard(pid)

    def mark_success(self, profile_id: str) -> None:
        """Record a successful credential use directly in SQLite."""
        with self._lock:
            now = datetime.now(UTC)
            now_iso = _dt_to_iso(now)
            self._conn.execute(
                "UPDATE auth_profiles SET "
                "    success_count = success_count + 1, "
                "    last_used_at = ?, "
                "    cooldown_until = CASE "
                "        WHEN cooldown_until IS NULL OR cooldown_until <= ? "
                "        THEN NULL ELSE cooldown_until END, "
                "    cooldown_reason = CASE "
                "        WHEN cooldown_until IS NULL OR cooldown_until <= ? "
                "        THEN NULL ELSE cooldown_reason END "
                "WHERE id = ?",
                (now_iso, now_iso, now_iso, profile_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM auth_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if row is None:
                self._cache.pop(profile_id, None)
                self._dirty.discard(profile_id)
                return
            self._cache.pop(profile_id, None)
            self._dirty.discard(profile_id)
            self._cache_put(_row_to_profile(row))

    def mark_failure(
        self,
        profile_id: str,
        reason: AuthProfileFailureReason,
        *,
        raw_error: str | None = None,
        cooldown_until: CooldownUpdate = COOLDOWN_UNCHANGED,
    ) -> None:
        """Record a failure — immediately persisted (cooldown must be durable)."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM auth_profiles WHERE id = ?", (profile_id,)
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    return
                profile = _row_to_profile(row)
                stats = profile.usage_stats
                last_used_at = datetime.now(UTC)
                merged_until, merged_reason = _merge_cooldown_state(
                    existing_until=stats.cooldown_until,
                    existing_reason=stats.cooldown_reason,
                    requested_until=cooldown_until,
                    requested_reason=reason,
                    now=last_used_at,
                )
                self._conn.execute(
                    "UPDATE auth_profiles SET "
                    "    failure_count = failure_count + 1, "
                    "    last_used_at = ?, "
                    "    cooldown_until = ?, "
                    "    cooldown_reason = ?, "
                    "    raw_error = COALESCE(?, raw_error) "
                    "WHERE id = ?",
                    (
                        _dt_to_iso(last_used_at),
                        _dt_to_iso(merged_until),
                        _reason_to_str(merged_reason),
                        raw_error[:RAW_ERROR_MAX_LEN] if raw_error is not None else None,
                        profile_id,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
            refreshed = self._conn.execute(
                "SELECT * FROM auth_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if refreshed is None:
                self._cache.pop(profile_id, None)
                self._dirty.discard(profile_id)
                return
            self._cache.pop(profile_id, None)
            self._dirty.discard(profile_id)
            self._cache_put(_row_to_profile(refreshed))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Flush all dirty profiles to SQLite."""
        with self._lock:
            self._flush_dirty()

    def close(self) -> None:
        """Flush dirty state and close the connection."""
        with self._lock:
            self._flush_dirty()
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("PRAGMA optimize")
        except Exception:
            pass
        self._conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cache_put(self, profile: AuthProfile) -> None:
        """Add profile to LRU cache, evicting oldest if at capacity.

        Caller must hold ``_lock``.
        """
        if profile.id in self._cache:
            self._cache.move_to_end(profile.id)
            self._cache[profile.id] = profile
            return
        if len(self._cache) >= self._cache_size:
            # Evict oldest; flush it first if dirty
            evicted_id, _evicted = self._cache.popitem(last=False)
            if evicted_id in self._dirty:
                self._flush_one(evicted_id, _evicted)
                self._dirty.discard(evicted_id)
        self._cache[profile.id] = profile

    def _flush_dirty(self) -> None:
        """Write all dirty profiles to SQLite. Caller must hold ``_lock``."""
        if not self._dirty:
            return
        for profile_id in list(self._dirty):
            profile = self._cache.get(profile_id)
            if profile is not None:
                self._flush_one(profile_id, profile)
        self._dirty.clear()
        self._last_flush_time = time.monotonic()

    def _flush_one(self, profile_id: str, profile: AuthProfile) -> None:
        """Write a single profile to SQLite. Caller must hold ``_lock``."""
        try:
            self._conn.execute(_UPSERT, _profile_to_tuple(profile))
            self._conn.commit()
        except sqlite3.Error:
            logger.warning("Failed to flush profile %s", profile_id, exc_info=True)

    def _maybe_flush(self) -> None:
        """Flush dirty profiles if the interval has elapsed. Caller must hold ``_lock``."""
        if self._dirty and (time.monotonic() - self._last_flush_time) >= self._flush_interval:
            self._flush_dirty()
