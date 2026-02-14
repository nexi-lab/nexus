"""Tests for ManifestMetricsObserver (Issue #1428).

Covers:
1. Config: default values, frozen immutability
2. Resolution counters: start/end increments, error counting
3. Source counters: per-type tracking, status breakdown
4. Slow source detection: above/below threshold, warning logged
5. Error/timeout logging: warning logged
6. Circuit breaker: auto-disable after N errors, disabled skips hooks
7. Snapshot: returns complete dict, reflects by_source_type breakdown
8. Reset: clears all counters, re-enables after disable
9. Disabled config: skips all tracking
10. Thread safety: 10 threads x 100 calls, verify total == 1000
11. Integration: resolver calls metrics hooks on success/failure
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from nexus.services.context_manifest.metrics import (
    ManifestMetricsConfig,
    ManifestMetricsObserver,
    SourceEvent,
)

# ---------------------------------------------------------------------------
# Test 1: Config defaults and immutability
# ---------------------------------------------------------------------------


class TestConfig:
    def test_default_values(self) -> None:
        """Config has sensible defaults."""
        config = ManifestMetricsConfig()
        assert config.enabled is True
        assert config.slow_source_threshold_ms == 2000.0
        assert config.max_listener_errors == 10
        assert config.log_source_names is True

    def test_frozen_immutability(self) -> None:
        """Config is frozen (immutable)."""
        config = ManifestMetricsConfig()
        with pytest.raises(AttributeError):
            config.enabled = False  # type: ignore[misc]

    def test_custom_values(self) -> None:
        """Config accepts custom values."""
        config = ManifestMetricsConfig(
            enabled=False,
            slow_source_threshold_ms=500.0,
            max_listener_errors=5,
            log_source_names=False,
        )
        assert config.enabled is False
        assert config.slow_source_threshold_ms == 500.0


class TestSourceEvent:
    def test_source_event_frozen(self) -> None:
        """SourceEvent is frozen."""
        evt = SourceEvent(
            source_type="file_glob",
            source_name="*.py",
            status="ok",
            elapsed_ms=10.0,
            is_slow=False,
        )
        assert evt.source_type == "file_glob"
        with pytest.raises(AttributeError):
            evt.status = "error"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 2: Resolution counters
# ---------------------------------------------------------------------------


class TestResolutionCounters:
    def test_start_increments_active(self) -> None:
        """on_resolution_start increments active_resolutions."""
        observer = ManifestMetricsObserver()
        observer.on_resolution_start()
        snap = observer.snapshot()
        assert snap["active_resolutions"] == 1

    def test_end_decrements_active_and_increments_total(self) -> None:
        """on_resolution_end decrements active and increments total."""
        observer = ManifestMetricsObserver()
        observer.on_resolution_start()
        observer.on_resolution_end(elapsed_ms=50.0, source_count=3)

        snap = observer.snapshot()
        assert snap["active_resolutions"] == 0
        assert snap["total_resolutions"] == 1

    def test_error_counting(self) -> None:
        """Error resolutions are counted separately."""
        observer = ManifestMetricsObserver()
        observer.on_resolution_start()
        observer.on_resolution_end(elapsed_ms=100.0, source_count=2, error=True)

        snap = observer.snapshot()
        assert snap["total_resolutions"] == 1
        assert snap["total_resolution_errors"] == 1

    def test_multiple_resolutions(self) -> None:
        """Multiple resolutions accumulate correctly."""
        observer = ManifestMetricsObserver()
        for i in range(5):
            observer.on_resolution_start()
            observer.on_resolution_end(elapsed_ms=10.0 * i, source_count=1, error=(i == 2))

        snap = observer.snapshot()
        assert snap["total_resolutions"] == 5
        assert snap["total_resolution_errors"] == 1
        assert snap["active_resolutions"] == 0


# ---------------------------------------------------------------------------
# Test 3: Source counters
# ---------------------------------------------------------------------------


class TestSourceCounters:
    def test_per_type_tracking(self) -> None:
        """Source executions are tracked per type."""
        observer = ManifestMetricsObserver()
        observer.on_source_complete("file_glob", "*.py", "ok", 10.0)
        observer.on_source_complete("file_glob", "*.md", "ok", 15.0)
        observer.on_source_complete("memory_query", "auth query", "ok", 20.0)

        snap = observer.snapshot()
        assert snap["total_source_executions"] == 3
        assert snap["by_source_type"]["file_glob"]["executions"] == 2
        assert snap["by_source_type"]["memory_query"]["executions"] == 1

    def test_status_breakdown(self) -> None:
        """Status counts are tracked per type."""
        observer = ManifestMetricsObserver()
        observer.on_source_complete("file_glob", "*.py", "ok", 10.0)
        observer.on_source_complete("file_glob", "*.md", "error", 5.0)
        observer.on_source_complete("file_glob", "*.txt", "ok", 8.0)

        snap = observer.snapshot()
        statuses = snap["by_source_type"]["file_glob"]["statuses"]
        assert statuses["ok"] == 2
        assert statuses["error"] == 1


# ---------------------------------------------------------------------------
# Test 4: Slow source detection
# ---------------------------------------------------------------------------


class TestSlowSourceDetection:
    def test_slow_source_above_threshold(self, caplog: pytest.LogCaptureFixture) -> None:
        """Source above threshold is counted as slow and logged."""
        config = ManifestMetricsConfig(slow_source_threshold_ms=100.0)
        observer = ManifestMetricsObserver(config=config)

        with caplog.at_level(logging.WARNING):
            observer.on_source_complete("file_glob", "*.py", "ok", 200.0)

        snap = observer.snapshot()
        assert snap["slow_sources"] == 1
        assert snap["by_source_type"]["file_glob"]["slow"] == 1
        assert "Slow source" in caplog.text

    def test_fast_source_below_threshold(self, caplog: pytest.LogCaptureFixture) -> None:
        """Source below threshold is not counted as slow."""
        config = ManifestMetricsConfig(slow_source_threshold_ms=100.0)
        observer = ManifestMetricsObserver(config=config)

        with caplog.at_level(logging.WARNING):
            observer.on_source_complete("file_glob", "*.py", "ok", 50.0)

        snap = observer.snapshot()
        assert snap["slow_sources"] == 0
        assert "Slow source" not in caplog.text


# ---------------------------------------------------------------------------
# Test 5: Error/timeout logging
# ---------------------------------------------------------------------------


class TestErrorLogging:
    def test_error_status_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Error status triggers a warning log."""
        observer = ManifestMetricsObserver()

        with caplog.at_level(logging.WARNING):
            observer.on_source_complete("file_glob", "bad.py", "error", 10.0)

        assert "Source error" in caplog.text

    def test_timeout_status_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Timeout status triggers a warning log."""
        observer = ManifestMetricsObserver()

        with caplog.at_level(logging.WARNING):
            observer.on_source_complete("memory_query", "slow q", "timeout", 5000.0)

        assert "Source timeout" in caplog.text


# ---------------------------------------------------------------------------
# Test 6: Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_auto_disable_after_max_errors(self) -> None:
        """Observer auto-disables after max_listener_errors."""
        config = ManifestMetricsConfig(max_listener_errors=3)
        observer = ManifestMetricsObserver(config=config)

        # Force errors by calling _record_error directly
        for _ in range(3):
            observer._record_error()

        snap = observer.snapshot()
        assert snap["disabled"] is True
        assert snap["error_count"] == 3

    def test_disabled_skips_hooks(self) -> None:
        """When disabled, hooks are no-ops."""
        config = ManifestMetricsConfig(max_listener_errors=1)
        observer = ManifestMetricsObserver(config=config)
        observer._record_error()  # trigger disable

        # These should all be no-ops
        observer.on_resolution_start()
        observer.on_source_complete("file_glob", "test", "ok", 10.0)
        observer.on_resolution_end(elapsed_ms=50.0, source_count=1)

        snap = observer.snapshot()
        assert snap["active_resolutions"] == 0
        assert snap["total_resolutions"] == 0
        assert snap["total_source_executions"] == 0


# ---------------------------------------------------------------------------
# Test 7: Snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_returns_complete_dict(self) -> None:
        """Snapshot returns all expected fields."""
        observer = ManifestMetricsObserver()
        observer.on_resolution_start()
        observer.on_source_complete("file_glob", "*.py", "ok", 10.0)
        observer.on_source_complete("memory_query", "q", "ok", 20.0)
        observer.on_resolution_end(elapsed_ms=30.0, source_count=2)

        snap = observer.snapshot()

        assert "total_resolutions" in snap
        assert "total_resolution_errors" in snap
        assert "active_resolutions" in snap
        assert "total_source_executions" in snap
        assert "slow_sources" in snap
        assert "disabled" in snap
        assert "error_count" in snap
        assert "by_source_type" in snap
        assert "file_glob" in snap["by_source_type"]
        assert "memory_query" in snap["by_source_type"]

    def test_by_source_type_structure(self) -> None:
        """by_source_type has executions, statuses, slow for each type."""
        observer = ManifestMetricsObserver()
        observer.on_source_complete("file_glob", "*.py", "ok", 10.0)

        snap = observer.snapshot()
        fg = snap["by_source_type"]["file_glob"]
        assert "executions" in fg
        assert "statuses" in fg
        assert "slow" in fg


# ---------------------------------------------------------------------------
# Test 8: Reset
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_all_counters(self) -> None:
        """Reset clears everything to initial state."""
        observer = ManifestMetricsObserver()
        observer.on_resolution_start()
        observer.on_source_complete("file_glob", "*.py", "ok", 10.0)
        observer.on_resolution_end(elapsed_ms=15.0, source_count=1)

        observer.reset()

        snap = observer.snapshot()
        assert snap["total_resolutions"] == 0
        assert snap["total_source_executions"] == 0
        assert snap["active_resolutions"] == 0
        assert snap["slow_sources"] == 0
        assert snap["by_source_type"] == {}

    def test_reset_re_enables_after_disable(self) -> None:
        """Reset re-enables observer after circuit breaker triggered."""
        config = ManifestMetricsConfig(max_listener_errors=1)
        observer = ManifestMetricsObserver(config=config)
        observer._record_error()
        assert observer.snapshot()["disabled"] is True

        observer.reset()
        assert observer.snapshot()["disabled"] is False
        assert observer.snapshot()["error_count"] == 0


# ---------------------------------------------------------------------------
# Test 9: Disabled config
# ---------------------------------------------------------------------------


class TestDisabledConfig:
    def test_disabled_skips_all_tracking(self) -> None:
        """Config with enabled=False skips all tracking."""
        config = ManifestMetricsConfig(enabled=False)
        observer = ManifestMetricsObserver(config=config)

        observer.on_resolution_start()
        observer.on_source_complete("file_glob", "*.py", "ok", 10.0)
        observer.on_resolution_end(elapsed_ms=10.0, source_count=1)

        snap = observer.snapshot()
        assert snap["total_resolutions"] == 0
        assert snap["total_source_executions"] == 0
        assert snap["active_resolutions"] == 0


# ---------------------------------------------------------------------------
# Test 10: Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_source_tracking(self) -> None:
        """10 threads x 100 calls → total == 1000."""
        observer = ManifestMetricsObserver()
        barrier = threading.Barrier(10)

        def worker() -> None:
            barrier.wait()
            for _ in range(100):
                observer.on_source_complete("file_glob", "*.py", "ok", 1.0)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = observer.snapshot()
        assert snap["total_source_executions"] == 1000


# ---------------------------------------------------------------------------
# Test 11: Integration with resolver
# ---------------------------------------------------------------------------


class StubOkExecutor:
    """Simple stub executor for integration testing."""

    async def execute(self, source: Any, variables: dict[str, str]) -> Any:
        from nexus.services.context_manifest.models import SourceResult

        return SourceResult.ok(
            source_type=source.type,
            source_name=source.source_name,
            data={"test": True},
            elapsed_ms=5.0,
        )


class StubErrorExecutor:
    """Stub executor that returns errors."""

    async def execute(self, source: Any, variables: dict[str, str]) -> Any:
        from nexus.services.context_manifest.models import SourceResult

        return SourceResult.error(
            source_type=source.type,
            source_name=source.source_name,
            error_message="test error",
            elapsed_ms=1.0,
        )


class TestResolverIntegration:
    @pytest.mark.asyncio
    async def test_resolver_calls_metrics_on_success(self, tmp_path: Path) -> None:
        """Resolver calls metrics hooks on successful resolution."""
        from nexus.services.context_manifest.models import FileGlobSource
        from nexus.services.context_manifest.resolver import ManifestResolver

        observer = ManifestMetricsObserver()
        resolver = ManifestResolver(
            executors={"file_glob": StubOkExecutor()},
            metrics_observer=observer,
        )
        sources = [FileGlobSource(pattern="*.py")]

        await resolver.resolve(sources, {}, tmp_path)

        snap = observer.snapshot()
        assert snap["total_resolutions"] == 1
        assert snap["total_resolution_errors"] == 0
        assert snap["total_source_executions"] == 1
        assert snap["active_resolutions"] == 0

    @pytest.mark.asyncio
    async def test_resolver_calls_metrics_on_failure(self, tmp_path: Path) -> None:
        """Resolver calls metrics hooks on failed resolution (required source error)."""
        from nexus.services.context_manifest.models import (
            FileGlobSource,
            ManifestResolutionError,
        )
        from nexus.services.context_manifest.resolver import ManifestResolver

        observer = ManifestMetricsObserver()
        resolver = ManifestResolver(
            executors={"file_glob": StubErrorExecutor()},
            metrics_observer=observer,
        )
        # required=True (default) → ManifestResolutionError
        sources = [FileGlobSource(pattern="*.py")]

        with pytest.raises(ManifestResolutionError):
            await resolver.resolve(sources, {}, tmp_path)

        snap = observer.snapshot()
        assert snap["total_resolutions"] == 1
        assert snap["total_resolution_errors"] == 1
        assert snap["active_resolutions"] == 0
