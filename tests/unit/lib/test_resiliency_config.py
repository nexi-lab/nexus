"""Tests for ResiliencyConfig.from_dict() — Issue #2180."""

from nexus.lib.resiliency import (
    CircuitBreakerPolicy,
    ResiliencyConfig,
    TargetBinding,
    TimeoutPolicy,
)


class TestResiliencyConfigFromDict:
    """ResiliencyConfig.from_dict() classmethod tests."""

    def test_none_returns_defaults(self) -> None:
        cfg = ResiliencyConfig.from_dict(None)
        assert cfg == ResiliencyConfig()
        assert "default" in cfg.timeouts
        assert "default" in cfg.retries
        assert "default" in cfg.circuit_breakers

    def test_empty_dict_returns_defaults(self) -> None:
        cfg = ResiliencyConfig.from_dict({})
        assert cfg == ResiliencyConfig()

    def test_valid_config(self) -> None:
        raw = {
            "timeouts": {"gcs": {"seconds": "10s"}, "fast": 2.0},
            "retries": {"gcs": {"max_retries": 5, "max_interval": 20.0}},
            "circuit_breakers": {
                "gcs": {"failure_threshold": 10, "success_threshold": 5, "timeout": "1m"}
            },
            "targets": {"gcs": {"timeout": "gcs", "retry": "gcs", "circuit_breaker": "gcs"}},
        }
        cfg = ResiliencyConfig.from_dict(raw)

        assert cfg.timeouts["gcs"] == TimeoutPolicy(seconds=10.0)
        assert cfg.timeouts["fast"] == TimeoutPolicy(seconds=2.0)
        assert cfg.retries["gcs"].max_retries == 5
        assert cfg.circuit_breakers["gcs"] == CircuitBreakerPolicy(
            failure_threshold=10, success_threshold=5, timeout=60.0
        )
        assert cfg.targets["gcs"] == TargetBinding(
            timeout="gcs", retry="gcs", circuit_breaker="gcs"
        )

    def test_partial_config_retains_defaults(self) -> None:
        raw = {"timeouts": {"fast": 1.0}}
        cfg = ResiliencyConfig.from_dict(raw)
        assert "default" in cfg.timeouts
        assert cfg.timeouts["fast"] == TimeoutPolicy(seconds=1.0)
        assert "default" in cfg.retries
        assert "default" in cfg.circuit_breakers

    def test_malformed_falls_back(self) -> None:
        raw: dict[str, object] = {"timeouts": "not_a_dict"}
        cfg = ResiliencyConfig.from_dict(raw)
        assert cfg == ResiliencyConfig()

    def test_duration_units(self) -> None:
        raw = {"timeouts": {"sec": "5s", "min": "2m", "hour": "1h"}}
        cfg = ResiliencyConfig.from_dict(raw)
        assert cfg.timeouts["sec"].seconds == 5.0
        assert cfg.timeouts["min"].seconds == 120.0
        assert cfg.timeouts["hour"].seconds == 3600.0

    def test_retry_policy_defaults(self) -> None:
        raw = {"retries": {"custom": {"max_retries": 10}}}
        cfg = ResiliencyConfig.from_dict(raw)
        rp = cfg.retries["custom"]
        assert rp.max_retries == 10
        assert rp.max_interval == 10.0  # default
        assert rp.multiplier == 2.0  # default
