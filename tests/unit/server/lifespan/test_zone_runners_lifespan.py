from types import SimpleNamespace

import pytest

from nexus.server.lifespan.services_container import LifespanServices
from nexus.server.lifespan.zone_runners import shutdown_zone_runners


class RecordingRegistry:
    def __init__(self) -> None:
        self.stopped = False

    def stop_all(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_shutdown_zone_runners_stops_registry_from_app_state() -> None:
    registry = RecordingRegistry()
    app = SimpleNamespace(state=SimpleNamespace(zone_registry=registry))
    svc = LifespanServices(zone_registry=registry)

    await shutdown_zone_runners(app, svc)

    assert registry.stopped is True


def test_lifespan_services_extracts_zone_registry() -> None:
    registry = RecordingRegistry()
    app = SimpleNamespace(state=SimpleNamespace(nexus_fs=None, zone_registry=registry))

    svc = LifespanServices.from_app(app)

    assert svc.zone_registry is registry
