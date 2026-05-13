"""E2E coverage for per-zone runners through ``nexus up --build``."""

from __future__ import annotations

import asyncio
import base64
import statistics
import time
import uuid

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(900)]


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def _rpc_call(
    client: httpx.AsyncClient,
    method: str,
    params: dict[str, object],
) -> dict[str, object]:
    response = await client.post(
        f"/api/nfs/{method}",
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "error" not in payload, payload
    return payload["result"]


class TestZoneRunnerLive:
    @pytest.mark.asyncio
    async def test_file_routes_instantiate_zone_runners_and_bulkhead_fast_zone(
        self, running_nexus
    ) -> None:
        tag = _tag()
        zone_a = f"za{tag[:6]}"
        zone_b = f"zb{tag[:6]}"
        headers = {"Authorization": f"Bearer {running_nexus.admin_api_key}"}

        async with httpx.AsyncClient(
            base_url=running_nexus.http_url,
            headers=headers,
            timeout=20.0,
        ) as client:
            hook_check = await client.get("/api/test-hooks/zone-runners")
            assert hook_check.status_code == 200, (
                "NEXUS_TEST_HOOKS=true is required for this live zone-runner E2E; "
                f"got {hook_check.status_code}: {hook_check.text}"
            )

            for zone in (zone_a, zone_b):
                path = f"/zone-runner-e2e-{tag}-{zone}.txt"
                write_resp = await client.post(
                    f"/api/v2/files/write?zone={zone}",
                    json={"path": path, "content": f"payload::{zone}::{tag}"},
                )
                assert write_resp.status_code == 200, write_resp.text

                read_resp = await client.get(
                    "/api/v2/files/read",
                    params={"zone": zone, "path": path},
                )
                assert read_resp.status_code == 200, read_resp.text
                assert read_resp.json()["content"] == f"payload::{zone}::{tag}"

            runners_resp = await client.get("/api/test-hooks/zone-runners")
            assert runners_resp.status_code == 200, runners_resp.text
            runner_zones = {r["zone_id"] for r in runners_resp.json()["runners"]}
            assert {zone_a, zone_b}.issubset(runner_zones)

            slow_task = asyncio.create_task(
                client.post(f"/api/test-hooks/zone-runners/{zone_a}/sleep?delay_ms=1500")
            )
            await asyncio.sleep(0.2)

            latencies: list[float] = []
            for _ in range(10):
                start = time.perf_counter()
                ping_resp = await client.get(f"/api/test-hooks/zone-runners/{zone_b}/ping")
                latencies.append(time.perf_counter() - start)
                assert ping_resp.status_code == 200, ping_resp.text
                assert ping_resp.json()["thread_name"] == f"nexus-zone-{zone_b}"

            slow_resp = await slow_task
            assert slow_resp.status_code == 200, slow_resp.text
            assert slow_resp.json()["thread_name"] == f"nexus-zone-{zone_a}"
            assert statistics.median(latencies) < 0.25

    @pytest.mark.asyncio
    async def test_http_rpc_routes_explicit_zone_paths_through_zone_runners(
        self, running_nexus
    ) -> None:
        tag = _tag()
        zone_a = f"ra{tag[:6]}"
        zone_b = f"rb{tag[:6]}"
        headers = {"Authorization": f"Bearer {running_nexus.admin_api_key}"}

        async with httpx.AsyncClient(
            base_url=running_nexus.http_url,
            headers=headers,
            timeout=20.0,
        ) as client:
            for zone in (zone_a, zone_b):
                path = f"/zone/{zone}/rpc-zone-runner-{tag}.txt"
                content = f"rpc::{zone}::{tag}"

                write_result = await _rpc_call(
                    client,
                    "write",
                    {"path": path, "content": content},
                )
                assert write_result["bytes_written"] == len(content)

                read_result = await _rpc_call(client, "read", {"path": path})
                assert base64.b64decode(read_result["data"]).decode() == content

            runners_resp = await client.get("/api/test-hooks/zone-runners")
            assert runners_resp.status_code == 200, runners_resp.text
            runner_zones = {r["zone_id"] for r in runners_resp.json()["runners"]}
            assert {zone_a, zone_b}.issubset(runner_zones)

            for zone in (zone_a, zone_b):
                ping_resp = await client.get(f"/api/test-hooks/zone-runners/{zone}/ping")
                assert ping_resp.status_code == 200, ping_resp.text
                assert ping_resp.json()["thread_name"] == f"nexus-zone-{zone}"
