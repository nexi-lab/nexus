"""Exporter factory — creates exporter instances from configuration (Issue #1138).

Creates the appropriate exporter based on EventStreamConfig.exporter selection.
Each exporter is an optional dependency; import failures are caught and logged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.system_services.event_subsystem.log.exporter_protocol import (
        EventStreamExporterProtocol,
    )
    from nexus.system_services.event_subsystem.log.exporters.config import EventStreamConfig

logger = logging.getLogger(__name__)


def create_exporter(config: EventStreamConfig) -> EventStreamExporterProtocol | None:
    """Create an exporter instance based on configuration.

    Returns None if the exporter's optional dependency is not installed.
    """
    if not config.enabled:
        return None

    if config.exporter == "kafka":
        kafka_config = config.kafka
        if kafka_config is None:
            from nexus.system_services.event_subsystem.log.exporters.config import (
                KafkaExporterConfig,
            )

            kafka_config = KafkaExporterConfig()
        try:
            from nexus.system_services.event_subsystem.log.exporters.kafka_exporter import (
                KafkaExporter,
            )

            return KafkaExporter(kafka_config)
        except ImportError:
            logger.warning("aiokafka not installed; Kafka exporter unavailable")
            return None

    if config.exporter == "nats":
        nats_config = config.nats
        if nats_config is None:
            from nexus.system_services.event_subsystem.log.exporters.config import (
                NatsExporterConfig,
            )

            nats_config = NatsExporterConfig()
        try:
            from nexus.system_services.event_subsystem.log.exporters.nats_exporter import (
                NatsExporter,
            )

            return NatsExporter(nats_config)
        except ImportError:
            logger.warning("nats-py not installed; NATS exporter unavailable")
            return None

    if config.exporter == "pubsub":
        pubsub_config = config.pubsub
        if pubsub_config is None:
            from nexus.system_services.event_subsystem.log.exporters.config import (
                PubSubExporterConfig,
            )

            pubsub_config = PubSubExporterConfig()
        try:
            from nexus.system_services.event_subsystem.log.exporters.pubsub_exporter import (
                PubSubExporter,
            )

            return PubSubExporter(pubsub_config)
        except ImportError:
            logger.warning("gcloud-aio-pubsub not installed; Pub/Sub exporter unavailable")
            return None

    logger.error("Unknown exporter type: %s", config.exporter)
    return None
