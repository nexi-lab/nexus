"""Tests for ProxyBrickConfig defaults and policy profiles.

Policy profile tests (TDD RED) are written for the not-yet-existing
.local(), .production(), .edge() class methods (Issue #2073).
"""

import pytest

from nexus.proxy.config import ProxyBrickConfig


class TestProxyBrickConfigDefaults:
    """Verify documented default values match implementation."""

    def test_circuit_breaker_defaults(self) -> None:
        cfg = ProxyBrickConfig(remote_url="http://test:8000")
        assert cfg.cb_failure_threshold == 5
        assert cfg.cb_recovery_timeout == 30.0
        assert cfg.cb_half_open_max_calls == 1

    def test_retry_defaults(self) -> None:
        cfg = ProxyBrickConfig(remote_url="http://test:8000")
        assert cfg.retry_max_attempts == 3
        assert cfg.retry_initial_wait == 0.5
        assert cfg.retry_max_wait == 30.0

    def test_transport_defaults(self) -> None:
        cfg = ProxyBrickConfig(remote_url="http://test:8000")
        assert cfg.connect_timeout == 5.0
        assert cfg.request_timeout == 30.0
        assert cfg.max_connections == 10
        assert cfg.max_keepalive == 5
        assert cfg.http2 is True

    def test_replay_defaults(self) -> None:
        cfg = ProxyBrickConfig(remote_url="http://test:8000")
        assert cfg.replay_batch_size == 50
        assert cfg.replay_poll_interval == 5.0
        assert cfg.max_retry_count == 10

    def test_streaming_defaults(self) -> None:
        cfg = ProxyBrickConfig(remote_url="http://test:8000")
        assert cfg.stream_threshold_bytes == 65_536

    def test_frozen(self) -> None:
        cfg = ProxyBrickConfig(remote_url="http://test:8000")
        with pytest.raises(AttributeError):
            cfg.remote_url = "http://other:8000"  # type: ignore[misc]


class TestProxyBrickConfigLocalProfile:
    """TDD RED: .local() profile — low-latency LAN."""

    def test_local_creates_config(self) -> None:
        cfg = ProxyBrickConfig.local("http://lan:8000")
        assert cfg.remote_url == "http://lan:8000"

    def test_local_has_tight_timeouts(self) -> None:
        cfg = ProxyBrickConfig.local("http://lan:8000")
        assert cfg.connect_timeout <= 2.0
        assert cfg.request_timeout <= 10.0

    def test_local_overrides_work(self) -> None:
        cfg = ProxyBrickConfig.local("http://lan:8000", max_connections=20)
        assert cfg.max_connections == 20


class TestProxyBrickConfigProductionProfile:
    """TDD RED: .production() profile — internet, conservative."""

    def test_production_creates_config(self) -> None:
        cfg = ProxyBrickConfig.production("http://cloud:8000")
        assert cfg.remote_url == "http://cloud:8000"

    def test_production_uses_standard_defaults(self) -> None:
        cfg = ProxyBrickConfig.production("http://cloud:8000")
        # Production should use the default values (they were tuned for this)
        assert cfg.cb_failure_threshold == 5
        assert cfg.retry_max_attempts == 3


class TestProxyBrickConfigEdgeProfile:
    """TDD RED: .edge() profile — intermittent connectivity."""

    def test_edge_creates_config(self) -> None:
        cfg = ProxyBrickConfig.edge("http://cloud:8000")
        assert cfg.remote_url == "http://cloud:8000"

    def test_edge_has_longer_recovery(self) -> None:
        cfg = ProxyBrickConfig.edge("http://cloud:8000")
        assert cfg.cb_recovery_timeout >= 60.0

    def test_edge_has_more_retries(self) -> None:
        cfg = ProxyBrickConfig.edge("http://cloud:8000")
        assert cfg.retry_max_attempts >= 5

    def test_edge_has_larger_queue_retries(self) -> None:
        cfg = ProxyBrickConfig.edge("http://cloud:8000")
        assert cfg.max_retry_count >= 20

    def test_edge_overrides_work(self) -> None:
        cfg = ProxyBrickConfig.edge("http://cloud:8000", cb_recovery_timeout=300.0)
        assert cfg.cb_recovery_timeout == 300.0
