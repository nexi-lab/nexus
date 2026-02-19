"""Proxy brick configuration."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any


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

    # ------------------------------------------------------------------
    # Named policy profiles (Issue #2073)
    # ------------------------------------------------------------------

    @classmethod
    def _validate_overrides(cls, overrides: dict[str, Any]) -> None:
        """Reject unknown field names early with a clear error."""
        valid = {f.name for f in dataclasses.fields(cls)}
        unknown = set(overrides) - valid
        if unknown:
            raise TypeError(f"Unknown ProxyBrickConfig fields: {unknown}")

    @classmethod
    def local(cls, remote_url: str, **overrides: Any) -> ProxyBrickConfig:
        """Low-latency LAN profile — tight timeouts, fast failure.

        Suitable when the remote kernel is on the same machine or local
        network, where high latency indicates a real problem.
        """
        cls._validate_overrides(overrides)
        defaults: dict[str, Any] = {
            "connect_timeout": 2.0,
            "request_timeout": 10.0,
            "cb_recovery_timeout": 10.0,
            "retry_max_attempts": 2,
            "retry_initial_wait": 0.2,
            "retry_max_wait": 5.0,
        }
        return cls(remote_url=remote_url, **{**defaults, **overrides})

    @classmethod
    def production(cls, remote_url: str, **overrides: Any) -> ProxyBrickConfig:
        """Internet production profile — conservative, standard defaults.

        The field defaults were originally tuned for this scenario, so
        production() applies them as-is unless overridden.
        """
        cls._validate_overrides(overrides)
        return cls(remote_url=remote_url, **overrides)

    @classmethod
    def edge(cls, remote_url: str, **overrides: Any) -> ProxyBrickConfig:
        """Edge / intermittent connectivity profile — patient retries.

        Suitable for edge deployments with unreliable networks where
        aggressive retries and long recovery windows are preferred over
        fast failure.
        """
        cls._validate_overrides(overrides)
        defaults: dict[str, Any] = {
            "cb_failure_threshold": 8,
            "cb_recovery_timeout": 60.0,
            "retry_max_attempts": 5,
            "retry_initial_wait": 1.0,
            "retry_max_wait": 60.0,
            "max_retry_count": 25,
            "connect_timeout": 10.0,
            "request_timeout": 60.0,
        }
        return cls(remote_url=remote_url, **{**defaults, **overrides})
