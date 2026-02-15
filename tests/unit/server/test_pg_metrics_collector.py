"""Unit tests for QueryObserverCollector Prometheus bridge (Issue #762).

Validates that the custom collector correctly translates QueryObserver
counters into Prometheus GaugeMetricFamily values.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nexus.server.pg_metrics_collector import QueryObserverCollector

# Expected metric family names emitted by the collector
_EXPECTED_FAMILIES = {
    "nexus_db_queries_total",
    "nexus_db_slow_queries_total",
    "nexus_db_observer_errors_total",
    "nexus_db_observer_disabled",
    "nexus_db_pool_checkouts_total",
    "nexus_db_pool_checkins_total",
    "nexus_db_pool_connects_total",
    "nexus_db_pool_invalidations_total",
}


def _make_observer(**overrides: int | bool) -> MagicMock:
    """Build a mock QueryObserver with sensible defaults."""
    defaults = {
        "total_queries": 0,
        "slow_queries": 0,
        "error_count": 0,
        "disabled": False,
        "pool_checkouts": 0,
        "pool_checkins": 0,
        "pool_connects": 0,
        "pool_invalidations": 0,
    }
    defaults.update(overrides)
    mock = MagicMock()
    for attr, val in defaults.items():
        setattr(mock, attr, val)
    return mock


class TestQueryObserverCollector:
    """Tests for the custom Prometheus collector."""

    def test_collector_yields_expected_metric_families(self) -> None:
        collector = QueryObserverCollector(_make_observer())
        families = list(collector.collect())
        names = {f.name for f in families}
        assert names == _EXPECTED_FAMILIES

    def test_collector_reads_observer_values(self) -> None:
        observer = _make_observer(
            total_queries=42,
            slow_queries=3,
            error_count=1,
            disabled=False,
            pool_checkouts=100,
            pool_checkins=98,
            pool_connects=5,
            pool_invalidations=2,
        )
        collector = QueryObserverCollector(observer)
        families = {f.name: f for f in collector.collect()}

        assert families["nexus_db_queries_total"].samples[0].value == 42
        assert families["nexus_db_slow_queries_total"].samples[0].value == 3
        assert families["nexus_db_observer_errors_total"].samples[0].value == 1
        assert families["nexus_db_observer_disabled"].samples[0].value == 0
        assert families["nexus_db_pool_checkouts_total"].samples[0].value == 100
        assert families["nexus_db_pool_checkins_total"].samples[0].value == 98
        assert families["nexus_db_pool_connects_total"].samples[0].value == 5
        assert families["nexus_db_pool_invalidations_total"].samples[0].value == 2

    def test_collector_handles_disabled_observer(self) -> None:
        observer = _make_observer(disabled=True)
        collector = QueryObserverCollector(observer)
        families = {f.name: f for f in collector.collect()}
        assert families["nexus_db_observer_disabled"].samples[0].value == 1

    def test_collector_with_zero_counters(self) -> None:
        collector = QueryObserverCollector(_make_observer())
        families = list(collector.collect())
        for family in families:
            assert family.samples[0].value == 0

    def test_describe_returns_empty(self) -> None:
        collector = QueryObserverCollector(_make_observer())
        assert list(collector.describe()) == []
