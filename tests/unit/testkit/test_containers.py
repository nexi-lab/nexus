from __future__ import annotations

import pytest
from testkit.containers import (
    ServiceProbe,
    get_env_url,
    nats_url,
    parse_host_port,
    postgres_probe,
    postgres_url,
    probe_tcp_service,
    redis_url,
    require_service,
    server_smoke_config,
)


def test_get_env_url_returns_first_configured_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONE", raising=False)
    monkeypatch.setenv("TWO", "value-two")
    monkeypatch.setenv("THREE", "value-three")

    assert get_env_url(("ONE", "TWO", "THREE")) == "value-two"


def test_postgres_url_uses_existing_env_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://u:p@db:5432/nexus")
    monkeypatch.setenv("DATABASE_URL", "postgresql://ignored")

    assert postgres_url() == "postgresql://u:p@db:5432/nexus"


def test_redis_url_uses_dragonfly_before_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_DRAGONFLY_URL", "redis://dragonfly:6379/0")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")

    assert redis_url() == "redis://dragonfly:6379/0"


def test_nats_url_defaults_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_NATS_URL", raising=False)

    assert nats_url() == "nats://localhost:4222"


def test_parse_host_port_supports_scheme_and_bare_host() -> None:
    assert parse_host_port("postgresql://u:p@db.example:5433/nexus", 5432) == (
        "db.example",
        5433,
    )
    assert parse_host_port("localhost:4222", 4222) == ("localhost", 4222)


def test_postgres_probe_reports_missing_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NEXUS_DATABASE_URL", "POSTGRES_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)

    probe = postgres_probe()
    assert probe.name == "postgres"
    assert probe.available is False
    assert "NEXUS_DATABASE_URL" in probe.reason


def test_probe_tcp_service_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, int], float]] = []

    class _Conn:
        def close(self) -> None:
            pass

    def fake_create_connection(address: tuple[str, int], timeout: float) -> _Conn:
        calls.append((address, timeout))
        return _Conn()

    monkeypatch.setattr("socket.create_connection", fake_create_connection)

    probe = probe_tcp_service("nats", "nats://localhost:4222", 4222, timeout=0.1)

    assert probe == ServiceProbe(
        name="nats",
        url="nats://localhost:4222",
        host="localhost",
        port=4222,
        available=True,
        reason="available",
    )
    assert calls == [(("localhost", 4222), 0.1)]


def test_require_service_skips_when_probe_is_unavailable() -> None:
    probe = ServiceProbe(
        name="postgres",
        url=None,
        host=None,
        port=None,
        available=False,
        reason="set NEXUS_DATABASE_URL",
    )

    with pytest.raises(pytest.skip.Exception) as exc_info:
        require_service(probe)

    assert "set NEXUS_DATABASE_URL" in str(exc_info.value)


def test_server_smoke_config_is_explicit() -> None:
    assert server_smoke_config(port=2028, api_key="key") == {
        "host": "127.0.0.1",
        "port": 2028,
        "base_url": "http://127.0.0.1:2028",
        "api_key": "key",
    }
