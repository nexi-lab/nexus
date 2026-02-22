"""Pydantic configuration models for event stream exporters (Issue #1138).

Defines KafkaExporterConfig, NatsExporterConfig, PubSubExporterConfig,
and the top-level EventStreamConfig that selects which exporter to use.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class KafkaExporterConfig(BaseModel):
    """Configuration for the Kafka event stream exporter."""

    bootstrap_servers: str = "localhost:9092"
    topic_prefix: str = "nexus.events"
    acks: Literal["all", "1", "0"] = "all"
    compression: str = "lz4"
    enable_idempotence: bool = True
    batch_size: int = 100


class NatsExporterConfig(BaseModel):
    """Configuration for the external NATS event stream exporter."""

    servers: str = "nats://localhost:4222"
    subject_prefix: str = "nexus.export"
    stream_name: str = "NEXUS_EXPORT"
    max_payload: int = 1_048_576  # 1MB


class PubSubExporterConfig(BaseModel):
    """Configuration for the Google Pub/Sub event stream exporter."""

    project_id: str = ""
    topic_prefix: str = "nexus-events"
    ordering_enabled: bool = True


class EventStreamConfig(BaseModel):
    """Top-level configuration for event stream export."""

    enabled: bool = False
    exporter: Literal["kafka", "nats", "pubsub"] = "kafka"
    kafka: KafkaExporterConfig | None = Field(default=None)
    nats: NatsExporterConfig | None = Field(default=None)
    pubsub: PubSubExporterConfig | None = Field(default=None)
    # Rate limiting for replay API
    replay_rate_limit: str = "10/minute"
    max_sse_connections_per_zone: int = 100
    sse_idle_timeout_s: float = 300.0
    sse_keepalive_s: float = 15.0
