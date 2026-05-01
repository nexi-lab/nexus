"""Integration: activity component is wired as optional and tolerates failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.server.lifespan.observability import create_registry


def test_activity_registered_as_optional() -> None:
    registry = create_registry()
    names = [n for n, *_ in registry._components]  # noqa: SLF001
    assert "activity" in names
    activity_entry = next(t for t in registry._components if t[0] == "activity")  # noqa: SLF001
    assert activity_entry[2] is False  # optional


@pytest.mark.asyncio
async def test_activity_failure_does_not_abort_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force SQLiteSink to fail by passing a forbidden path; lifespan must
    fall back to NoopSink and setup_activity returns without raising."""
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", "/nonexistent/forbidden/path/activity.db")
    from nexus.services.activity.lifespan import setup_activity, shutdown_activity

    await setup_activity()
    await shutdown_activity()


@pytest.mark.asyncio
async def test_registry_path_installs_queue_emitter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server-startup path: registry.start_all() must actually install the
    QueueEmitter (not silently discard the setup coroutine), and shutdown_all()
    must restore NoopEmitter after draining the worker. Catches the regression
    where _make_start/_make_stop wrappers swallowed async return values."""
    from nexus.contracts.protocols.activity import NoopEmitter, get_emitter
    from nexus.services.activity.emitter import QueueEmitter

    db = tmp_path / "registry-activity.db"
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(db))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "0")

    registry = create_registry()
    await registry.start_all()
    try:
        assert isinstance(get_emitter(), QueueEmitter)
    finally:
        await registry.shutdown_all()
    assert isinstance(get_emitter(), NoopEmitter)
