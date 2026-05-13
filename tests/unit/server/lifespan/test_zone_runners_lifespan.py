import threading
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


@pytest.mark.asyncio
async def test_shutdown_zone_runners_offloads_stop_all_from_event_loop_thread() -> None:
    event_loop_thread_id = threading.get_ident()
    stop_all_thread_id: int | None = None

    class ThreadRecordingRegistry:
        def stop_all(self) -> None:
            nonlocal stop_all_thread_id
            stop_all_thread_id = threading.get_ident()

    registry = ThreadRecordingRegistry()
    app = SimpleNamespace(state=SimpleNamespace(zone_registry=registry))
    svc = LifespanServices(zone_registry=registry)

    await shutdown_zone_runners(app, svc)

    assert stop_all_thread_id is not None
    assert stop_all_thread_id != event_loop_thread_id


@pytest.mark.asyncio
async def test_shutdown_zone_runners_uses_service_registry_fallback() -> None:
    registry = RecordingRegistry()
    app = SimpleNamespace(state=SimpleNamespace(zone_registry=None))
    svc = LifespanServices(zone_registry=registry)

    await shutdown_zone_runners(app, svc)

    assert registry.stopped is True
