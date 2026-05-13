"""E2E coverage for per-zone runners through ``nexus up --build``."""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(900)]


def _tag() -> str:
    return uuid.uuid4().hex[:8]


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
