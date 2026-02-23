"""Integration tests for NatsExporter with Testcontainers.

Requires: testcontainers, nats-py, Docker daemon running.
Skip if dependencies not available or Docker is down.

Issue #1138: Event Stream Export.
"""

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_subsystem.types import FileEvent, FileEventType

# Skip if testcontainers or nats not installed
pytest.importorskip("testcontainers")
nats_mod = pytest.importorskip("nats")


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
def nats_container():
    """Start a NATS testcontainer with JetStream enabled."""
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = DockerContainer("nats:latest").with_command("-js").with_exposed_ports(4222)
    container.start()
    wait_for_logs(container, "Server is ready", timeout=30)
    yield container
    container.stop()


@pytest.fixture
def nats_url(nats_container) -> str:
    host = nats_container.get_container_host_ip()
    port = nats_container.get_exposed_port(4222)
    return f"nats://{host}:{port}"


@pytest.fixture
def exporter(nats_url: str):
    from nexus.services.event_subsystem.log.exporters.config import NatsExporterConfig
    from nexus.services.event_subsystem.log.exporters.nats_exporter import NatsExporter

    config = NatsExporterConfig(
        servers=nats_url,
        subject_prefix="test.export",
        stream_name="TEST_EXPORT",
    )
    return NatsExporter(config)


def _make_event(event_id: str = "test-1", zone_id: str = ROOT_ZONE_ID) -> FileEvent:
    return FileEvent(
        type=FileEventType.FILE_WRITE,
        path="/test.txt",
        zone_id=zone_id,
        event_id=event_id,
    )


@pytest.mark.asyncio
class TestNatsExporter:
    async def test_publish_single_event(self, exporter) -> None:
        event = _make_event()
        await exporter.publish(event)
        await exporter.close()

    async def test_publish_batch(self, exporter) -> None:
        events = [_make_event(f"evt-{i}") for i in range(10)]
        failed = await exporter.publish_batch(events)
        assert failed == []
        await exporter.close()

    async def test_deduplication(self, exporter) -> None:
        """Publishing same event_id twice should not create duplicates."""
        event = _make_event("dedup-test")
        await exporter.publish(event)
        await exporter.publish(event)  # Should be deduplicated by NATS
        await exporter.close()

    async def test_health_check(self, exporter) -> None:
        healthy = await exporter.health_check()
        assert healthy is True
        await exporter.close()
