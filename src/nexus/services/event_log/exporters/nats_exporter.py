"""NATS external event stream exporter (Issue #1138).

Publishes FileEvents to an external NATS JetStream stream, separate
from the internal NatsEventBus used for intra-cluster communication.
Subjects: ``{subject_prefix}.{zone_id}.{event_type}``

Uses Nats-Msg-Id header for server-side deduplication.

Optional dependency: ``pip install nexus-ai-fs[nats-export]``
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.constants import ROOT_ZONE_ID
from nexus.core.file_events import FileEvent

if TYPE_CHECKING:
    from nexus.services.event_log.exporters.config import NatsExporterConfig

logger = logging.getLogger(__name__)


class NatsExporter:
    """NATS JetStream event stream exporter implementing EventStreamExporterProtocol."""

    def __init__(self, config: NatsExporterConfig) -> None:
        self._config = config
        self._nc: Any | None = None
        self._js: Any | None = None

    @property
    def name(self) -> str:
        return "nats-external"

    async def _ensure_connection(self) -> Any:
        """Lazily connect to NATS and create the JetStream context."""
        if self._js is not None:
            return self._js
        try:
            import nats
            from nats.js.api import StreamConfig
        except ImportError as e:
            raise ImportError(
                "nats-py is required for NATS export. "
                "Install with: pip install nexus-ai-fs[nats-export]"
            ) from e

        self._nc = await nats.connect(self._config.servers)
        self._js = self._nc.jetstream()

        # Ensure the export stream exists
        try:
            await self._js.find_stream_name_by_subject(f"{self._config.subject_prefix}.>")
        except Exception:
            await self._js.add_stream(
                StreamConfig(
                    name=self._config.stream_name,
                    subjects=[f"{self._config.subject_prefix}.>"],
                    max_bytes=1_073_741_824,  # 1GB
                    max_age=7 * 24 * 3600 * 1_000_000_000,  # 7 days in ns
                    duplicate_window=120_000_000_000,  # 2 min dedup
                )
            )
            logger.info("Created NATS export stream: %s", self._config.stream_name)

        logger.info("NATS exporter connected (servers=%s)", self._config.servers)
        return self._js

    def _subject(self, event: FileEvent) -> str:
        """Build the NATS subject for an event."""
        zone = event.zone_id or ROOT_ZONE_ID
        event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
        return f"{self._config.subject_prefix}.{zone}.{event_type}"

    async def publish(self, event: FileEvent) -> None:
        """Publish a single event to NATS JetStream."""
        js = await self._ensure_connection()
        payload = json.dumps(event.to_dict()).encode("utf-8")
        await js.publish(
            self._subject(event),
            payload,
            headers={"Nats-Msg-Id": event.event_id},
        )

    async def publish_batch(self, events: list[FileEvent]) -> list[str]:
        """Publish a batch of events to NATS JetStream."""
        js = await self._ensure_connection()
        failed_ids: list[str] = []

        for event in events:
            try:
                payload = json.dumps(event.to_dict()).encode("utf-8")
                await js.publish(
                    self._subject(event),
                    payload,
                    headers={"Nats-Msg-Id": event.event_id},
                )
            except Exception:
                logger.warning(
                    "NATS publish failed for event %s",
                    event.event_id,
                    exc_info=True,
                )
                failed_ids.append(event.event_id)

        return failed_ids

    async def close(self) -> None:
        """Close the NATS connection."""
        if self._nc is not None:
            await self._nc.close()
            self._nc = None
            self._js = None
            logger.info("NATS exporter connection closed")

    async def health_check(self) -> bool:
        """Check if NATS is reachable."""
        try:
            if self._nc is None:
                await self._ensure_connection()
            return self._nc is not None and self._nc.is_connected
        except Exception:
            return False
