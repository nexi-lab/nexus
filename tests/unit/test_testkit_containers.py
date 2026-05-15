"""Tests for optional service helpers in tests.testkit.containers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_containers_module_does_not_import_optional_clients_in_fresh_process() -> None:
    code = """
import importlib.abc
import sys

blocked = {"docker", "redis", "nats", "psycopg2", "asyncpg"}


class BlockOptionalClients(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split(".", 1)[0] in blocked:
            raise AssertionError(f"optional client imported eagerly: {fullname}")
        return None


sys.meta_path.insert(0, BlockOptionalClients())
import tests.testkit.containers as containers
assert containers.ServiceInfo.__name__ == "ServiceInfo"
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_service_info_context_manager_runs_cleanup_once() -> None:
    from tests.testkit.containers import ServiceInfo

    calls: list[str] = []
    service = ServiceInfo(
        name="redis",
        url="redis://localhost:6379/0",
        env={"REDIS_URL": "redis://localhost:6379/0"},
        cleanup=lambda: calls.append("cleanup"),
    )

    with service as entered:
        assert entered is service

    assert calls == ["cleanup"]


def test_patched_service_env_restores_environment_after_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.testkit.containers import ServiceInfo, patched_service_env

    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://original/db")
    monkeypatch.delenv("NEXUS_NATS_URL", raising=False)
    service = ServiceInfo(
        name="postgres",
        url="postgresql://patched/db",
        env={
            "NEXUS_DATABASE_URL": "postgresql://patched/db",
            "NEXUS_NATS_URL": "nats://patched:4222",
        },
    )

    with patched_service_env(monkeypatch, service) as entered:
        assert entered is service
        assert os.environ["NEXUS_DATABASE_URL"] == "postgresql://patched/db"
        assert os.environ["NEXUS_NATS_URL"] == "nats://patched:4222"

    assert os.environ["NEXUS_DATABASE_URL"] == "postgresql://original/db"
    assert "NEXUS_NATS_URL" not in os.environ


def test_redis_service_env_includes_canonical_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.testkit import containers

    monkeypatch.setattr(containers, "_is_tcp_open", lambda host, port: True)

    service = containers.redis_service("redis://example.test:6379/1")

    assert service.env == {
        "NEXUS_REDIS_URL": "redis://example.test:6379/1",
        "DRAGONFLY_URL": "redis://example.test:6379/1",
        "REDIS_URL": "redis://example.test:6379/1",
        "NEXUS_DRAGONFLY_URL": "redis://example.test:6379/1",
    }


def test_service_helpers_return_env_metadata_when_service_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.testkit import containers

    monkeypatch.setattr(containers, "_is_tcp_open", lambda host, port: True)

    postgres = containers.postgres_service("postgresql://example.test/nexus")
    nats = containers.nats_service("nats://example.test:4222")
    server = containers.server_smoke_service("http://example.test:2026")

    assert postgres.name == "postgres"
    assert postgres.url == "postgresql://example.test/nexus"
    assert postgres.env == {"NEXUS_DATABASE_URL": "postgresql://example.test/nexus"}
    assert nats.name == "nats"
    assert nats.url == "nats://example.test:4222"
    assert nats.env == {"NEXUS_NATS_URL": "nats://example.test:4222"}
    assert server.name == "server"
    assert server.url == "http://example.test:2026"
    assert server.env == {"NEXUS_BASE_URL": "http://example.test:2026"}


def test_skip_unavailable_service_raises_pytest_skip() -> None:
    from tests.testkit.containers import skip_unavailable_service

    with pytest.raises(pytest.skip.Exception, match="Postgres is not available"):
        skip_unavailable_service("Postgres")
