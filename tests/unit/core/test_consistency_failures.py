"""Unit tests for consistency failure modes and edge cases (Issue #1180).

Tests the failure-path behavior of the consistency subsystem:
- _wait_for_revision timeout and success paths
- _check_consistency_before_read with STRONG/CTO/EVENTUAL
- ConsistencyTimeoutError debugging info
- STRONG on EC store downgrade to CTO with warning
- COMPATIBILITY_MATRIX exhaustive coverage
- Per-zone lock contention isolation
- Poll backoff cap at 10ms

These tests exercise the NexusFSCoreMixin methods directly by constructing
a minimal test harness that provides the required attributes and methods.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from typing import Any
from unittest.mock import patch

import pytest

from nexus.core._metadata_generated import FileMetadata, FileMetadataProtocol
from nexus.core.consistency import (
    COMPATIBILITY_MATRIX,
    ConsistencyMode,
    FSConsistency,
    StoreMode,
)
from nexus.core.nexus_fs_core import NexusFSCoreMixin
from nexus.core.permissions import OperationContext
from nexus.core.zookie import ConsistencyTimeoutError, InvalidZookieError, Zookie

# ---------------------------------------------------------------------------
# Test harness: minimal object that satisfies NexusFSCoreMixin dependencies
# ---------------------------------------------------------------------------


class InMemoryMetadataStore(FileMetadataProtocol):
    """Minimal in-memory metadata store for unit tests."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}
        self._mode: StoreMode = StoreMode.EMBEDDED  # default: no EC

    @property
    def mode(self) -> StoreMode:
        """Operational mode (mirrors RaftMetadataStore.mode)."""
        return self._mode

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata) -> None:
        self._store[metadata.path] = metadata

    def delete(self, path: str) -> dict[str, Any] | None:
        return self._store.pop(path, None)

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(self, prefix: str = "", recursive: bool = True, **kwargs: Any) -> list[FileMetadata]:
        return [m for p, m in self._store.items() if p.startswith(prefix)]

    def delete_batch(self, paths: Sequence[str]) -> None:
        for p in paths:
            self._store.pop(p, None)

    def is_implicit_directory(self, path: str) -> bool:
        pfx = path.rstrip("/") + "/"
        return any(p.startswith(pfx) for p in self._store)

    def close(self) -> None:
        self._store.clear()


class StubFS(NexusFSCoreMixin):
    """Minimal stub that provides attributes required by NexusFSCoreMixin.

    Does NOT inherit from NexusFS to avoid pulling in backends and routing.
    Provides just enough surface area for the consistency methods under test.
    """

    def __init__(self, metadata: InMemoryMetadataStore | None = None) -> None:
        self.metadata = metadata or InMemoryMetadataStore()

    # _check_consistency_before_read calls _get_routing_params(context)
    def _get_routing_params(
        self, context: OperationContext | dict[Any, Any] | None
    ) -> tuple[str | None, str | None, bool]:
        if context is None:
            return (None, None, False)
        if isinstance(context, dict):
            return (context.get("zone_id"), context.get("agent_id"), False)
        return (getattr(context, "zone_id", None), getattr(context, "agent_id", None), False)


def _make_ctx(
    consistency: FSConsistency = FSConsistency.CLOSE_TO_OPEN,
    min_zookie: str | None = None,
    zone_id: str | None = None,
) -> OperationContext:
    """Create an OperationContext with consistency settings."""
    return OperationContext(
        user="test_user",
        groups=[],
        consistency=consistency,
        min_zookie=min_zookie,
        zone_id=zone_id,
    )


# ---------------------------------------------------------------------------
# 1. _wait_for_revision timeout / success tests
# ---------------------------------------------------------------------------


class TestWaitForRevision:
    """Tests for _wait_for_revision polling and timeout behavior."""

    def test_wait_for_revision_timeout_returns_false(self) -> None:
        """When the deadline expires before the target revision is reached, return False."""
        fs = StubFS()
        # Write revision 5 into the store
        fs._increment_and_get_revision("z1")  # rev 1
        for _ in range(4):
            fs._increment_and_get_revision("z1")  # rev 2..5

        # Target revision 100 is unreachable; use a short timeout
        result = fs._wait_for_revision("z1", min_revision=100, timeout_ms=50)
        assert result is False

    def test_wait_for_revision_immediate_success(self) -> None:
        """When the current revision already meets the target, return True immediately."""
        fs = StubFS()
        for _ in range(10):
            fs._increment_and_get_revision("z1")

        start = time.monotonic()
        result = fs._wait_for_revision("z1", min_revision=5, timeout_ms=5000)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert result is True
        # Should return almost instantly (well under 100ms)
        assert elapsed_ms < 100, f"Took {elapsed_ms:.1f}ms, expected near-instant"

    def test_wait_for_revision_eventual_success(self) -> None:
        """When the revision arrives during the wait, return True."""
        fs = StubFS()
        fs._increment_and_get_revision("z1")  # rev 1

        target_revision = 3
        arrived = threading.Event()

        def advance_revision() -> None:
            """Simulate a concurrent writer advancing the revision."""
            time.sleep(0.01)  # 10ms delay
            fs._increment_and_get_revision("z1")  # rev 2
            fs._increment_and_get_revision("z1")  # rev 3
            arrived.set()

        t = threading.Thread(target=advance_revision, daemon=True)
        t.start()

        result = fs._wait_for_revision("z1", min_revision=target_revision, timeout_ms=2000)
        t.join(timeout=3)

        assert result is True
        assert arrived.is_set()

    def test_notification_wakeup_latency(self) -> None:
        """Verify Condition-based notification wakes waiter within ~10ms (Issue #1180 Phase B).

        After Phase B, _wait_for_revision uses RevisionNotifier (threading.Condition)
        instead of polling. A writer thread notifies and the waiter wakes up quickly.
        """
        fs = StubFS()
        fs._increment_and_get_revision("z1")  # rev 1

        def writer() -> None:
            time.sleep(0.02)  # 20ms delay to let waiter block
            fs._increment_and_get_revision("z1")  # rev 2
            fs._increment_and_get_revision("z1")  # rev 3

        t = threading.Thread(target=writer, daemon=True)
        start = time.monotonic()
        t.start()

        result = fs._wait_for_revision("z1", min_revision=3, timeout_ms=2000)
        elapsed_ms = (time.monotonic() - start) * 1000
        t.join(timeout=3)

        assert result is True
        # Should wake up shortly after the writer notifies (~20-30ms total)
        assert elapsed_ms < 200, (
            f"Wakeup took {elapsed_ms:.1f}ms, expected < 200ms with Condition notification"
        )


# ---------------------------------------------------------------------------
# 2. _check_consistency_before_read — STRONG timeout raises
# ---------------------------------------------------------------------------


class TestCheckConsistencyBeforeRead:
    """Tests for _check_consistency_before_read with different consistency levels."""

    def test_strong_timeout_raises_error(self) -> None:
        """STRONG consistency with an unreachable revision should raise ConsistencyTimeoutError."""
        fs = StubFS()
        fs._increment_and_get_revision("default")  # rev 1

        future_zookie = Zookie.encode("default", 999999)
        ctx = _make_ctx(
            consistency=FSConsistency.STRONG,
            min_zookie=future_zookie,
        )

        # Override timeout to keep test fast
        with (
            pytest.raises(ConsistencyTimeoutError),
            patch.object(fs, "_wait_for_revision", return_value=False),
        ):
            fs._check_consistency_before_read(ctx)

    def test_cto_timeout_falls_through(self) -> None:
        """CTO consistency with an unreachable revision should NOT raise — just fall through."""
        fs = StubFS()
        fs._increment_and_get_revision("default")  # rev 1

        future_zookie = Zookie.encode("default", 999999)
        ctx = _make_ctx(
            consistency=FSConsistency.CLOSE_TO_OPEN,
            min_zookie=future_zookie,
        )

        # Should NOT raise
        with patch.object(fs, "_wait_for_revision", return_value=False):
            fs._check_consistency_before_read(ctx)
        # If we get here without exception, test passes

    def test_timeout_error_contains_debugging_info(self) -> None:
        """ConsistencyTimeoutError should contain zone_id, revisions, and timeout_ms."""
        fs = StubFS()
        for _ in range(5):
            fs._increment_and_get_revision("zone_abc")

        future_zookie = Zookie.encode("zone_abc", 999)
        ctx = _make_ctx(
            consistency=FSConsistency.STRONG,
            min_zookie=future_zookie,
            zone_id="zone_abc",
        )

        with (
            pytest.raises(ConsistencyTimeoutError) as exc_info,
            patch.object(fs, "_wait_for_revision", return_value=False),
        ):
            fs._check_consistency_before_read(ctx)

        err = exc_info.value
        assert err.zone_id == "zone_abc"
        assert err.requested_revision == 999
        assert err.current_revision == 5
        assert err.timeout_ms > 0

    def test_revision_recovery_after_timeout(self) -> None:
        """After a timeout, the next operation with a satisfied revision should succeed."""
        fs = StubFS()
        for _ in range(5):
            fs._increment_and_get_revision("default")

        # First: STRONG with unreachable revision -> timeout
        future_zookie = Zookie.encode("default", 999999)
        ctx_fail = _make_ctx(
            consistency=FSConsistency.STRONG,
            min_zookie=future_zookie,
        )
        with (
            pytest.raises(ConsistencyTimeoutError),
            patch.object(fs, "_wait_for_revision", return_value=False),
        ):
            fs._check_consistency_before_read(ctx_fail)

        # Second: same zone, reachable revision -> success
        ok_zookie = Zookie.encode("default", 3)
        ctx_ok = _make_ctx(
            consistency=FSConsistency.STRONG,
            min_zookie=ok_zookie,
        )
        # This should NOT raise (revision 3 <= current 5)
        fs._check_consistency_before_read(ctx_ok)

    def test_eventual_on_ec_skips_check(self) -> None:
        """EVENTUAL consistency should skip the zookie check entirely, regardless of store mode."""
        fs = StubFS()
        # Even with a future zookie, EVENTUAL should not block or raise
        future_zookie = Zookie.encode("default", 999999)
        ctx = _make_ctx(
            consistency=FSConsistency.EVENTUAL,
            min_zookie=future_zookie,
        )
        # Should return without waiting or raising
        fs._check_consistency_before_read(ctx)

    def test_invalid_zookie_strong_raises(self) -> None:
        """STRONG consistency with an invalid zookie should raise InvalidZookieError."""
        fs = StubFS()
        ctx = _make_ctx(
            consistency=FSConsistency.STRONG,
            min_zookie="this_is_not_a_valid_zookie",
        )
        with pytest.raises(InvalidZookieError):
            fs._check_consistency_before_read(ctx)


# ---------------------------------------------------------------------------
# 3. STRONG on EC store downgrade to CTO with warning (Issue #1180)
# ---------------------------------------------------------------------------


class TestStrongOnECDowngrade:
    """Tests for STRONG consistency on EC-mode metadata store.

    Issue #1180: When the metadata store is in EC mode, STRONG consistency
    cannot be guaranteed. The system should downgrade to CLOSE_TO_OPEN
    and log a warning rather than providing a false guarantee.
    """

    def test_strong_on_ec_downgraded_to_cto(self, caplog: pytest.LogCaptureFixture) -> None:
        """STRONG on EC store should be downgraded to CTO behavior (no raise on timeout).

        After Issue #1180, _check_consistency_before_read should detect that the
        metadata store is in EC mode and treat STRONG as CTO (warn_then_wait):
        on timeout, fall through instead of raising ConsistencyTimeoutError.
        """
        metadata = InMemoryMetadataStore()
        metadata._mode = StoreMode.EC
        fs = StubFS(metadata=metadata)
        fs._increment_and_get_revision("default")  # rev 1

        future_zookie = Zookie.encode("default", 999999)
        ctx = _make_ctx(
            consistency=FSConsistency.STRONG,
            min_zookie=future_zookie,
        )

        # After Issue #1180: STRONG on EC should NOT raise (downgraded to CTO)
        # It should fall through like CTO does.
        with (
            caplog.at_level(logging.WARNING),
            patch.object(fs, "_wait_for_revision", return_value=False),
        ):
            fs._check_consistency_before_read(ctx)

        # Verify a warning was logged about the downgrade
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("EC" in msg or "downgrade" in msg.lower() for msg in warning_messages), (
            f"Expected a warning about EC/downgrade. Got: {warning_messages}"
        )

    def test_compat_matrix_ec_strong_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify that the COMPATIBILITY_MATRIX value for (EC, STRONG) is 'warn_then_wait'.

        This test validates the matrix definition directly, then verifies the
        runtime behavior logs a warning.
        """
        # Direct matrix check
        behavior = COMPATIBILITY_MATRIX[(ConsistencyMode.EC, FSConsistency.STRONG)]
        assert behavior == "warn_then_wait"

        # Runtime behavior: should log warning
        metadata = InMemoryMetadataStore()
        metadata._mode = StoreMode.EC
        fs = StubFS(metadata=metadata)
        fs._increment_and_get_revision("default")

        reachable_zookie = Zookie.encode("default", 1)
        ctx = _make_ctx(
            consistency=FSConsistency.STRONG,
            min_zookie=reachable_zookie,
        )

        with caplog.at_level(logging.WARNING):
            fs._check_consistency_before_read(ctx)

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "EC" in msg or "STRONG" in msg or "downgrade" in msg.lower() for msg in warning_messages
        ), f"Expected EC/STRONG/downgrade warning. Got: {warning_messages}"


# ---------------------------------------------------------------------------
# 4. COMPATIBILITY_MATRIX exhaustive tests
# ---------------------------------------------------------------------------


class TestCompatibilityMatrix:
    """Tests for the COMPATIBILITY_MATRIX covering all 6 (ConsistencyMode x FSConsistency) pairs."""

    @pytest.mark.parametrize(
        "mode,consistency,expected_behavior",
        [
            (ConsistencyMode.SC, FSConsistency.EVENTUAL, "skip_zookie_wait"),
            (ConsistencyMode.SC, FSConsistency.CLOSE_TO_OPEN, "wait_best_effort"),
            (ConsistencyMode.SC, FSConsistency.STRONG, "wait_or_raise"),
            (ConsistencyMode.EC, FSConsistency.EVENTUAL, "skip_zookie_wait"),
            (ConsistencyMode.EC, FSConsistency.CLOSE_TO_OPEN, "wait_best_effort"),
            (ConsistencyMode.EC, FSConsistency.STRONG, "warn_then_wait"),
        ],
    )
    def test_compat_matrix_all_6_combinations(
        self,
        mode: ConsistencyMode,
        consistency: FSConsistency,
        expected_behavior: str,
    ) -> None:
        """Each (ConsistencyMode, FSConsistency) pair maps to the expected behavior string."""
        assert (mode, consistency) in COMPATIBILITY_MATRIX
        assert COMPATIBILITY_MATRIX[(mode, consistency)] == expected_behavior

    def test_matrix_has_exactly_6_entries(self) -> None:
        """COMPATIBILITY_MATRIX should have exactly 6 entries (2 modes x 3 levels)."""
        assert len(COMPATIBILITY_MATRIX) == 6

    def test_matrix_covers_all_mode_consistency_pairs(self) -> None:
        """Every combination of ConsistencyMode and FSConsistency should be in the matrix."""
        for mode in ConsistencyMode:
            for consistency in FSConsistency:
                assert (mode, consistency) in COMPATIBILITY_MATRIX, (
                    f"Missing matrix entry for ({mode}, {consistency})"
                )

    def test_skip_behaviors_are_for_eventual_only(self) -> None:
        """'skip_zookie_wait' should only appear for EVENTUAL consistency."""
        for (mode, consistency), behavior in COMPATIBILITY_MATRIX.items():
            if behavior == "skip_zookie_wait":
                assert consistency == FSConsistency.EVENTUAL, (
                    f"skip_zookie_wait should only be for EVENTUAL, found for ({mode}, {consistency})"
                )


# ---------------------------------------------------------------------------
# 5. Per-zone lock tests (Issue #1180)
# ---------------------------------------------------------------------------


class TestPerZoneLock:
    """Tests for per-zone lock isolation (Issue #1180).

    Issue #1180 replaces the single _revision_lock with per-zone locks
    so that writes to zone A do not block writes to zone B.
    """

    def test_per_zone_lock_no_cross_zone_contention(self) -> None:
        """Writes to zone A should not block writes to zone B.

        After Issue #1180, zone A and zone B should use separate locks,
        allowing concurrent writes to different zones without contention.
        """
        fs = StubFS()
        zone_a_revisions: list[int] = []
        zone_b_revisions: list[int] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def write_zone_a() -> None:
            try:
                barrier.wait(timeout=2)
                for _ in range(5):
                    rev = fs._increment_and_get_revision("zone_a")
                    zone_a_revisions.append(rev)
            except Exception as e:
                errors.append(e)

        def write_zone_b() -> None:
            try:
                barrier.wait(timeout=2)
                for _ in range(5):
                    rev = fs._increment_and_get_revision("zone_b")
                    zone_b_revisions.append(rev)
            except Exception as e:
                errors.append(e)

        t_a = threading.Thread(target=write_zone_a, daemon=True)
        t_b = threading.Thread(target=write_zone_b, daemon=True)
        t_a.start()
        t_b.start()
        t_a.join(timeout=5)
        t_b.join(timeout=5)

        assert not errors, f"Unexpected errors: {errors}"
        # Each zone should have 5 unique, monotonically increasing revisions
        assert zone_a_revisions == [1, 2, 3, 4, 5]
        assert zone_b_revisions == [1, 2, 3, 4, 5]

    def test_per_zone_lock_same_zone_serialized(self) -> None:
        """Concurrent writes to the same zone should be serialized (no duplicates).

        Even with per-zone locks, writes within a single zone must be atomic.
        """
        fs = StubFS()
        all_revisions: list[int] = []
        lock = threading.Lock()
        errors: list[Exception] = []

        def write_many(count: int) -> None:
            try:
                for _ in range(count):
                    rev = fs._increment_and_get_revision("shared_zone")
                    with lock:
                        all_revisions.append(rev)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=write_many, args=(10,), daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Unexpected errors: {errors}"
        # 4 threads x 10 writes = 40 unique revisions
        assert len(all_revisions) == 40
        assert len(set(all_revisions)) == 40, f"Duplicate revisions: {sorted(all_revisions)}"
        # All revisions should be in range [1, 40]
        assert min(all_revisions) == 1
        assert max(all_revisions) == 40
