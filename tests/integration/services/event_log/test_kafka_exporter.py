"""Integration tests for KafkaExporter with Testcontainers.

Requires: testcontainers, aiokafka, Docker daemon running.
Skip if dependencies not available or Docker is down.

Issue #1138: Event Stream Export.
"""

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_subsystem.types import FileEvent, FileEventType

# Skip if testcontainers or aiokafka not installed
pytest.importorskip("testcontainers")
aiokafka = pytest.importorskip("aiokafka")


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
def kafka_container():
    """Start a Kafka testcontainer for the module."""
    from testcontainers.kafka import KafkaContainer

    with KafkaContainer("confluentinc/cp-kafka:7.6.0") as kafka:
        yield kafka


@pytest.fixture
def kafka_bootstrap(kafka_container) -> str:
    return kafka_container.get_bootstrap_server()


@pytest.fixture
def exporter(kafka_bootstrap: str):
    from nexus.services.event_subsystem.log.exporters.config import KafkaExporterConfig
    from nexus.services.event_subsystem.log.exporters.kafka_exporter import KafkaExporter

    config = KafkaExporterConfig(
        bootstrap_servers=kafka_bootstrap,
        topic_prefix="test.events",
        acks="all",
        compression="none",  # Simpler for tests
    )
    return KafkaExporter(config)


def _make_event(event_id: str = "test-1", zone_id: str = ROOT_ZONE_ID) -> FileEvent:
    return FileEvent(
        type=FileEventType.FILE_WRITE,
        path="/test.txt",
        zone_id=zone_id,
        event_id=event_id,
    )


@pytest.mark.asyncio
class TestKafkaExporter:
    async def test_publish_single_event(self, exporter) -> None:
        event = _make_event()
        await exporter.publish(event)
        # If no exception, publish succeeded
        await exporter.close()

    async def test_publish_batch(self, exporter) -> None:
        events = [_make_event(f"evt-{i}") for i in range(10)]
        failed = await exporter.publish_batch(events)
        assert failed == []
        await exporter.close()

    async def test_health_check(self, exporter) -> None:
        healthy = await exporter.health_check()
        assert healthy is True
        await exporter.close()

    async def test_connection_failure_handling(self) -> None:
        from nexus.services.event_subsystem.log.exporters.config import KafkaExporterConfig
        from nexus.services.event_subsystem.log.exporters.kafka_exporter import KafkaExporter

        config = KafkaExporterConfig(bootstrap_servers="localhost:19999")
        bad_exporter = KafkaExporter(config)

        healthy = await bad_exporter.health_check()
        assert healthy is False
