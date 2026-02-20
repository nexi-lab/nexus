"""Lifecycle adapter for the search brick (Issue #2036).

Wraps ``SearchDaemon`` to satisfy ``BrickLifecycleProtocol`` so the search
brick can be registered with the ``BrickLifecycleManager`` alongside other
lifecycle-aware bricks.

The adapter maps:
    - ``start()``        → ``SearchDaemon.startup()``
    - ``stop()``         → ``SearchDaemon.shutdown()``
    - ``health_check()`` → ``SearchDaemon.get_health()["initialized"]``

Zoekt lifecycle is an internal detail of the daemon and is NOT exposed
to the lifecycle manager (Decision #3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.search.daemon import SearchDaemon


class SearchBrickLifecycleAdapter:
    """Adapts SearchDaemon to BrickLifecycleProtocol."""

    def __init__(self, daemon: SearchDaemon) -> None:
        self._daemon: Any = daemon

    async def start(self) -> None:
        """Initialize the search daemon."""
        await self._daemon.startup()

    async def stop(self) -> None:
        """Gracefully shut down the search daemon."""
        await self._daemon.shutdown()

    async def health_check(self) -> bool:
        """Return True if the search daemon is healthy."""
        health: dict[str, Any] = self._daemon.get_health()
        return bool(health.get("initialized", False))
