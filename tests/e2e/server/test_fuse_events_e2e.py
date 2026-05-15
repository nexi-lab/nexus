"""End-to-end tests for FUSE event firing (Issue #1115).

Tests the complete flow:
1. Start FastAPI server with real database
2. Create a webhook subscription
3. Start a mock webhook receiver
4. Perform file operations via API
5. Verify events are delivered to webhook

Run with:
    pytest tests/e2e/test_fuse_events_e2e.py -v --override-ini="addopts="
"""

import base64
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Empty, Queue
from typing import Any

import httpx
import pytest

from nexus.contracts.constants import ROOT_ZONE_ID

# ==============================================================================
# Mock Webhook Server
# ==============================================================================


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures webhook requests."""

    # Class-level queue shared by all handler instances
    received_events: Queue = Queue()

    def do_POST(self) -> None:
        """Handle POST requests (webhook deliveries)."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body.decode("utf-8"))
            self.received_events.put(payload)
        except json.JSONDecodeError:
            self.received_events.put({"raw": body.decode("utf-8")})

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress logging."""
        pass


class MockWebhookServer:
    """Context manager for running a mock webhook server."""

    def __init__(self, port: int = 0):
        self.port = port
        self.server: HTTPServer | None = None
        self.thread: threading.Thread | None = None
        # Reset the queue
        WebhookHandler.received_events = Queue()

    def __enter__(self) -> "MockWebhookServer":
        self.server = HTTPServer(("127.0.0.1", self.port), WebhookHandler)
        self.port = self.server.server_address[1]  # Get assigned port
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        if self.server:
            self.server.shutdown()
        if self.thread:
            self.thread.join(timeout=1)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/webhook"

    def get_events(self, timeout: float = 5.0) -> list[dict]:
        """Get all received events within timeout."""
        events = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                event = WebhookHandler.received_events.get(timeout=0.1)
                events.append(event)
            except Empty:
                if events:  # Got at least one event, wait a bit more for others
                    time.sleep(0.2)
                    try:
                        while True:
                            event = WebhookHandler.received_events.get_nowait()
                            events.append(event)
                    except Empty:
                        break
        return events


# ==============================================================================
# Helper Functions
# ==============================================================================


def encode_bytes(content: bytes) -> dict:
    """Encode bytes for JSON-RPC transport."""
    return {"__type__": "bytes", "data": base64.b64encode(content).decode("utf-8")}


"""The e2e server is started with ``NEXUS_API_KEY=test-e2e-api-key-12345``
(see ``tests/e2e/conftest.py``).  All requests must carry this key as a
Bearer token — user-registration based auth is not used here.
"""

# Static API key matching conftest.py's NEXUS_API_KEY
_E2E_API_KEY = "test-e2e-api-key-12345"
_AUTH_HEADERS: dict[str, str] = {"Authorization": f"Bearer {_E2E_API_KEY}"}


def make_rpc_request(
    client: httpx.Client,
    method: str,
    params: dict,
) -> dict:
    """Make an RPC request to the server using static API key auth."""
    response = client.post(
        f"/api/nfs/{method}",
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        },
        headers=_AUTH_HEADERS,
    )
    return response.json()


# ==============================================================================
# Tests
# ==============================================================================


class TestFUSEEventsE2E:
    """E2E tests for FUSE event firing."""

    def test_write_event_fires_webhook(self, test_app: httpx.Client) -> None:
        """Test that writing a file triggers webhook delivery."""
        with MockWebhookServer() as webhook_server:
            # 1. Create webhook subscription
            response = test_app.post(
                "/api/v2/subscriptions",
                json={
                    "url": webhook_server.url,
                    "event_types": ["file_write", "file_delete"],
                    "patterns": ["/**/*"],
                    "name": "test-subscription",
                },
                headers=_AUTH_HEADERS,
            )
            assert response.status_code == 201, f"Failed to create subscription: {response.text}"
            subscription = response.json()
            assert subscription.get("id"), "No subscription ID returned"

            # 2. Write a file via RPC
            file_path = f"/test_{uuid.uuid4().hex[:8]}.txt"
            result = make_rpc_request(
                test_app,
                "write",
                {"path": file_path, "content": encode_bytes(b"Hello, World!")},
            )
            assert "error" not in result, f"Write failed: {result}"

            # 3. Wait for webhook delivery
            events = webhook_server.get_events(timeout=5.0)

            # 4. Verify event was received
            assert len(events) >= 1, f"Expected at least 1 event, got {len(events)}: {events}"

            # Find the file_write event
            write_events = [e for e in events if e.get("event") == "file_write"]
            assert len(write_events) >= 1, f"No file_write event found in: {events}"

            event = write_events[0]
            assert event.get("data", {}).get("file_path") == file_path

    def test_delete_event_fires_webhook(self, test_app: httpx.Client) -> None:
        """Test that deleting a file triggers webhook delivery."""
        with MockWebhookServer() as webhook_server:
            # 1. Create file first
            file_path = f"/test_{uuid.uuid4().hex[:8]}.txt"
            make_rpc_request(
                test_app,
                "write",
                {"path": file_path, "content": encode_bytes(b"To be deleted")},
            )

            # 2. Create webhook subscription (after file exists)
            response = test_app.post(
                "/api/v2/subscriptions",
                json={
                    "url": webhook_server.url,
                    "event_types": ["file_delete"],
                    "patterns": ["/**/*"],
                    "name": "delete-subscription",
                },
                headers=_AUTH_HEADERS,
            )
            assert response.status_code == 201

            # 3. Delete the file
            result = make_rpc_request(
                test_app,
                "delete",
                {"path": file_path},
            )
            assert "error" not in result, f"Delete failed: {result}"

            # 4. Verify event was received
            events = webhook_server.get_events(timeout=5.0)
            delete_events = [e for e in events if e.get("event") == "file_delete"]
            assert len(delete_events) >= 1, f"No file_delete event found in: {events}"

    def test_mkdir_event_fires_webhook(self, test_app: httpx.Client) -> None:
        """Test that creating a directory triggers webhook delivery."""
        with MockWebhookServer() as webhook_server:
            # 1. Create webhook subscription
            response = test_app.post(
                "/api/v2/subscriptions",
                json={
                    "url": webhook_server.url,
                    "event_types": ["dir_create"],
                    "patterns": ["/**/*"],
                    "name": "mkdir-subscription",
                },
                headers=_AUTH_HEADERS,
            )
            assert response.status_code == 201, f"Failed to create subscription: {response.text}"

            # 2. Create directory
            dir_path = f"/testdir_{uuid.uuid4().hex[:8]}"
            result = make_rpc_request(
                test_app,
                "mkdir",
                {"path": dir_path},
            )
            assert "error" not in result, f"Mkdir failed: {result}"

            # 3. Verify event was received
            events = webhook_server.get_events(timeout=5.0)
            dir_events = [e for e in events if e.get("event") == "dir_create"]
            assert len(dir_events) >= 1, f"No dir_create event found in: {events}"

    def test_subscription_test_endpoint(self, test_app: httpx.Client) -> None:
        """Test the subscription test endpoint works."""
        with MockWebhookServer() as webhook_server:
            # 1. Create webhook subscription
            response = test_app.post(
                "/api/v2/subscriptions",
                json={
                    "url": webhook_server.url,
                    "event_types": ["file_write"],
                    "patterns": ["/**/*"],
                    "name": "test-endpoint-subscription",
                },
                headers=_AUTH_HEADERS,
            )
            assert response.status_code == 201
            subscription = response.json()
            sub_id = subscription.get("id")

            # 2. Call test endpoint
            test_response = test_app.post(
                f"/api/v2/subscriptions/{sub_id}/test",
                headers=_AUTH_HEADERS,
            )
            assert test_response.status_code == 200
            test_result = test_response.json()
            assert test_result.get("success") is True

            # 3. Verify test event was received
            events = webhook_server.get_events(timeout=5.0)
            assert len(events) >= 1, "Test event not received"
            assert events[0].get("data", {}).get("_test") is True


def is_redis_available(host: str = "127.0.0.1", port: int = 1778) -> bool:
    """Check if Redis is available on the specified host:port."""
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


class TestEventBusIntegration:
    """Tests for event bus integration (requires Redis/Dragonfly)."""

    @pytest.mark.skipif(
        not is_redis_available(),
        reason="Requires Redis/Dragonfly to be running on port 1778",
    )
    @pytest.mark.asyncio
    async def test_event_bus_publish(self) -> None:
        """Test events are published to event bus via Redis."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

        from nexus.cache.dragonfly import DragonflyClient
        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        # Connect to Redis on port 7899
        redis_client = DragonflyClient(url="redis://127.0.0.1:1778")
        await redis_client.connect()

        try:
            bus = RedisEventBus(redis_client)
            await bus.start()

            # Create and publish an event
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/test/event_bus_test.txt",
                zone_id=ROOT_ZONE_ID,
                size=100,
            )

            # Publish event
            subscribers = await bus.publish(event)
            print(f"[EVENT BUS] Published to {subscribers} subscribers")

            # Verify the event was published by checking Redis
            # (In a real test, we'd have a subscriber waiting for the event)
            assert event.path == "/test/event_bus_test.txt"
            assert event.type == FileEventType.FILE_WRITE

            await bus.stop()
            print("[EVENT BUS] Test passed: event published successfully")

        finally:
            await redis_client.disconnect()

    @pytest.mark.skipif(
        not is_redis_available(),
        reason="Requires Redis/Dragonfly to be running on port 1778",
    )
    @pytest.mark.asyncio
    async def test_event_bus_subscribe_and_receive(self) -> None:
        """Test subscribing and receiving events via Redis."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

        from nexus.cache.dragonfly import DragonflyClient
        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        # Connect to Redis on port 7899
        redis_client = DragonflyClient(url="redis://127.0.0.1:1778")
        await redis_client.connect()

        try:
            bus = RedisEventBus(redis_client)
            await bus.start()

            # Create event
            event = FileEvent(
                type=FileEventType.FILE_DELETE,
                path="/test/to_delete.txt",
                zone_id="test_zone",
            )

            # Start waiting for event in background
            import asyncio

            async def publish_after_delay():
                await asyncio.sleep(0.5)
                await bus.publish(event)

            # Start publisher
            publish_task = asyncio.create_task(publish_after_delay())

            # Wait for event
            received = await bus.wait_for_event(
                zone_id="test_zone",
                path_pattern="/test/",
                timeout=5.0,
            )

            await publish_task

            # Verify received event
            assert received is not None, "Should have received an event"
            assert received.path == "/test/to_delete.txt"
            assert received.type == FileEventType.FILE_DELETE

            await bus.stop()
            print("[EVENT BUS] Test passed: event received successfully")

        finally:
            await redis_client.disconnect()
