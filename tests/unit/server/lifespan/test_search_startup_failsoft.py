"""Search management is published only after a healthy plugin probe.

A failed probe closes the client and leaves management disabled so routes
return 503 without starting further RPCs against an unreachable host.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nexus.server.lifespan.search import startup_search


class _FakeApp:
    """Minimal FastAPI stand-in — ``startup_search`` only touches
    ``app.state`` (SimpleNamespace so attribute assignment works)."""

    def __init__(self) -> None:
        self.state = SimpleNamespace()


class _FakeSvc:
    def __init__(self) -> None:
        self.database_url = "postgresql://x"
        self.nexus_fs = SimpleNamespace(service=lambda _: None)
        self.zone_manager = None


@pytest.mark.asyncio
async def test_startup_failsoft_when_plugin_unreachable(monkeypatch):
    """Plugin health probe raises ⇒ app.state.search_daemon = None."""
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "true")
    monkeypatch.setenv("NEXUS_SEARCH_PLUGIN_TARGET", "127.0.0.1:2126")

    fake = AsyncMock()
    fake.startup.return_value = None
    fake.get_health.side_effect = ConnectionRefusedError("Connection refused")
    fake.shutdown.return_value = None

    app, svc = _FakeApp(), _FakeSvc()
    with patch("nexus.bricks.search.daemon.SearchDaemon", return_value=fake):
        tasks = await startup_search(app, svc)

    assert tasks == []
    assert app.state.search_daemon is None, (
        "unreachable plugin must not leave a broken proxy on app.state"
    )
    assert app.state.search_daemon_enabled is False
    fake.shutdown.assert_awaited()


@pytest.mark.asyncio
async def test_startup_publishes_daemon_when_plugin_healthy(monkeypatch, tmp_path):
    """Healthy probe ⇒ HTTP search transport published."""
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "true")
    monkeypatch.setenv("NEXUS_SEARCH_PLUGIN_TARGET", "127.0.0.1:2126")

    fake = AsyncMock()
    fake.startup.return_value = None
    fake.get_health.return_value = {"status": "healthy", "detail": ""}

    app, svc = _FakeApp(), _FakeSvc()
    svc.nexus_fs = SimpleNamespace()

    svc.zone_manager = SimpleNamespace(_base_path=str(tmp_path), list_zones=lambda: ["local"])
    with patch("nexus.bricks.search.daemon.SearchDaemon", return_value=fake):
        tasks = await startup_search(app, svc)

    assert tasks == []
    assert app.state.search_daemon is fake
    assert app.state.search_daemon_enabled is True
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_startup_skipped_when_explicit_off(monkeypatch):
    """NEXUS_SEARCH_DAEMON=false ⇒ never construct."""
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")

    app, svc = _FakeApp(), _FakeSvc()
    with patch("nexus.bricks.search.daemon.SearchDaemon") as ctor:
        tasks = await startup_search(app, svc)

    assert tasks == []
    ctor.assert_not_called()
    assert not hasattr(app.state, "search_daemon")
