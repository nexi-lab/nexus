"""``startup_search`` must fail SOFT when the nexus-search-plugin
sidecar isn't reachable.

P12 turned ``SearchDaemon`` into a Python-side proxy for the Rust
``nexus-search-plugin`` cdylib.  Every method dials
``NEXUS_SEARCH_PLUGIN_TARGET`` (default ``127.0.0.1:2126``).  A
deployment topology that boots the FastAPI server without also
loading the plugin (single-container edge smoke, dev workstation,
anything that hasn't caught up to the new sidecar contract) used to
get an in-process daemon and now would leave an unusable proxy in
``app.state`` — every subsequent request handler would UNAVAILABLE.

The fix: ``startup_search`` runs a bounded ``get_health()`` probe
before publishing the daemon.  On failure it logs a warning, tears
the candidate down, and sets ``app.state.search_daemon = None`` so
``_get_search_daemon`` returns 503 cleanly and no code path blocks
on unreachable gRPC.
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
async def test_startup_publishes_daemon_when_plugin_healthy(monkeypatch):
    """Healthy probe ⇒ daemon published + wired into search service."""
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "true")
    monkeypatch.setenv("NEXUS_SEARCH_PLUGIN_TARGET", "127.0.0.1:2126")

    fake = AsyncMock()
    fake.startup.return_value = None
    fake.get_health.return_value = {"status": "healthy", "detail": ""}

    class _SearchSvcHolder:
        def __init__(self) -> None:
            self._search_daemon = None

    holder = _SearchSvcHolder()

    app, svc = _FakeApp(), _FakeSvc()
    svc.nexus_fs = SimpleNamespace(service=lambda name: holder if name == "search" else None)

    # _wire_notify_hooks + _init_zone_registry short-circuit on the
    # SimpleNamespace fake (no register_intercept_write / zone_manager).
    with patch("nexus.bricks.search.daemon.SearchDaemon", return_value=fake):
        tasks = await startup_search(app, svc)

    assert tasks == []
    assert app.state.search_daemon is fake
    assert app.state.search_daemon_enabled is True
    assert holder._search_daemon is fake, "SearchService got wired to the daemon"


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
