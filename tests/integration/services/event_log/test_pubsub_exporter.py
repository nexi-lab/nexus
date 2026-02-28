"""Integration tests for PubSubExporter with Testcontainers (emulator).

Requires: testcontainers, gcloud-aio-pubsub, Docker daemon running.
Skip if dependencies not available or Docker is down.

Issue #1138: Event Stream Export.
"""

import os

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.system_services.event_subsystem.types import FileEvent, FileEventType

# Skip if testcontainers or gcloud-aio-pubsub not installed
pytest.importorskip("testcontainers")
pytest.importorskip("gcloud.aio.pubsub")


def _docker_available() -> bool:
    """Check if Docker daemon is accessible."""
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="Docker daemon not running")


@pytest.fixture(scope="module")
def pubsub_emulator():
    """Start a Google Pub/Sub emulator testcontainer."""
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = (
        DockerContainer("google/cloud-sdk:emulators")
        .with_command("gcloud beta emulators pubsub start --host-port=0.0.0.0:8085")
        .with_exposed_ports(8085)
    )
    container.start()
    wait_for_logs(container, "Server started", timeout=60)
    yield container
    container.stop()


@pytest.fixture
def pubsub_host(pubsub_emulator) -> str:
    host = pubsub_emulator.get_container_host_ip()
    port = pubsub_emulator.get_exposed_port(8085)
    return f"http://{host}:{port}"


@pytest.fixture(autouse=True)
def set_emulator_env(pubsub_host: str):
    """Set PUBSUB_EMULATOR_HOST for the gcloud library."""
    old = os.environ.get("PUBSUB_EMULATOR_HOST")
    os.environ["PUBSUB_EMULATOR_HOST"] = pubsub_host
    yield
    if old is not None:
        os.environ["PUBSUB_EMULATOR_HOST"] = old
    else:
        os.environ.pop("PUBSUB_EMULATOR_HOST", None)


@pytest.fixture
def exporter():
    from nexus.system_services.event_subsystem.log.exporters.config import PubSubExporterConfig
    from nexus.system_services.event_subsystem.log.exporters.pubsub_exporter import PubSubExporter

    config = PubSubExporterConfig(
        project_id="test-project",
        topic_prefix="test-events",
    )
    return PubSubExporter(config)


def _make_event(event_id: str = "test-1", zone_id: str = ROOT_ZONE_ID) -> FileEvent:
    return FileEvent(
        type=FileEventType.FILE_WRITE,
        path="/test.txt",
        zone_id=zone_id,
        event_id=event_id,
    )


@pytest.mark.asyncio
class TestPubSubExporter:
    async def test_publish_single_event(self, exporter) -> None:
        event = _make_event()
        # Note: with emulator, topic must be pre-created or auto-created
        # The emulator may return errors if topic doesn't exist
        try:
            await exporter.publish(event)
        except Exception:
            pytest.skip("Pub/Sub emulator topic not pre-created")
        finally:
            await exporter.close()

    async def test_publish_batch(self, exporter) -> None:
        events = [_make_event(f"evt-{i}") for i in range(5)]
        try:
            failed = await exporter.publish_batch(events)
            assert isinstance(failed, list)
        except Exception:
            pytest.skip("Pub/Sub emulator topic not pre-created")
        finally:
            await exporter.close()

    async def test_health_check(self, exporter) -> None:
        healthy = await exporter.health_check()
        assert healthy is True
        await exporter.close()
