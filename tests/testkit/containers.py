"""Optional-service probes and smoke-test config helpers."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest

POSTGRES_ENV_VARS: tuple[str, ...] = (
    "NEXUS_DATABASE_URL",
    "POSTGRES_URL",
    "DATABASE_URL",
)

REDIS_ENV_VARS: tuple[str, ...] = (
    "NEXUS_DRAGONFLY_URL",
    "REDIS_URL",
    "NEXUS_DRAGONFLY_COORDINATION_URL",
)

NATS_ENV_VARS: tuple[str, ...] = ("NEXUS_NATS_URL",)


@dataclass(frozen=True, slots=True)
class ServiceProbe:
    """Result of checking whether an optional service is reachable."""

    name: str
    url: str | None
    host: str | None
    port: int | None
    available: bool
    reason: str


def get_env_url(names: tuple[str, ...]) -> str | None:
    """Return the first non-empty URL from the provided environment names."""

    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def postgres_url() -> str | None:
    """Return the configured Postgres URL, if present."""

    return get_env_url(POSTGRES_ENV_VARS)


def redis_url() -> str | None:
    """Return the configured Redis or Dragonfly URL, if present."""

    return get_env_url(REDIS_ENV_VARS)


def nats_url(default: str = "nats://localhost:4222") -> str:
    """Return the configured NATS URL, defaulting to localhost."""

    return get_env_url(NATS_ENV_VARS) or default


def parse_host_port(url: str, default_port: int) -> tuple[str, int]:
    """Extract host and port from a service URL or `host:port` string."""

    raw = url if "://" in url else f"//{url}"
    parsed = urlparse(raw)
    if parsed.hostname is None:
        raise ValueError(f"service URL has no host: {url!r}")
    return parsed.hostname, parsed.port or default_port


def _missing_probe(name: str, env_names: tuple[str, ...]) -> ServiceProbe:
    return ServiceProbe(
        name=name,
        url=None,
        host=None,
        port=None,
        available=False,
        reason=f"set one of {', '.join(env_names)}",
    )


def probe_tcp_service(
    name: str,
    url: str,
    default_port: int,
    *,
    timeout: float = 0.25,
) -> ServiceProbe:
    """Probe a TCP service without importing the service's Python client."""

    host, port = parse_host_port(url, default_port)
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
        conn.close()
    except OSError as exc:
        return ServiceProbe(
            name=name,
            url=url,
            host=host,
            port=port,
            available=False,
            reason=f"{name} unavailable at {host}:{port}: {exc}",
        )
    return ServiceProbe(
        name=name,
        url=url,
        host=host,
        port=port,
        available=True,
        reason="available",
    )


def postgres_probe(*, timeout: float = 0.25) -> ServiceProbe:
    """Probe configured Postgres, or return an unavailable probe when unset."""

    url = postgres_url()
    if url is None:
        return _missing_probe("postgres", POSTGRES_ENV_VARS)
    return probe_tcp_service("postgres", url, 5432, timeout=timeout)


def redis_probe(*, timeout: float = 0.25) -> ServiceProbe:
    """Probe configured Redis or Dragonfly, or return unavailable when unset."""

    url = redis_url()
    if url is None:
        return _missing_probe("redis", REDIS_ENV_VARS)
    return probe_tcp_service("redis", url, 6379, timeout=timeout)


def nats_probe(*, timeout: float = 0.25) -> ServiceProbe:
    """Probe configured NATS, defaulting to localhost."""

    return probe_tcp_service("nats", nats_url(), 4222, timeout=timeout)


def require_service(probe: ServiceProbe) -> ServiceProbe:
    """Skip the current pytest test when the probe is unavailable."""

    if not probe.available:
        pytest.skip(probe.reason)
    return probe


def server_smoke_config(
    *,
    host: str = "127.0.0.1",
    port: int,
    api_key: str = "test-api-key",
) -> dict[str, object]:
    """Build a small server-smoke config dictionary."""

    return {
        "host": host,
        "port": port,
        "base_url": f"http://{host}:{port}",
        "api_key": api_key,
    }
