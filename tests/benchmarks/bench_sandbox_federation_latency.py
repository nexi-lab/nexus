"""Synthetic latency guardrails for sandbox workspace + hub federation (#4130).

These benchmarks do not require a live hub. They pin the expected cost class
for the local control branches that wrap the real network calls:
handshake parsing/mapping and remote-zone read dispatch.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from nexus.backends.storage.remote_zone import RemoteZoneBackend
from nexus.contracts.types import OperationContext
from nexus.remote import federation_handshake


def _p99(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * 0.99) - 1)]


def _measure_us(fn: Any, *, iterations: int = 500, warmup: int = 50) -> dict[str, float]:
    for _ in range(warmup):
        fn()

    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1_000_000)

    return {"p50_us": sorted(samples)[len(samples) // 2], "p99_us": _p99(samples)}


async def _measure_async_us(
    fn: Any, *, iterations: int = 200, warmup: int = 20
) -> dict[str, float]:
    for _ in range(warmup):
        await fn()

    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        await fn()
        samples.append((time.perf_counter() - start) * 1_000_000)

    return {"p50_us": sorted(samples)[len(samples) // 2], "p99_us": _p99(samples)}


class FakeHandshakeTransport:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls: list[str] = []

    def call_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> dict[str, Any]:
        assert params is None
        assert read_timeout == 0.1
        self.calls.append(method)
        return {
            "zones": [
                {"zone_id": "company", "permission": "r"},
                {"zone_id": "shared", "permission": "rw"},
            ]
        }


class FakeFileTransport:
    def __init__(self) -> None:
        self.read_calls = 0

    def read_file(self, path: str, content_id: str = "") -> bytes:
        self.read_calls += 1
        assert path == "/zone/company/policies/rate-limit.md"
        assert content_id == "cid-policy"
        return b"company policy"


class TestSandboxFederationHandshakeLatency:
    def test_handshake_control_path_maps_zone_grants_under_5ms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(federation_handshake, "RPCTransport", FakeHandshakeTransport)

        def _run_once() -> None:
            session = federation_handshake.FederationHandshake(
                "grpc://hub.example.com:2028",
                "hub-token",
                timeout=0.1,
                connect_timeout=0.1,
            ).run()
            assert [z.zone_id for z in session.zones] == ["company", "shared"]

        stats = _measure_us(_run_once, iterations=500)
        assert stats["p99_us"] < 5_000.0, stats


class TestSandboxFederationReadLatency:
    def test_remote_zone_read_dispatch_under_5ms_synthetic(self) -> None:
        transport = FakeFileTransport()
        backend = RemoteZoneBackend(zone_id="company", transport=transport, permission="r")
        context = OperationContext(
            user_id="alice",
            groups=[],
            zone_id="company",
            backend_path="policies/rate-limit.md",
            virtual_path="/zone/company/policies/rate-limit.md",
        )

        def _run_once() -> None:
            assert backend.read_content("cid-policy", context=context) == b"company policy"

        stats = _measure_us(_run_once, iterations=1_000)
        assert stats["p99_us"] < 5_000.0, stats
        assert transport.read_calls >= 1_000
