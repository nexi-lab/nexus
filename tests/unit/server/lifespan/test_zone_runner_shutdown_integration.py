from types import SimpleNamespace

import pytest

from nexus.runtime.zone_runner import ZoneRegistry
from nexus.server.lifespan.services_container import LifespanServices
from nexus.server.lifespan.zone_runners import shutdown_zone_runners


@pytest.mark.asyncio
async def test_lifespan_shutdown_stops_started_zone_threads() -> None:
    registry = ZoneRegistry()
    runner_a = registry.runner_for("zone-a")
    runner_b = registry.runner_for("zone-b")
    runner_a.start()
    runner_b.start()
    app = SimpleNamespace(state=SimpleNamespace(zone_registry=registry))
    svc = LifespanServices(zone_registry=registry)

    await shutdown_zone_runners(app, svc)

    assert not runner_a.is_alive
    assert not runner_b.is_alive
