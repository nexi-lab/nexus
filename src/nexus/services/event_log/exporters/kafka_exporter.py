"""Kafka event stream exporter (Issue #1138).

Publishes FileEvents to Apache Kafka topics using aiokafka.
Topics: ``{topic_prefix}.{zone_id}`` with LZ4 compression and
idempotent producer for exactly-once semantics.

Optional dependency: ``pip install nexus-ai-fs[kafka]``
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.file_events import FileEvent

if TYPE_CHECKING:
    from nexus.services.event_log.exporters.config import KafkaExporterConfig

logger = logging.getLogger(__name__)


class KafkaExporter:
    """Kafka event stream exporter implementing EventStreamExporterProtocol."""

    def __init__(self, config: KafkaExporterConfig) -> None:
        self._config = config
        self._producer: Any | None = None

    @property
    def name(self) -> str:
        return "kafka"

    async def _ensure_producer(self) -> Any:
        """Lazily create the Kafka producer."""
        if self._producer is not None:
            return self._producer
        try:
            from aiokafka import AIOKafkaProducer
        except ImportError as e:
            raise ImportError(
                "aiokafka is required for Kafka export. "
                "Install with: pip install nexus-ai-fs[kafka]"
            ) from e

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._config.bootstrap_servers,
            enable_idempotence=self._config.enable_idempotence,
            acks=self._config.acks,
            compression_type=self._config.compression,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8") if k else None,
        )
        await self._producer.start()
        logger.info(
            "Kafka producer started (servers=%s)",
            self._config.bootstrap_servers,
        )
        return self._producer

    async def publish(self, event: FileEvent) -> None:
        """Publish a single event to Kafka."""
        producer = await self._ensure_producer()
        topic = f"{self._config.topic_prefix}.{event.zone_id or ROOT_ZONE_ID}"
        await producer.send_and_wait(
            topic,
            value=event.to_dict(),
            key=event.event_id,
        )

    async def publish_batch(self, events: list[FileEvent]) -> list[str]:
        """Publish a batch of events, internally chunking to configured batch_size."""
        producer = await self._ensure_producer()
        failed_ids: list[str] = []
        chunk_size = self._config.batch_size

        for i in range(0, len(events), chunk_size):
            chunk = events[i : i + chunk_size]
            for event in chunk:
                topic = f"{self._config.topic_prefix}.{event.zone_id or ROOT_ZONE_ID}"
                try:
                    await producer.send_and_wait(
                        topic,
                        value=event.to_dict(),
                        key=event.event_id,
                    )
                except Exception:
                    logger.warning(
                        "Kafka publish failed for event %s",
                        event.event_id,
                        exc_info=True,
                    )
                    failed_ids.append(event.event_id)

        return failed_ids

    async def close(self) -> None:
        """Flush and close the Kafka producer."""
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
            logger.info("Kafka producer stopped")

    async def health_check(self) -> bool:
        """Check if the Kafka cluster is reachable."""
        try:
            producer = await self._ensure_producer()
            # partitions_for returns None if topic unknown, but doesn't fail
            # The act of starting the producer validates the connection.
            return producer.client.cluster.brokers() is not None
        except Exception:
            return False
