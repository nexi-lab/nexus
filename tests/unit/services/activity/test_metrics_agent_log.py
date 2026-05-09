"""Unit tests for agent_log metrics."""

from __future__ import annotations

from nexus.services.activity import metrics


def test_agent_log_lines_dropped_counter_present():
    assert metrics.AGENT_LOG_LINES_DROPPED is not None
    assert "reason" in metrics.AGENT_LOG_LINES_DROPPED._labelnames


def test_agent_log_bytes_gauge_present():
    assert metrics.AGENT_LOG_BYTES is not None
    assert "agent_id" in metrics.AGENT_LOG_BYTES._labelnames
