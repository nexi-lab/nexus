"""Proxy brick configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProxyBrickConfig:
    """Immutable configuration for ProxyBrick.

    Attributes:
        remote_url: Base URL of the remote kernel (required).
        api_key: Bearer token for authentication.
        queue_db_path: SQLite path for the offline queue.
        cb_failure_threshold: Failures before circuit opens.
        cb_recovery_timeout: Seconds before OPEN transitions to HALF_OPEN.
        cb_half_open_max_calls: Max probe calls allowed in HALF_OPEN.
        retry_max_attempts: Per-call retry attempts.
        retry_initial_wait: Initial retry backoff in seconds.
        retry_max_wait: Maximum retry backoff in seconds.
        connect_timeout: HTTP connect timeout in seconds.
        request_timeout: HTTP read/write timeout in seconds.
        max_connections: Maximum HTTP connection pool size.
        max_keepalive: Maximum keepalive connections.
        http2: Enable HTTP/2 multiplexing.
        replay_batch_size: Operations per replay batch.
        replay_poll_interval: Seconds between replay polls.
        max_retry_count: Max retries before dead-lettering a queued op.
        stream_threshold_bytes: Payloads above this size use streaming.
    """

    remote_url: str
    api_key: str | None = None
    queue_db_path: str = "~/.nexus/proxy_queue.db"

    # Circuit breaker
    cb_failure_threshold: int = 5
    cb_recovery_timeout: float = 30.0
    cb_half_open_max_calls: int = 1

    # Retry
    retry_max_attempts: int = 3
    retry_initial_wait: float = 0.5
    retry_max_wait: float = 30.0

    # Transport
    connect_timeout: float = 5.0
    request_timeout: float = 30.0
    max_connections: int = 10
    max_keepalive: int = 5
    http2: bool = True

    # Queue replay
    replay_batch_size: int = 50
    replay_poll_interval: float = 5.0
    max_retry_count: int = 10

    # Streaming
    stream_threshold_bytes: int = 65_536  # 64 KB
