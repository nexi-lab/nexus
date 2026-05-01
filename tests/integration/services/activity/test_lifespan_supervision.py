"""Integration: activity component is wired as optional and tolerates failures."""

from __future__ import annotations

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

    setup_activity()
    shutdown_activity()
