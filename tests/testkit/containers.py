"""Lazy optional-service helpers for integration and e2e tests."""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import TracebackType

import pytest


@dataclass(frozen=True)
class ServiceInfo:
    """Connection metadata for an optional service used by tests."""

    name: str
    url: str
    env: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    cleanup: Callable[[], None] | None = None

    def __enter__(self) -> "ServiceInfo":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.cleanup is not None:
            self.cleanup()


def _is_tcp_open(host: str, port: int, *, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def skip_unavailable_service(name: str, reason: str | None = None) -> None:
    message = reason or f"{name} is not available"
    pytest.skip(message)


def postgres_service(
    url: str = "postgresql://postgres:nexus@localhost:5432/nexus",
    *,
    host: str = "localhost",
    port: int = 5432,
) -> ServiceInfo:
    """Return Postgres service metadata or skip when the service is unavailable."""
    if not _is_tcp_open(host, port):
        skip_unavailable_service("Postgres")
    return ServiceInfo("postgres", url, {"NEXUS_DATABASE_URL": url})


def redis_service(
    url: str = "redis://localhost:6379/0",
    *,
    host: str = "localhost",
    port: int = 6379,
) -> ServiceInfo:
    """Return Redis/Dragonfly service metadata or skip when unavailable."""
    if not _is_tcp_open(host, port):
        skip_unavailable_service("Redis/Dragonfly")
    return ServiceInfo(
        "redis",
        url,
        {
            "NEXUS_REDIS_URL": url,
            "DRAGONFLY_URL": url,
            "REDIS_URL": url,
            "NEXUS_DRAGONFLY_URL": url,
        },
    )


def nats_service(
    url: str = "nats://localhost:4222",
    *,
    host: str = "localhost",
    port: int = 4222,
) -> ServiceInfo:
    """Return NATS service metadata or skip when unavailable."""
    if not _is_tcp_open(host, port):
        skip_unavailable_service("NATS")
    return ServiceInfo("nats", url, {"NEXUS_NATS_URL": url})


def server_smoke_service(
    base_url: str = "http://127.0.0.1:2026",
    *,
    host: str = "127.0.0.1",
    port: int = 2026,
) -> ServiceInfo:
    """Return live Nexus server metadata or skip when unavailable."""
    if not _is_tcp_open(host, port):
        skip_unavailable_service("Nexus server")
    return ServiceInfo("server", base_url, {"NEXUS_BASE_URL": base_url})


@contextmanager
def patched_service_env(
    monkeypatch: pytest.MonkeyPatch,
    service: ServiceInfo,
) -> Iterator[ServiceInfo]:
    """Patch service environment variables for the duration of a test."""
    with monkeypatch.context() as service_env:
        for key, value in service.env.items():
            service_env.setenv(key, value)
        yield service


__all__ = [
    "ServiceInfo",
    "nats_service",
    "patched_service_env",
    "postgres_service",
    "redis_service",
    "server_smoke_service",
    "skip_unavailable_service",
]
