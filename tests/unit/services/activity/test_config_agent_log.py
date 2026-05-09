import pytest

from nexus.services.activity.config import ActivityConfig


def test_defaults_when_unset(monkeypatch):
    for k in (
        "NEXUS_ACTIVITY_AGENT_LOG_ENABLED",
        "NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES",
        "NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS",
        "NEXUS_ACTIVITY_AGENT_LOG_CMD_MAX_BYTES",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = ActivityConfig.from_env()
    assert cfg.agent_log_enabled is True
    assert cfg.agent_log_cap_bytes == 10 * 1024 * 1024
    assert cfg.agent_log_retention_days == 7
    assert cfg.agent_log_cmd_max_bytes == 4 * 1024


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "0")
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES", "1048576")
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS", "3")
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_CMD_MAX_BYTES", "256")
    cfg = ActivityConfig.from_env()
    assert cfg.agent_log_enabled is False
    assert cfg.agent_log_cap_bytes == 1_048_576
    assert cfg.agent_log_retention_days == 3
    assert cfg.agent_log_cmd_max_bytes == 256


def test_invalid_cap_bytes_rejected(monkeypatch):
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES", "0")
    with pytest.raises(ValueError):
        ActivityConfig.from_env()
