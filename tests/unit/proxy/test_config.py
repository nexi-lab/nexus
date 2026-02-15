"""Unit tests for ProxyBrickConfig."""

from __future__ import annotations

import pytest

from nexus.proxy.config import ProxyBrickConfig


class TestProxyBrickConfig:
    def test_frozen_immutable(self) -> None:
        cfg = ProxyBrickConfig(remote_url="http://localhost:2026")
        with pytest.raises(AttributeError):
            cfg.remote_url = "http://other"  # type: ignore[misc]

    def test_sensible_defaults(self) -> None:
        cfg = ProxyBrickConfig(remote_url="http://localhost:2026")
        assert cfg.remote_url == "http://localhost:2026"
        assert cfg.api_key is None
        assert cfg.cb_failure_threshold == 5
        assert cfg.cb_recovery_timeout == 30.0
        assert cfg.retry_max_attempts == 3
        assert cfg.connect_timeout == 5.0
        assert cfg.request_timeout == 30.0
        assert cfg.max_connections == 10
        assert cfg.http2 is True
        assert cfg.replay_batch_size == 50
        assert cfg.max_retry_count == 10
        assert cfg.stream_threshold_bytes == 65_536

    def test_custom_values(self) -> None:
        cfg = ProxyBrickConfig(
            remote_url="https://cloud.example.com",
            api_key="secret",
            cb_failure_threshold=10,
            retry_max_attempts=5,
        )
        assert cfg.api_key == "secret"
        assert cfg.cb_failure_threshold == 10
        assert cfg.retry_max_attempts == 5
