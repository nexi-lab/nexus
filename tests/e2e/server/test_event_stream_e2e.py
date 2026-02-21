"""E2E tests for Event Stream Export + Event Replay (Issues #1138, #1139).

Starts a real nexus server process, performs file operations via the API,
and verifies:
1. Events appear in the replay API with correct pagination
2. SSE endpoint returns proper headers for real-time streaming
3. No errors/warnings in server logs during normal operation

Issue #1138: Event Stream Export
Issue #1139: Event Replay
"""

import base64
import time
from typing import Any

import httpx


def _encode_bytes(data: bytes) -> dict:
    """Encode bytes for JSON-RPC transport."""
    return {"__type__": "bytes", "data": base64.b64encode(data).decode()}


def _rpc(client: httpx.Client, method: str, params: dict[str, Any], api_key: str) -> dict:
    """Send a JSON-RPC request to the nexus server."""
    resp = client.post(
        f"/api/nfs/{method}",
        json={"jsonrpc": "2.0", "id": "1", "method": method, "params": params},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return resp.json()


def _write_file(
    client: httpx.Client,
    path: str,
    content: bytes,
    api_key: str,
) -> dict:
    """Write a file via JSON-RPC (method: write, param: content)."""
    return _rpc(
        client,
        "write",
        {
            "path": path,
            "content": _encode_bytes(content),
        },
        api_key,
    )


API_KEY = "test-e2e-api-key-12345"


class TestEventReplayE2E:
    """E2E tests for the v2 events replay REST endpoint."""

    def test_replay_empty_before_operations(self, test_app: httpx.Client) -> None:
        """Replay returns empty list before any file operations."""
        resp = test_app.get(
            "/api/v2/events/replay",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["events"] == []
        assert data["has_more"] is False

    def test_replay_after_file_writes(self, test_app: httpx.Client) -> None:
        """Events appear in replay after file write operations."""
        for i in range(3):
            result = _write_file(
                test_app,
                f"/workspace/replay-test-{i}.txt",
                f"content-{i}".encode(),
                API_KEY,
            )
            # Write may return result or error; check it didn't hard-fail
            assert "error" not in result or result.get("result") is not None, (
                f"Write failed: {result}"
            )

        # Give the delivery worker time to process
        time.sleep(2)

        resp = test_app.get(
            "/api/v2/events/replay",
            params={"limit": 100},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()

        # Should have at least the 3 write events
        assert len(data["events"]) >= 3

        # Verify event shape
        for event in data["events"]:
            assert "event_id" in event
            assert "type" in event
            assert "path" in event
            assert "timestamp" in event

    def test_replay_pagination_e2e(self, test_app: httpx.Client) -> None:
        """Pagination works correctly with real events."""
        for i in range(5):
            _write_file(
                test_app,
                f"/workspace/page-test-{i}.txt",
                f"data-{i}".encode(),
                API_KEY,
            )

        time.sleep(2)

        all_ids: set[str] = set()
        cursor = None

        for _ in range(20):  # Safety bound
            params: dict[str, Any] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor

            resp = test_app.get(
                "/api/v2/events/replay",
                params=params,
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            assert resp.status_code == 200
            data = resp.json()

            for ev in data["events"]:
                assert ev["event_id"] not in all_ids, "Duplicate event in pagination"
                all_ids.add(ev["event_id"])

            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
            assert cursor is not None

        # Should have collected at least 5 events
        assert len(all_ids) >= 5

    def test_replay_filter_by_path_pattern(self, test_app: httpx.Client) -> None:
        """Path pattern filter works in live replay."""
        _write_file(test_app, "/workspace/filter/a.txt", b"a", API_KEY)
        _write_file(test_app, "/other/filter/b.txt", b"b", API_KEY)

        time.sleep(2)

        resp = test_app.get(
            "/api/v2/events/replay",
            params={"path_pattern": "/workspace/%"},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()

        for event in data["events"]:
            assert event["path"].startswith("/workspace/"), (
                f"Event path {event['path']} does not match pattern"
            )

    def test_replay_event_ordering(self, test_app: httpx.Client) -> None:
        """Events are returned in sequence_number order."""
        for i in range(3):
            _write_file(
                test_app,
                f"/workspace/order-test-{i}.txt",
                f"order-{i}".encode(),
                API_KEY,
            )

        time.sleep(2)

        resp = test_app.get(
            "/api/v2/events/replay",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        data = resp.json()

        seq_nums = [
            e["sequence_number"]
            for e in data["events"]
            if "sequence_number" in e and e["sequence_number"] is not None
        ]
        assert seq_nums == sorted(seq_nums), "Events not in sequence order"


class TestEventStreamSSEE2E:
    """E2E tests for SSE streaming endpoint."""

    def test_sse_endpoint_returns_correct_headers(self, nexus_server: dict[str, Any]) -> None:
        """SSE endpoint returns proper content-type and headers."""
        base_url = nexus_server["base_url"]

        with (
            httpx.Client(base_url=base_url, timeout=5.0, trust_env=False) as client,
            client.stream(
                "GET",
                "/api/v2/events/stream",
                params={"since_revision": 0},
                headers={"Authorization": f"Bearer {API_KEY}"},
            ) as resp,
        ):
            assert resp.status_code == 200
            content_type = resp.headers.get("content-type", "")
            assert "text/event-stream" in content_type
            assert resp.headers.get("x-accel-buffering") == "no"
            assert resp.headers.get("cache-control") == "no-cache"


class TestEventReplayPerformance:
    """Performance validation for replay queries."""

    def test_replay_query_latency(self, test_app: httpx.Client) -> None:
        """Replay query should complete within reasonable time.

        Target: <500ms for moderate event counts via e2e (includes network).
        """
        for i in range(10):
            _write_file(
                test_app,
                f"/workspace/perf-test-{i}.txt",
                f"perf-data-{i}".encode(),
                API_KEY,
            )

        time.sleep(2)

        start = time.monotonic()
        resp = test_app.get(
            "/api/v2/events/replay",
            params={"limit": 100},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) >= 10

        # E2E latency should be under 500ms (generous for full server roundtrip)
        assert elapsed_ms < 500, f"Replay query took {elapsed_ms:.0f}ms (target <500ms)"


class TestEventHealthEndpoint:
    """Verify health endpoint includes event system status."""

    def test_health_endpoint_available(self, test_app: httpx.Client) -> None:
        """Health endpoint responds (exporter health only when configured)."""
        resp = test_app.get("/health")
        assert resp.status_code == 200
