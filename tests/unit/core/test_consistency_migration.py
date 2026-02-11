"""Tests for ConsistencyMigration orchestrator (Issue #1180 Phases C-D).

Covers:
1. Successful SC → EC migration
2. Successful EC → SC migration
3. Same-mode migration rejected
4. Zone not found
5. Concurrent migration lock
6. State transitions during migration
7. Quiesce/unquiesce lifecycle
8. DB mode switch
9. Raft mode switch (NotImplementedError handled)
10. Rollback on DB failure
11. MigrationResult immutability
12. Progress callback
13. is_zone_quiesced zero-cost when idle
14. get_migration_state defaults to IDLE
15. Timeout parameter respected
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.consistency import ConsistencyMode, MigrationState
from nexus.core.consistency_migration import ConsistencyMigration, MigrationResult
from nexus.storage.models import ZoneModel
from nexus.storage.models._base import Base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create an in-memory SQLite engine."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _set_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    """Create a session factory."""
    return sessionmaker(bind=engine)


@pytest.fixture
def zone_sc(session_factory):
    """Create a zone in SC mode."""
    with session_factory() as session:
        zone = ZoneModel(zone_id="zone-sc", name="SC Zone", consistency_mode="SC")
        session.add(zone)
        session.commit()
    return "zone-sc"


@pytest.fixture
def zone_ec(session_factory):
    """Create a zone in EC mode."""
    with session_factory() as session:
        zone = ZoneModel(zone_id="zone-ec", name="EC Zone", consistency_mode="EC")
        session.add(zone)
        session.commit()
    return "zone-ec"


@pytest.fixture
def migrator(session_factory):
    """Create a ConsistencyMigration instance."""
    return ConsistencyMigration(session_factory=session_factory)


# ---------------------------------------------------------------------------
# 1. Successful SC → EC migration
# ---------------------------------------------------------------------------


class TestSuccessfulMigration:
    def test_sc_to_ec_success(self, migrator, zone_sc, session_factory) -> None:
        """SC → EC migration succeeds and updates DB."""
        result = migrator.migrate(zone_sc, ConsistencyMode.EC)

        assert result.success is True
        assert result.zone_id == zone_sc
        assert result.from_mode == ConsistencyMode.SC
        assert result.to_mode == ConsistencyMode.EC
        assert result.error is None
        assert result.duration_ms > 0

        # Verify DB was updated
        with session_factory() as session:
            zone = session.get(ZoneModel, zone_sc)
            assert zone.consistency_mode == "EC"

    def test_ec_to_sc_success(self, migrator, zone_ec, session_factory) -> None:
        """EC → SC migration succeeds and updates DB."""
        result = migrator.migrate(zone_ec, ConsistencyMode.SC)

        assert result.success is True
        assert result.from_mode == ConsistencyMode.EC
        assert result.to_mode == ConsistencyMode.SC

        with session_factory() as session:
            zone = session.get(ZoneModel, zone_ec)
            assert zone.consistency_mode == "SC"


# ---------------------------------------------------------------------------
# 3. Same-mode rejection
# ---------------------------------------------------------------------------


class TestSameModeRejection:
    def test_same_mode_rejected(self, migrator, zone_sc) -> None:
        """Migrating to the same mode returns failure."""
        result = migrator.migrate(zone_sc, ConsistencyMode.SC)

        assert result.success is False
        assert "already" in result.error.lower()


# ---------------------------------------------------------------------------
# 4. Zone not found
# ---------------------------------------------------------------------------


class TestZoneNotFound:
    def test_nonexistent_zone(self, migrator) -> None:
        """Migration for a nonexistent zone returns failure."""
        result = migrator.migrate("nonexistent", ConsistencyMode.EC)

        assert result.success is False
        assert "not found" in result.error.lower()


# ---------------------------------------------------------------------------
# 5. Concurrent migration lock
# ---------------------------------------------------------------------------


class TestConcurrentMigrationLock:
    def test_concurrent_migration_blocked(self, session_factory) -> None:
        """Only one migration per zone at a time."""
        with session_factory() as session:
            zone = ZoneModel(zone_id="zone-lock", name="Lock Zone", consistency_mode="SC")
            session.add(zone)
            session.commit()

        # Create a migrator with a slow drain to hold the lock
        migrator = ConsistencyMigration(session_factory=session_factory)

        results: list[MigrationResult] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        original_drain = migrator._drain_writes

        def slow_drain(zone_id: str, timeout_s: float) -> None:
            barrier.wait(timeout=3)  # Sync: both threads running
            time.sleep(1.0)  # Hold the lock for 1s
            original_drain(zone_id, timeout_s)

        def migrate_1() -> None:
            with patch.object(migrator, "_drain_writes", side_effect=slow_drain):
                r = migrator.migrate("zone-lock", ConsistencyMode.EC)
            with lock:
                results.append(r)

        def migrate_2() -> None:
            barrier.wait(timeout=3)  # Sync: both threads running
            time.sleep(0.05)  # Brief delay for t1 to acquire lock
            r = migrator.migrate("zone-lock", ConsistencyMode.EC)
            with lock:
                results.append(r)

        t1 = threading.Thread(target=migrate_1, daemon=True)
        t2 = threading.Thread(target=migrate_2, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 2
        # One should succeed and one should fail
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 1
        assert len(failures) == 1
        # The failure should be either "already in progress" (lock contention)
        # or "already in EC mode" (first completed before second started)
        err = failures[0].error.lower()
        assert "already" in err


# ---------------------------------------------------------------------------
# 6. State transitions
# ---------------------------------------------------------------------------


class TestStateTransitions:
    def test_state_transitions_during_migration(self, migrator, zone_sc) -> None:
        """Migration progresses through expected states."""
        observed_states: list[MigrationState] = []

        def on_progress(state: MigrationState) -> None:
            observed_states.append(state)

        migrator.migrate(zone_sc, ConsistencyMode.EC, progress=on_progress)

        assert MigrationState.DRAINING in observed_states
        assert MigrationState.QUIESCED in observed_states
        assert MigrationState.SWITCHING in observed_states
        assert MigrationState.VALIDATING in observed_states


# ---------------------------------------------------------------------------
# 7. Quiesce/unquiesce lifecycle
# ---------------------------------------------------------------------------


class TestQuiesceLifecycle:
    def test_zone_not_quiesced_before_migration(self, migrator, zone_sc) -> None:
        """Zone is not quiesced before migration starts."""
        assert migrator.is_zone_quiesced(zone_sc) is False

    def test_zone_unquiesced_after_migration(self, migrator, zone_sc) -> None:
        """Zone is unquiesced after migration completes (success or failure)."""
        migrator.migrate(zone_sc, ConsistencyMode.EC)
        assert migrator.is_zone_quiesced(zone_sc) is False

    def test_zone_quiesced_during_migration(self, session_factory) -> None:
        """Zone is quiesced during the SWITCHING phase."""
        with session_factory() as session:
            zone = ZoneModel(zone_id="zone-q", name="Q Zone", consistency_mode="SC")
            session.add(zone)
            session.commit()

        migrator = ConsistencyMigration(session_factory=session_factory)
        was_quiesced = False

        original_switch = migrator._switch_mode_in_db

        def check_quiesce(zone_id: str, target_mode: ConsistencyMode) -> None:
            nonlocal was_quiesced
            was_quiesced = migrator.is_zone_quiesced(zone_id)
            original_switch(zone_id, target_mode)

        with patch.object(migrator, "_switch_mode_in_db", side_effect=check_quiesce):
            migrator.migrate("zone-q", ConsistencyMode.EC)

        assert was_quiesced is True


# ---------------------------------------------------------------------------
# 8. DB mode switch
# ---------------------------------------------------------------------------


class TestDBModeSwitch:
    def test_db_mode_switched(self, migrator, zone_sc, session_factory) -> None:
        """Database is updated to new mode after migration."""
        migrator.migrate(zone_sc, ConsistencyMode.EC)

        with session_factory() as session:
            zone = session.get(ZoneModel, zone_sc)
            assert zone.consistency_mode == "EC"


# ---------------------------------------------------------------------------
# 9. Raft mode switch (NotImplementedError)
# ---------------------------------------------------------------------------


class TestRaftModeSwitch:
    def test_raft_not_implemented_is_handled(self, migrator, zone_sc) -> None:
        """NotImplementedError from _switch_raft_mode is handled gracefully."""
        result = migrator.migrate(zone_sc, ConsistencyMode.EC)
        # Should succeed — Raft switch is deferred
        assert result.success is True


# ---------------------------------------------------------------------------
# 10. Rollback on DB failure
# ---------------------------------------------------------------------------


class TestRollback:
    def test_rollback_on_db_failure(self, session_factory) -> None:
        """If DB switch fails, zone stays in original mode."""
        with session_factory() as session:
            zone = ZoneModel(zone_id="zone-rb", name="RB Zone", consistency_mode="SC")
            session.add(zone)
            session.commit()

        migrator = ConsistencyMigration(session_factory=session_factory)

        def fail_switch(zone_id: str, target_mode: ConsistencyMode) -> None:
            raise RuntimeError("DB write failed")

        with patch.object(migrator, "_switch_mode_in_db", side_effect=fail_switch):
            result = migrator.migrate("zone-rb", ConsistencyMode.EC)

        assert result.success is False
        assert "DB" in result.error

        # Zone should still be SC
        with session_factory() as session:
            zone = session.get(ZoneModel, "zone-rb")
            assert zone.consistency_mode == "SC"


# ---------------------------------------------------------------------------
# 11. MigrationResult immutability
# ---------------------------------------------------------------------------


class TestMigrationResultImmutable:
    def test_migration_result_frozen(self) -> None:
        """MigrationResult is frozen (immutable)."""
        result = MigrationResult(
            success=True,
            zone_id="z1",
            from_mode=ConsistencyMode.SC,
            to_mode=ConsistencyMode.EC,
            duration_ms=42.0,
        )
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 12. Progress callback
# ---------------------------------------------------------------------------


class TestProgressCallback:
    def test_progress_callback_called(self, migrator, zone_sc) -> None:
        """Progress callback is invoked for each state transition."""
        callback = MagicMock()
        migrator.migrate(zone_sc, ConsistencyMode.EC, progress=callback)
        assert callback.call_count >= 4  # DRAINING, QUIESCED, SWITCHING, VALIDATING


# ---------------------------------------------------------------------------
# 13. is_zone_quiesced zero-cost when idle
# ---------------------------------------------------------------------------


class TestQuiesceZeroCost:
    def test_is_zone_quiesced_fast_when_idle(self, migrator) -> None:
        """is_zone_quiesced returns False very quickly when no migration active."""
        start = time.monotonic()
        for _ in range(10000):
            migrator.is_zone_quiesced("nonexistent-zone")
        elapsed_ms = (time.monotonic() - start) * 1000

        # 10,000 calls should take well under 100ms
        assert elapsed_ms < 100, f"10K calls took {elapsed_ms:.1f}ms"


# ---------------------------------------------------------------------------
# 14. get_migration_state defaults to IDLE
# ---------------------------------------------------------------------------


class TestGetMigrationState:
    def test_defaults_to_idle(self, migrator) -> None:
        """Unknown zones return IDLE."""
        assert migrator.get_migration_state("unknown") == MigrationState.IDLE

    def test_returns_idle_after_migration(self, migrator, zone_sc) -> None:
        """After migration completes, state returns to IDLE."""
        migrator.migrate(zone_sc, ConsistencyMode.EC)
        assert migrator.get_migration_state(zone_sc) == MigrationState.IDLE


# ---------------------------------------------------------------------------
# 15. Drain failure
# ---------------------------------------------------------------------------


class TestDrainFailure:
    def test_drain_failure_returns_error(self, session_factory) -> None:
        """If drain fails, migration returns error."""
        with session_factory() as session:
            zone = ZoneModel(zone_id="zone-df", name="DF Zone", consistency_mode="SC")
            session.add(zone)
            session.commit()

        migrator = ConsistencyMigration(session_factory=session_factory)

        def fail_drain(zone_id: str, timeout_s: float) -> None:
            raise RuntimeError("Drain timeout")

        with patch.object(migrator, "_drain_writes", side_effect=fail_drain):
            result = migrator.migrate("zone-df", ConsistencyMode.EC)

        assert result.success is False
        assert "drain" in result.error.lower()
