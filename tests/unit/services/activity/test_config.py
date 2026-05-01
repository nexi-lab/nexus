"""Unit tests for ActivityConfig env parsing."""

from __future__ import annotations

import pytest

from nexus.services.activity.config import ActivityConfig


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "NEXUS_ACTIVITY_ENABLED",
        "NEXUS_ACTIVITY_DB_PATH",
        "NEXUS_ACTIVITY_RETENTION_DAYS",
        "NEXUS_ACTIVITY_QUEUE_SIZE",
        "NEXUS_ACTIVITY_BATCH_SIZE",
        "NEXUS_ACTIVITY_BATCH_TIMEOUT_S",
        "NEXUS_DATA_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = ActivityConfig.from_env()
    assert cfg.enabled is True
    assert cfg.retention_days == 30
    assert cfg.queue_size == 10000
    assert cfg.batch_size == 200
    assert cfg.batch_timeout_s == 0.5
    assert cfg.db_path.name == "activity.db"


def test_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "0")
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", "/tmp/activity-test.db")
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "7")
    monkeypatch.setenv("NEXUS_ACTIVITY_QUEUE_SIZE", "100")
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_SIZE", "5")
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_TIMEOUT_S", "0.1")
    cfg = ActivityConfig.from_env()
    assert cfg.enabled is False
    assert str(cfg.db_path) == "/tmp/activity-test.db"
    assert cfg.retention_days == 7
    assert cfg.queue_size == 100
    assert cfg.batch_size == 5
    assert cfg.batch_timeout_s == 0.1


def test_invalid_int_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_QUEUE_SIZE", "not-a-number")
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_QUEUE_SIZE"):
        ActivityConfig.from_env()


def test_negative_retention_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "0")
    cfg = ActivityConfig.from_env()
    assert cfg.retention_days == 0


def test_zero_queue_size_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-positive queue_size makes asyncio.Queue unbounded — that breaks
    the bounded-telemetry contract and prevents drop accounting."""
    monkeypatch.setenv("NEXUS_ACTIVITY_QUEUE_SIZE", "0")
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_QUEUE_SIZE"):
        ActivityConfig.from_env()


def test_negative_queue_size_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_QUEUE_SIZE", "-1")
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_QUEUE_SIZE"):
        ActivityConfig.from_env()


def test_zero_batch_size_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_SIZE", "0")
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_BATCH_SIZE"):
        ActivityConfig.from_env()


def test_zero_batch_timeout_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_TIMEOUT_S", "0")
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_BATCH_TIMEOUT_S"):
        ActivityConfig.from_env()


def test_negative_retention_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """retention_days=0 disables prune (valid); negative is invalid."""
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "-1")
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_RETENTION_DAYS"):
        ActivityConfig.from_env()


@pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf"])
def test_non_finite_batch_timeout_rejected(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    """NaN passes <=0 (NaN comparisons are False); inf would prevent partial-
    batch flushes. Both must be rejected at config-parse time."""
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_TIMEOUT_S", bad_value)
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_BATCH_TIMEOUT_S"):
        ActivityConfig.from_env()
