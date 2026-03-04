"""Unit tests for WrapperMetrics OTel integration (#2077, Issue 12).

Tests verify:
1. In-memory counters always work (regardless of OTel availability)
2. OTel counters are lazy-initialized on first increment
3. OTel counters are created with correct names
4. Disabled metrics skip OTel but still track in-memory
5. Thread safety of increment/get_stats
6. Reset clears in-memory counters

Design reference:
    - Issue #1705: EncryptedStorage + CompressedStorage recursive wrappers
    - Issue #2077: Deduplicate backend wrapper boilerplate
"""

import threading
from unittest.mock import MagicMock, patch

from nexus.backends.wrappers.metrics import WrapperMetrics

# ---------------------------------------------------------------------------
# In-Memory Counter Tests
# ---------------------------------------------------------------------------


class TestInMemoryCounters:
    """In-memory counters should always work regardless of OTel."""

    def test_increment_and_get_stats(self) -> None:
        metrics = WrapperMetrics(
            meter_name="test.metrics",
            counter_names=["ops", "errors"],
            enabled=False,  # OTel disabled
        )

        metrics.increment("ops")
        metrics.increment("ops")
        metrics.increment("errors")

        stats = metrics.get_stats()
        assert stats == {"ops": 2, "errors": 1}

    def test_increment_unknown_counter_is_no_op(self) -> None:
        metrics = WrapperMetrics(
            meter_name="test.metrics",
            counter_names=["ops"],
            enabled=False,
        )

        # Should not raise
        metrics.increment("nonexistent")
        assert metrics.get_stats() == {"ops": 0}

    def test_increment_with_delta(self) -> None:
        metrics = WrapperMetrics(
            meter_name="test.metrics",
            counter_names=["bytes_saved"],
            enabled=False,
        )

        metrics.increment("bytes_saved", 100)
        metrics.increment("bytes_saved", 200)
        assert metrics.get_stats()["bytes_saved"] == 300

    def test_reset_clears_counters(self) -> None:
        metrics = WrapperMetrics(
            meter_name="test.metrics",
            counter_names=["ops", "errors"],
            enabled=False,
        )

        metrics.increment("ops", 5)
        metrics.increment("errors", 3)
        metrics.reset()
        assert metrics.get_stats() == {"ops": 0, "errors": 0}

    def test_get_stats_returns_copy(self) -> None:
        metrics = WrapperMetrics(
            meter_name="test.metrics",
            counter_names=["ops"],
            enabled=False,
        )

        metrics.increment("ops")
        stats = metrics.get_stats()
        stats["ops"] = 999  # Mutate the copy
        assert metrics.get_stats()["ops"] == 1  # Original unchanged


# ---------------------------------------------------------------------------
# OTel Integration Tests (mocked)
# ---------------------------------------------------------------------------


class TestOTelIntegration:
    """OTel counters should be lazy-initialized and increment correctly."""

    def test_otel_counters_created_on_first_increment(self) -> None:
        mock_meter = MagicMock()
        mock_counter = MagicMock()
        mock_meter.create_counter.return_value = mock_counter

        mock_get_meter = MagicMock(return_value=mock_meter)

        with (
            patch("nexus.lib.telemetry.is_telemetry_enabled", return_value=True),
            patch("opentelemetry.metrics.get_meter", mock_get_meter),
        ):
            metrics = WrapperMetrics(
                meter_name="nexus.test",
                counter_names=["ops", "errors"],
                enabled=True,
            )

            metrics.increment("ops")

            # Verify meter was created with correct name
            mock_get_meter.assert_called_once_with("nexus.test")

            # Verify counters were created for all names
            assert mock_meter.create_counter.call_count == 2
            counter_names = [call[0][0] for call in mock_meter.create_counter.call_args_list]
            assert "nexus.test.ops" in counter_names
            assert "nexus.test.errors" in counter_names

            # Verify OTel counter was incremented
            mock_counter.add.assert_called_with(1)

    def test_otel_disabled_skips_counter_creation(self) -> None:
        metrics = WrapperMetrics(
            meter_name="nexus.test",
            counter_names=["ops"],
            enabled=False,
        )

        metrics.increment("ops")
        # In-memory should still work
        assert metrics.get_stats()["ops"] == 1
        # OTel should not be initialized
        assert metrics._otel_counters is None

    def test_otel_import_failure_graceful(self) -> None:
        """If OTel is not installed, should fall back gracefully."""
        metrics = WrapperMetrics(
            meter_name="nexus.test",
            counter_names=["ops"],
            enabled=True,
        )

        with patch(
            "nexus.lib.telemetry.is_telemetry_enabled",
            side_effect=ImportError("no telemetry"),
        ):
            metrics.increment("ops")

        # In-memory should still work
        assert metrics.get_stats()["ops"] == 1


# ---------------------------------------------------------------------------
# Thread Safety Tests
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """WrapperMetrics should be safe for concurrent access."""

    def test_concurrent_increments(self) -> None:
        metrics = WrapperMetrics(
            meter_name="test.concurrent",
            counter_names=["ops"],
            enabled=False,
        )

        def worker() -> None:
            for _ in range(1000):
                metrics.increment("ops")

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert metrics.get_stats()["ops"] == 10_000
