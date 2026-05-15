"""Tests for the CLI REST API client."""

from __future__ import annotations

from typing import Any

import httpx

from nexus.cli.api_client import NexusApiClient


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {"status": "ready"}


def test_loopback_localhost_uses_ipv4_without_proxy_env(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    def fake_get(
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        timeout: float,
        trust_env: bool,
    ) -> _Response:
        calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout,
                "trust_env": trust_env,
            }
        )
        return _Response()

    monkeypatch.setattr(httpx, "get", fake_get)

    client = NexusApiClient(url="http://localhost:2026", api_key="sk-test")
    assert client.get("/healthz/ready") == {"status": "ready"}

    assert calls == [
        {
            "url": "http://127.0.0.1:2026/healthz/ready",
            "headers": {"Accept": "application/json", "Authorization": "Bearer sk-test"},
            "params": None,
            "timeout": 30.0,
            "trust_env": False,
        }
    ]


def test_remote_url_keeps_proxy_env_trust(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    def fake_get(
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        timeout: float,
        trust_env: bool,
    ) -> _Response:
        calls.append({"url": url, "trust_env": trust_env})
        return _Response()

    monkeypatch.setattr(httpx, "get", fake_get)

    client = NexusApiClient(url="https://nexus.example.com", api_key="sk-test")
    client.get("/healthz/ready")

    assert calls == [{"url": "https://nexus.example.com/healthz/ready", "trust_env": True}]
