"""Zone consistency mode migration orchestrator (Issue #1180 Phases C-D).

Orchestrates live migration between SC and EC modes for a zone.

Lifecycle: validate → drain → quiesce → switch DB → (switch Raft: TODO) → unquiesce.
Rollback on failure. Per-zone lock prevents concurrent migrations.

Thread safety:
    All methods are safe for concurrent use. Per-zone locks prevent
    concurrent migrations on the same zone. Different zones can migrate
    concurrently.

Usage:
    from nexus.core.consistency_migration import ConsistencyMigration

    migrator = ConsistencyMigration(session_factory=session_factory)
    result = migrator.migrate("zone-1", ConsistencyMode.EC, timeout_s=30)
    if result.success:
        print(f"Migrated in {result.duration_ms:.0f}ms")
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nexus.core.consistency import (
    ConsistencyMode,
    MigrationState,
    validate_migration,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MigrationResult:
    """Immutable result of a consistency mode migration."""

    success: bool
    zone_id: str
    from_mode: ConsistencyMode
    to_mode: ConsistencyMode
    duration_ms: float
    error: str | None = None


class ConsistencyMigration:
    """Orchestrates live migration between SC and EC modes for a zone.

    Per-zone locking prevents concurrent migrations on the same zone.
    Different zones can migrate concurrently.
    """

    def __init__(
        self,
        session_factory: Any,
        metadata_store: Any | None = None,
    ) -> None:
        """Initialize the migration orchestrator.

        Args:
            session_factory: SQLAlchemy session factory for DB access.
            metadata_store: Optional RaftMetadataStore for Raft mode switching.
        """
        self._session_factory = session_factory
        self._metadata_store = metadata_store

        # Per-zone migration locks
        self._zone_locks: dict[str, threading.Lock] = {}
        self._zone_locks_guard = threading.Lock()

        # Per-zone migration state
        self._zone_states: dict[str, MigrationState] = {}

        # Quiesced zones — checked by write path
        self._quiesced_zones: set[str] = set()
        self._quiesced_lock = threading.Lock()

    def _get_zone_lock(self, zone_id: str) -> threading.Lock:
        """Get or create a per-zone migration lock."""
        with self._zone_locks_guard:
            if zone_id not in self._zone_locks:
                self._zone_locks[zone_id] = threading.Lock()
            return self._zone_locks[zone_id]

    def get_migration_state(self, zone_id: str) -> MigrationState:
        """Get the current migration state for a zone.

        Args:
            zone_id: The zone to query.

        Returns:
            The current MigrationState (IDLE if no migration active).
        """
        return self._zone_states.get(zone_id, MigrationState.IDLE)

    def is_zone_quiesced(self, zone_id: str) -> bool:
        """Check whether a zone is currently quiesced (writes blocked).

        This is called by the write path to reject writes during migration.
        Designed for near-zero cost when no migration is active.

        Args:
            zone_id: The zone to check.

        Returns:
            True if the zone is quiesced (writes should be rejected).
        """
        with self._quiesced_lock:
            return zone_id in self._quiesced_zones

    def migrate(
        self,
        zone_id: str,
        target_mode: ConsistencyMode,
        timeout_s: float = 30.0,
        progress: Callable[[MigrationState], None] | None = None,
    ) -> MigrationResult:
        """Migrate a zone from its current mode to `target_mode`.

        Lifecycle: validate → drain → quiesce → switch DB → unquiesce.
        Rollback on failure at any step.

        Args:
            zone_id: The zone to migrate.
            target_mode: The target consistency mode (SC or EC).
            timeout_s: Maximum time for the entire migration.
            progress: Optional callback invoked on each state transition.

        Returns:
            MigrationResult with success/failure details.
        """
        start = time.monotonic()
        lock = self._get_zone_lock(zone_id)

        if not lock.acquire(timeout=1.0):
            return MigrationResult(
                success=False,
                zone_id=zone_id,
                from_mode=target_mode,  # unknown, use target as placeholder
                to_mode=target_mode,
                duration_ms=(time.monotonic() - start) * 1000,
                error="Another migration is already in progress for this zone",
            )

        try:
            return self._do_migrate(zone_id, target_mode, timeout_s, start, progress)
        finally:
            # Always ensure zone is unquiesced on exit
            self._unquiesce_zone(zone_id)
            self._zone_states[zone_id] = MigrationState.IDLE
            lock.release()

    def _do_migrate(
        self,
        zone_id: str,
        target_mode: ConsistencyMode,
        timeout_s: float,
        start: float,
        progress: Callable[[MigrationState], None] | None,
    ) -> MigrationResult:
        """Internal migration logic (called while holding zone lock)."""

        def _set_state(state: MigrationState) -> None:
            self._zone_states[zone_id] = state
            if progress:
                progress(state)

        # 1. Validate
        current_mode = self._get_current_mode(zone_id)
        if current_mode is None:
            return MigrationResult(
                success=False,
                zone_id=zone_id,
                from_mode=target_mode,
                to_mode=target_mode,
                duration_ms=(time.monotonic() - start) * 1000,
                error=f"Zone '{zone_id}' not found",
            )

        valid, error = validate_migration(current_mode, target_mode)
        if not valid:
            return MigrationResult(
                success=False,
                zone_id=zone_id,
                from_mode=current_mode,
                to_mode=target_mode,
                duration_ms=(time.monotonic() - start) * 1000,
                error=error,
            )

        # 2. Drain writes
        _set_state(MigrationState.DRAINING)
        try:
            self._drain_writes(zone_id, timeout_s)
        except Exception as e:
            _set_state(MigrationState.FAILED)
            return MigrationResult(
                success=False,
                zone_id=zone_id,
                from_mode=current_mode,
                to_mode=target_mode,
                duration_ms=(time.monotonic() - start) * 1000,
                error=f"Failed to drain writes: {e}",
            )

        # 3. Quiesce zone (block new writes)
        _set_state(MigrationState.QUIESCED)
        self._quiesce_zone(zone_id)

        # 4. Switch DB
        _set_state(MigrationState.SWITCHING)
        try:
            self._switch_mode_in_db(zone_id, target_mode)
        except Exception as e:
            _set_state(MigrationState.FAILED)
            return MigrationResult(
                success=False,
                zone_id=zone_id,
                from_mode=current_mode,
                to_mode=target_mode,
                duration_ms=(time.monotonic() - start) * 1000,
                error=f"Failed to switch mode in DB: {e}",
            )

        # 5. Switch Raft mode (TODO(rust): set_lazy binding)
        try:
            self._switch_raft_mode(zone_id, target_mode)
        except NotImplementedError:
            # Expected: Raft mode switching not yet available via PyO3
            logger.info(
                f"Raft mode switch for zone {zone_id} deferred "
                f"(TODO(rust): set_lazy binding)"
            )
        except Exception as e:
            # Rollback DB change
            try:
                self._switch_mode_in_db(zone_id, current_mode)
            except Exception:
                logger.error(f"Failed to rollback DB mode for zone {zone_id}")
            _set_state(MigrationState.FAILED)
            return MigrationResult(
                success=False,
                zone_id=zone_id,
                from_mode=current_mode,
                to_mode=target_mode,
                duration_ms=(time.monotonic() - start) * 1000,
                error=f"Failed to switch Raft mode: {e}",
            )

        # 6. Validate
        _set_state(MigrationState.VALIDATING)

        duration_ms = (time.monotonic() - start) * 1000
        logger.info(
            f"Zone {zone_id} migrated {current_mode.value} → {target_mode.value} "
            f"in {duration_ms:.0f}ms"
        )

        return MigrationResult(
            success=True,
            zone_id=zone_id,
            from_mode=current_mode,
            to_mode=target_mode,
            duration_ms=duration_ms,
        )

    def _get_current_mode(self, zone_id: str) -> ConsistencyMode | None:
        """Read the current consistency mode from the database."""
        from nexus.storage.models import ZoneModel

        with self._session_factory() as session:
            zone = session.get(ZoneModel, zone_id)
            if zone is None:
                return None
            return ConsistencyMode(zone.consistency_mode)

    def _drain_writes(self, zone_id: str, timeout_s: float) -> None:
        """Wait for in-flight writes to complete.

        Currently a short sleep to allow pending operations to finish.
        TODO: Monitor active write count for more precise draining.
        """
        # Brief pause to allow in-flight writes to complete
        time.sleep(min(0.1, timeout_s / 10))

    def _quiesce_zone(self, zone_id: str) -> None:
        """Mark a zone as quiesced — new writes will be rejected."""
        with self._quiesced_lock:
            self._quiesced_zones.add(zone_id)
        logger.info(f"Zone {zone_id} quiesced — writes blocked")

    def _unquiesce_zone(self, zone_id: str) -> None:
        """Remove the quiesce flag — writes are allowed again."""
        with self._quiesced_lock:
            self._quiesced_zones.discard(zone_id)
        logger.debug(f"Zone {zone_id} unquiesced — writes allowed")

    def _switch_mode_in_db(self, zone_id: str, target_mode: ConsistencyMode) -> None:
        """Update ZoneModel.consistency_mode in the database."""
        from nexus.storage.models import ZoneModel

        with self._session_factory() as session:
            zone = session.get(ZoneModel, zone_id)
            if zone is None:
                raise ValueError(f"Zone '{zone_id}' not found")
            zone.consistency_mode = target_mode.value
            session.commit()

    def _switch_raft_mode(
        self, zone_id: str, target_mode: ConsistencyMode
    ) -> None:
        """Switch the Raft consensus mode for a zone.

        TODO(rust): Requires set_lazy(bool) binding on PyRaftConsensus.
        Currently raises NotImplementedError.
        """
        raise NotImplementedError(
            "Raft mode switching requires set_lazy() PyO3 binding. "
            "See Issue #1180 Phase D."
        )
