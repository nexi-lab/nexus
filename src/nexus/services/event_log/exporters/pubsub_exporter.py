"""Google Pub/Sub event stream exporter (Issue #1138).

Publishes FileEvents to Google Cloud Pub/Sub topics.
Topics: ``projects/{project}/topics/{prefix}-{zone_id}``
Uses ordering key = zone_id for ordered delivery within a zone.

Optional dependency: ``pip install nexus-ai-fs[pubsub]``
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.constants import ROOT_ZONE_ID
from nexus.core.file_events import FileEvent

if TYPE_CHECKING:
    from nexus.services.event_log.exporters.config import PubSubExporterConfig

logger = logging.getLogger(__name__)


class PubSubExporter:
    """Google Pub/Sub event stream exporter implementing EventStreamExporterProtocol."""

    def __init__(self, config: PubSubExporterConfig) -> None:
        self._config = config
        self._publisher: Any | None = None

    @property
    def name(self) -> str:
        return "pubsub"

    async def _ensure_publisher(self) -> Any:
        """Lazily create the Pub/Sub publisher client."""
        if self._publisher is not None:
            return self._publisher
        try:
            from gcloud.aio.pubsub import PublisherClient
        except ImportError as e:
            raise ImportError(
                "gcloud-aio-pubsub is required for Pub/Sub export. "
                "Install with: pip install nexus-ai-fs[pubsub]"
            ) from e

        self._publisher = PublisherClient()
        logger.info("Pub/Sub publisher created (project=%s)", self._config.project_id)
        return self._publisher

    def _topic_path(self, zone_id: str) -> str:
        """Build the full topic path for a zone."""
        return f"projects/{self._config.project_id}/topics/{self._config.topic_prefix}-{zone_id}"

    async def publish(self, event: FileEvent) -> None:
        """Publish a single event to Pub/Sub."""
        publisher = await self._ensure_publisher()
        zone = event.zone_id or ROOT_ZONE_ID
        topic = self._topic_path(zone)
        data = json.dumps(event.to_dict()).encode("utf-8")

        kwargs: dict[str, Any] = {}
        if self._config.ordering_enabled:
            kwargs["ordering_key"] = zone

        await publisher.publish(
            topic,
            [data],
            **kwargs,
        )

    async def publish_batch(self, events: list[FileEvent]) -> list[str]:
        """Publish a batch of events to Pub/Sub."""
        publisher = await self._ensure_publisher()
        failed_ids: list[str] = []

        # Group events by zone for efficient batching
        by_zone: dict[str, list[FileEvent]] = {}
        for event in events:
            zone = event.zone_id or ROOT_ZONE_ID
            by_zone.setdefault(zone, []).append(event)

        for zone, zone_events in by_zone.items():
            topic = self._topic_path(zone)
            messages = [json.dumps(ev.to_dict()).encode("utf-8") for ev in zone_events]
            try:
                kwargs: dict[str, Any] = {}
                if self._config.ordering_enabled:
                    kwargs["ordering_key"] = zone
                await publisher.publish(topic, messages, **kwargs)
            except Exception:
                logger.warning(
                    "Pub/Sub publish failed for zone %s (%d events)",
                    zone,
                    len(zone_events),
                    exc_info=True,
                )
                failed_ids.extend(ev.event_id for ev in zone_events)

        return failed_ids

    async def close(self) -> None:
        """Close the Pub/Sub publisher."""
        if self._publisher is not None:
            await self._publisher.close()
            self._publisher = None
            logger.info("Pub/Sub publisher closed")

    async def health_check(self) -> bool:
        """Check if Pub/Sub is reachable."""
        try:
            await self._ensure_publisher()
            return True
        except Exception:
            return False
