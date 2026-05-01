"""Integration: activity metrics are exposed via the global Prom REGISTRY after emits."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from prometheus_client import generate_latest

from nexus.services.activity import EventKind, Result, emit
from nexus.services.activity.lifespan import setup_activity, shutdown_activity


@pytest.mark.asyncio
async def test_metrics_exposed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "activity.db"
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(db))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "0")

    setup_activity()
    try:
        emit(
            kind=EventKind.SEARCH,
            result=Result.OK,
            actor_token_hash="t",
            subject_zone="eng",
            latency_ms=5,
        )
        emit(kind=EventKind.MCP_TOOL_CALL, result=Result.OK, subject_extra={"tool": "search"})
        emit(kind=EventKind.ZONE_ACCESS, result=Result.BLOCKED, subject_zone="legal")
        emit(kind=EventKind.APPROVAL, result=Result.PENDING_APPROVAL)
        await asyncio.sleep(0.2)
    finally:
        shutdown_activity()
        await asyncio.sleep(0.1)

    body = generate_latest().decode()
    assert "nexus_search_requests_total" in body
    assert "nexus_search_latency_seconds" in body
    assert "nexus_mcp_tool_calls_total" in body
    assert "nexus_policy_blocks_total" in body
    assert "nexus_approvals_pending" in body
    assert "nexus_activity_drops_total" in body
