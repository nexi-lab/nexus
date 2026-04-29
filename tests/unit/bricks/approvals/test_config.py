import pytest

from nexus.bricks.approvals.config import ApprovalConfig


def test_defaults_match_spec():
    cfg = ApprovalConfig()
    assert cfg.enabled is False
    assert cfg.auto_deny_after_seconds == 60.0
    assert cfg.auto_deny_max_seconds == 600.0
    assert cfg.sweeper_interval_seconds == 5.0
    assert cfg.watch_buffer_size == 256
    assert cfg.diag_dump_history_limit == 100


def test_clamp_request_timeout_to_max():
    cfg = ApprovalConfig(auto_deny_after_seconds=60.0, auto_deny_max_seconds=600.0)
    assert cfg.clamp_request_timeout(None) == 60.0
    assert cfg.clamp_request_timeout(10.0) == 10.0
    assert cfg.clamp_request_timeout(9999.0) == 600.0


def test_clamp_rejects_non_positive():
    cfg = ApprovalConfig()
    with pytest.raises(ValueError):
        cfg.clamp_request_timeout(0.0)
    with pytest.raises(ValueError):
        cfg.clamp_request_timeout(-3.0)
