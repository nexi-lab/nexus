"""E2E tests for NATS event bus with FastAPI server and permissions.

Tests the full flow:
1. FastAPI server starts with NATS event bus backend
2. File writes via JSON-RPC → events published to NATS JetStream
3. Permission enforcement: unauthorized requests rejected, no events published
4. Events received by durable subscribers
5. Event deduplication works end-to-end

Requires: NATS JetStream server (port 4222)
Related: Issue #1331
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from typing import Any

import pytest

# ============================================================================
# Skip conditions
# ============================================================================


def _is_port_open(host: str, port: int) -> bool:
    try:
        sock = socket.create_connection((host, port), timeout=2)
        sock.close()
        return True
    except OSError:
        return False


NATS_URL = os.environ.get("NEXUS_NATS_URL", "nats://localhost:4222")
nats_available = _is_port_open("localhost", 4222)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not nats_available, reason="NATS not available on :4222"),
]


# ============================================================================
# Helpers
# ============================================================================


def _rpc_call(
    client: Any, method: str, params: dict, api_key: str | None = None
) -> tuple[int, dict]:
    """Make a JSON-RPC call to the test server.

    Returns (status_code, response_json) so tests can check both HTTP status
    and body content.
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    response = client.post(f"/api/nfs/{method}", json=payload, headers=headers)
    return response.status_code, response.json()


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from sync test code."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _find_event_in_nats(
    path: str, event_type: str | None = None, timeout: float = 5.0
) -> bool:
    """Connect to NATS and scan the stream for an event matching the given path."""
    import nats as nats_lib

    nc = await nats_lib.connect(NATS_URL)
    js = nc.jetstream()
    sub = await js.subscribe("nexus.events.>", ordered_consumer=True)

    found = False
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(sub.next_msg(), timeout=2.0)
                data = json.loads(msg.data)
                if data.get("path") == path and (
                    event_type is None or data.get("type") == event_type
                ):
                    found = True
                    break
            except TimeoutError:
                break
    finally:
        await sub.unsubscribe()
        await nc.close()

    return found


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="module")
def server_app():
    """Create a FastAPI app with NATS event bus and API key auth."""
    import nats as nats_lib

    # Clean NATS stream before tests
    async def _cleanup():
        nc = await nats_lib.connect(NATS_URL)
        js = nc.jetstream()
        try:
            await js.delete_stream("NEXUS_EVENTS")
        except Exception:
            pass
        await nc.close()

    _run_async(_cleanup())

    # Set env vars BEFORE creating NexusFS
    os.environ["NEXUS_EVENT_BUS_BACKEND"] = "nats"
    os.environ["NEXUS_NATS_URL"] = NATS_URL

    import tempfile

    import nexus
    from nexus.core.nexus_fs import NexusFS
    from nexus.server.fastapi_server import create_app

    data_dir = tempfile.mkdtemp(prefix="nexus_e2e_nats_")

    nexus_fs = nexus.connect(
        config={
            "mode": "embedded",
            "data_dir": data_dir,
            # Filesystem-level permissions off (SQLite lacks rebac_tuples table).
            # Auth enforcement is tested at the HTTP layer via require_auth.
            "enforce_permissions": False,
            "allow_admin_bypass": True,
        }
    )

    assert isinstance(nexus_fs, NexusFS)

    # Disable audit strict mode — SQLite embedded mode lacks operation_log table.
    # We're testing the NATS event bus, not audit logging.
    nexus_fs._audit_strict_mode = False

    # Verify event bus is NATS
    from nexus.core.event_bus_nats import NatsEventBus

    assert nexus_fs._event_bus is not None
    assert isinstance(nexus_fs._event_bus, NatsEventBus)

    app = create_app(
        nexus_fs=nexus_fs,
        api_key="test-e2e-key-1331",
    )

    yield app, nexus_fs

    # Cleanup
    try:
        nexus_fs.close()
    except Exception:
        pass

    # Clean NATS stream after tests
    _run_async(_cleanup())

    os.environ.pop("NEXUS_EVENT_BUS_BACKEND", None)
    os.environ.pop("NEXUS_NATS_URL", None)


@pytest.fixture(scope="module")
def client(server_app):
    """Create test client — triggers lifespan which starts event bus + creates stream."""
    from starlette.testclient import TestClient

    app, _ = server_app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def nexus_fs(server_app):
    _, nfs = server_app
    return nfs


@pytest.fixture(scope="module")
def api_key():
    return "test-e2e-key-1331"


# ============================================================================
# Test: Server starts with NATS event bus
# ============================================================================


class TestServerStartup:
    """Verify the server initializes NATS event bus correctly."""

    def test_event_bus_is_nats(self, client, nexus_fs):
        """Event bus should be NatsEventBus."""
        from nexus.core.event_bus_nats import NatsEventBus

        assert isinstance(nexus_fs._event_bus, NatsEventBus)

    def test_event_bus_started(self, client, nexus_fs):
        """Event bus should be started after lifespan init."""
        assert nexus_fs._event_bus._started is True

    def test_health_endpoint(self, client):
        """Server health endpoint should respond."""
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_event_bus_health(self, client, nexus_fs):
        """NATS event bus health check should pass."""
        result = _run_async(nexus_fs._event_bus.health_check())
        assert result is True

    def test_main_event_loop_set(self, client, nexus_fs):
        """Lifespan should set _main_event_loop for cross-thread publishing."""
        assert hasattr(nexus_fs, "_main_event_loop")
        assert nexus_fs._main_event_loop is not None
        assert nexus_fs._main_event_loop.is_running()


# ============================================================================
# Test: Direct event bus publish (bypasses threading complexity)
# ============================================================================


class TestDirectPublish:
    """Test direct event bus publish → NATS receive."""

    def test_direct_publish_received(self, client, nexus_fs):
        """Events published directly to bus should appear in NATS stream."""
        from nexus.core.event_bus import FileEvent, FileEventType

        unique_path = f"/e2e-nats-test/direct-{uuid.uuid4().hex[:8]}.txt"
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path=unique_path,
            zone_id="default",
        )

        # Publish directly to NATS via the bus (schedule on main loop)
        future = asyncio.run_coroutine_threadsafe(
            nexus_fs._event_bus.publish(event),
            nexus_fs._main_event_loop,
        )
        seq = future.result(timeout=10)
        assert isinstance(seq, int)
        assert seq > 0

        # Verify event in stream
        found = _run_async(_find_event_in_nats(unique_path, "file_write"))
        assert found, f"Direct publish event for {unique_path} not found in NATS"


# ============================================================================
# Test: File writes via RPC trigger NATS events
# ============================================================================


class TestFileWriteEvents:
    """Test that RPC file operations produce events in NATS JetStream."""

    def test_write_file_via_rpc(self, client, api_key):
        """Write a file via RPC and verify success."""
        status, body = _rpc_call(
            client,
            "write",
            {"path": "/e2e-nats-test/hello.txt", "content": "SGVsbG8gTkFUUw=="},
            api_key=api_key,
        )
        assert status == 200, f"Write returned HTTP {status}: {body}"
        assert body.get("error") is None, f"Write failed: {body}"

    def test_write_triggers_nats_event(self, client, api_key):
        """Write a file via RPC and verify NATS event was published."""
        unique_path = f"/e2e-nats-test/rpc-event-{uuid.uuid4().hex[:8]}.txt"

        status, body = _rpc_call(
            client,
            "write",
            {"path": unique_path, "content": "dGVzdA=="},
            api_key=api_key,
        )
        assert status == 200, f"Write returned HTTP {status}: {body}"
        assert body.get("error") is None, f"Write failed: {body}"

        # Give event time to propagate (published async from thread pool → main loop)
        time.sleep(3.0)

        found = _run_async(_find_event_in_nats(unique_path, "file_write"))
        assert found, f"Event for {unique_path} not found in NATS stream"

    def test_mkdir_triggers_nats_event(self, client, api_key):
        """mkdir via RPC should publish a dir_create event."""
        unique_dir = f"/e2e-nats-test/dir-{uuid.uuid4().hex[:8]}"

        # Ensure parent /e2e-nats-test exists (mkdir_p creates intermediate dirs)
        status, body = _rpc_call(
            client,
            "mkdir",
            {"path": unique_dir, "parents": True},
            api_key=api_key,
        )
        assert status == 200, f"mkdir returned HTTP {status}: {body}"
        assert body.get("error") is None, f"mkdir failed: {body}"

        time.sleep(3.0)

        found = _run_async(_find_event_in_nats(unique_dir, "dir_create"))
        assert found, f"dir_create event for {unique_dir} not found in NATS"

    def test_delete_triggers_nats_event(self, client, api_key):
        """delete via RPC should publish a file_delete event."""
        unique_path = f"/e2e-nats-test/del-{uuid.uuid4().hex[:8]}.txt"

        # Create file first
        status, _ = _rpc_call(
            client,
            "write",
            {"path": unique_path, "content": "dGVzdA=="},
            api_key=api_key,
        )
        assert status == 200
        time.sleep(1.0)

        # Delete it
        status, body = _rpc_call(
            client,
            "delete",
            {"path": unique_path},
            api_key=api_key,
        )
        assert status == 200, f"delete returned HTTP {status}: {body}"
        assert body.get("error") is None, f"delete failed: {body}"

        time.sleep(3.0)

        found = _run_async(_find_event_in_nats(unique_path, "file_delete"))
        assert found, f"file_delete event for {unique_path} not found in NATS"


# ============================================================================
# Test: Durable consumer receives events
# ============================================================================


class TestDurableSubscriber:
    """Test durable consumer subscription via NATS JetStream."""

    def test_durable_consumer_receives_events(self, client, nexus_fs):
        """A durable consumer should receive events from direct NexusFS writes."""
        import nats as nats_lib

        from nexus.core.event_bus import FileEvent, FileEventType

        unique_path = f"/e2e-nats-test/durable-{uuid.uuid4().hex[:8]}.txt"

        async def _test():
            nc = await nats_lib.connect(NATS_URL)
            js = nc.jetstream()

            consumer_name = f"e2e-{uuid.uuid4().hex[:8]}"
            from nats.js.api import ConsumerConfig, DeliverPolicy

            sub = await js.pull_subscribe(
                "nexus.events.>",
                durable=consumer_name,
                config=ConsumerConfig(
                    durable_name=consumer_name,
                    deliver_policy=DeliverPolicy.NEW,
                ),
            )

            # Publish event directly to NATS via bus (using main loop)
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path=unique_path,
                zone_id="default",
            )
            future = asyncio.run_coroutine_threadsafe(
                nexus_fs._event_bus.publish(event),
                nexus_fs._main_event_loop,
            )
            future.result(timeout=10)

            await asyncio.sleep(2.0)

            from nats.errors import TimeoutError as NatsTimeoutError

            found = False
            for _ in range(10):
                try:
                    msgs = await sub.fetch(batch=10, timeout=3)
                    for msg in msgs:
                        data = json.loads(msg.data)
                        if data.get("path") == unique_path:
                            found = True
                            await msg.ack()
                            break
                    if found:
                        break
                except NatsTimeoutError:
                    continue

            await sub.unsubscribe()
            await nc.close()
            return found

        found = _run_async(_test())
        assert found, f"Durable consumer did not receive event for {unique_path}"


# ============================================================================
# Test: Permission enforcement
# ============================================================================


class TestPermissionEnforcement:
    """Test that auth is enforced and unauthorized writes produce no events."""

    def test_unauthenticated_write_rejected(self, client):
        """Write without API key should be rejected with 401."""
        status, body = _rpc_call(
            client,
            "write",
            {"path": "/e2e-nats-test/unauthed.txt", "content": "dGVzdA=="},
            api_key=None,
        )
        assert status == 401, f"Expected 401 for unauthenticated write, got {status}: {body}"

    def test_wrong_api_key_rejected(self, client):
        """Write with wrong API key should be rejected with 401."""
        status, body = _rpc_call(
            client,
            "write",
            {"path": "/e2e-nats-test/wrong-key.txt", "content": "dGVzdA=="},
            api_key="wrong-key-12345",
        )
        assert status == 401, f"Expected 401 for wrong API key, got {status}: {body}"

    def test_authorized_write_succeeds(self, client, api_key):
        """Write with correct API key should succeed."""
        unique_path = f"/e2e-nats-test/authed-{uuid.uuid4().hex[:8]}.txt"
        status, body = _rpc_call(
            client,
            "write",
            {"path": unique_path, "content": "dGVzdA=="},
            api_key=api_key,
        )
        assert status == 200, f"Expected 200 for authorized write, got {status}: {body}"
        assert body.get("error") is None

    def test_no_event_for_rejected_write(self, client):
        """A rejected write should NOT produce a NATS event."""
        unique_path = f"/e2e-nats-test/no-event-{uuid.uuid4().hex[:8]}.txt"

        # Attempt unauthorized write
        status, _ = _rpc_call(
            client,
            "write",
            {"path": unique_path, "content": "dGVzdA=="},
            api_key=None,
        )
        assert status == 401

        time.sleep(2.0)

        found = _run_async(_find_event_in_nats(unique_path, timeout=3.0))
        assert not found, f"Unexpected event for rejected write to {unique_path}"


# ============================================================================
# Test: Event bus stats
# ============================================================================


class TestEventBusStats:
    """Test NATS event bus statistics."""

    def test_get_stats(self, client, nexus_fs):
        """NATS event bus should return meaningful stats."""
        future = asyncio.run_coroutine_threadsafe(
            nexus_fs._event_bus.get_stats(),
            nexus_fs._main_event_loop,
        )
        stats = future.result(timeout=10)

        assert stats["backend"] == "nats_jetstream"
        assert stats["status"] == "running"
        assert "stream" in stats
        assert stats["stream"]["messages"] >= 0
        assert stats["nats_url"] == NATS_URL


# ============================================================================
# Test: Event deduplication
# ============================================================================


class TestDeduplication:
    """Test JetStream message deduplication end-to-end."""

    def test_duplicate_events_deduplicated(self, client, nexus_fs):
        """Publishing the same event_id twice should be deduplicated."""
        from nexus.core.event_bus import FileEvent, FileEventType

        dedup_id = f"dedup-e2e-{uuid.uuid4().hex[:8]}"
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path=f"/e2e-nats-test/dedup-{dedup_id}.txt",
            zone_id="default",
            event_id=dedup_id,
        )

        # Publish twice via main loop
        f1 = asyncio.run_coroutine_threadsafe(
            nexus_fs._event_bus.publish(event),
            nexus_fs._main_event_loop,
        )
        seq1 = f1.result(timeout=10)

        f2 = asyncio.run_coroutine_threadsafe(
            nexus_fs._event_bus.publish(event),
            nexus_fs._main_event_loop,
        )
        seq2 = f2.result(timeout=10)

        # Same sequence = deduplicated
        assert seq1 == seq2, f"Expected dedup (same seq), got seq1={seq1}, seq2={seq2}"
