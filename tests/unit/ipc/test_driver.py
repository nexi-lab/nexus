"""Unit tests for IPCVFSDriver — VFS Backend for /agents/ mount point.

TDD approach: tests written first (Phase 2.3), then implementation.
IPCVFSDriver bridges the CAS-oriented Backend ABC with path-oriented
IPC storage, exposing agent messaging via the VFS Router.
"""

from __future__ import annotations

import json

import pytest

from nexus.ipc.conventions import (
    AGENTS_ROOT,
    inbox_path,
)
from nexus.ipc.driver import IPCVFSDriver
from nexus.ipc.envelope import MessageEnvelope, MessageType

from .fakes import InMemoryStorageDriver

ZONE = "test-zone"


def _make_envelope(
    sender: str = "agent:alice",
    recipient: str = "agent:bob",
    msg_id: str = "msg_test001",
) -> MessageEnvelope:
    return MessageEnvelope(
        sender=sender,
        recipient=recipient,
        type=MessageType.TASK,
        id=msg_id,
        payload={"action": "test"},
    )


async def _provision(storage: InMemoryStorageDriver, agent_id: str) -> None:
    """Provision agent directories in storage."""
    root = f"{AGENTS_ROOT}/{agent_id}"
    await storage.mkdir(root, ZONE)
    for sub in ("inbox", "outbox", "processed", "dead_letter"):
        await storage.mkdir(f"{root}/{sub}", ZONE)
    card = json.dumps({"name": agent_id, "status": "connected"}).encode()
    await storage.write(f"{root}/AGENT.json", card, ZONE)


class TestIPCVFSDriverProperties:
    """Tests for driver identity and capability flags."""

    def test_name_is_ipc(self) -> None:
        driver = IPCVFSDriver(storage=InMemoryStorageDriver())
        assert driver.name == "ipc"

    def test_has_virtual_filesystem(self) -> None:
        driver = IPCVFSDriver(storage=InMemoryStorageDriver())
        assert driver.has_virtual_filesystem is True

    def test_supports_rename(self) -> None:
        driver = IPCVFSDriver(storage=InMemoryStorageDriver())
        assert driver.supports_rename is True

    def test_is_connected(self) -> None:
        driver = IPCVFSDriver(storage=InMemoryStorageDriver())
        assert driver.is_connected is True


class TestIPCVFSDriverListDir:
    """Tests for directory listing via VFS."""

    @pytest.fixture
    def storage(self) -> InMemoryStorageDriver:
        return InMemoryStorageDriver()

    @pytest.mark.asyncio
    async def test_list_agents_root(self, storage: InMemoryStorageDriver) -> None:
        await _provision(storage, "agent:alice")
        await _provision(storage, "agent:bob")
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        entries = driver.list_dir(AGENTS_ROOT)
        assert sorted(entries) == sorted(["agent:alice", "agent:bob"])

    @pytest.mark.asyncio
    async def test_list_agent_subdirs(self, storage: InMemoryStorageDriver) -> None:
        await _provision(storage, "agent:bob")
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        entries = driver.list_dir(f"{AGENTS_ROOT}/agent:bob")
        assert "inbox" in entries
        assert "outbox" in entries
        assert "AGENT.json" in entries

    @pytest.mark.asyncio
    async def test_list_inbox_messages(self, storage: InMemoryStorageDriver) -> None:
        await _provision(storage, "agent:bob")
        msg = _make_envelope()
        msg_filename = f"20260212T100000_{msg.id}.json"
        await storage.write(
            f"{inbox_path('agent:bob')}/{msg_filename}",
            msg.to_bytes(),
            ZONE,
        )
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        entries = driver.list_dir(inbox_path("agent:bob"))
        assert msg_filename in entries

    @pytest.mark.asyncio
    async def test_list_nonexistent_raises(self, storage: InMemoryStorageDriver) -> None:
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        with pytest.raises(FileNotFoundError):
            driver.list_dir("/agents/nonexistent")


class TestIPCVFSDriverReadContent:
    """Tests for reading messages/agent cards via CAS interface."""

    @pytest.fixture
    def storage(self) -> InMemoryStorageDriver:
        return InMemoryStorageDriver()

    @pytest.mark.asyncio
    async def test_read_agent_card(self, storage: InMemoryStorageDriver) -> None:
        await _provision(storage, "agent:bob")
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        # read_content takes a "path" (virtual filesystem mode)
        response = driver.read_content(f"{AGENTS_ROOT}/agent:bob/AGENT.json")
        assert response.success
        card = json.loads(response.data)
        assert card["name"] == "agent:bob"

    @pytest.mark.asyncio
    async def test_read_inbox_message(self, storage: InMemoryStorageDriver) -> None:
        await _provision(storage, "agent:bob")
        msg = _make_envelope()
        msg_path = f"{inbox_path('agent:bob')}/20260212T100000_{msg.id}.json"
        await storage.write(msg_path, msg.to_bytes(), ZONE)
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        response = driver.read_content(msg_path)
        assert response.success
        restored = MessageEnvelope.from_bytes(response.data)
        assert restored.id == msg.id

    @pytest.mark.asyncio
    async def test_read_nonexistent_returns_error(self, storage: InMemoryStorageDriver) -> None:
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        response = driver.read_content("/agents/nobody/inbox/ghost.json")
        assert not response.success
        assert response.error_code == 404


class TestIPCVFSDriverWriteContent:
    """Tests for writing messages via CAS interface."""

    @pytest.fixture
    def storage(self) -> InMemoryStorageDriver:
        return InMemoryStorageDriver()

    @pytest.mark.asyncio
    async def test_write_to_inbox(self, storage: InMemoryStorageDriver) -> None:
        await _provision(storage, "agent:bob")
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)
        msg = _make_envelope()

        response = driver.write_content(msg.to_bytes())
        assert response.success
        # write_content returns the content hash (path in virtual FS mode)
        assert response.data is not None

    @pytest.mark.asyncio
    async def test_write_agent_card(self, storage: InMemoryStorageDriver) -> None:
        await _provision(storage, "agent:bob")
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)
        card = json.dumps({"name": "agent:bob", "status": "idle"}).encode()

        # Writing to a specific path (via write_path helper)
        response = driver.write_path(
            f"{AGENTS_ROOT}/agent:bob/AGENT.json", card
        )
        assert response.success

        # Verify it was written
        read_resp = driver.read_content(f"{AGENTS_ROOT}/agent:bob/AGENT.json")
        assert read_resp.success
        assert json.loads(read_resp.data)["status"] == "idle"


class TestIPCVFSDriverMkdir:
    """Tests for directory creation."""

    @pytest.fixture
    def storage(self) -> InMemoryStorageDriver:
        return InMemoryStorageDriver()

    @pytest.mark.asyncio
    async def test_mkdir_agent_root(self, storage: InMemoryStorageDriver) -> None:
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        response = driver.mkdir(f"{AGENTS_ROOT}/agent:new", parents=True, exist_ok=True)
        assert response.success

        # Verify directory exists
        is_dir = driver.is_directory(f"{AGENTS_ROOT}/agent:new")
        assert is_dir.success
        assert is_dir.data is True

    @pytest.mark.asyncio
    async def test_mkdir_idempotent_with_exist_ok(self, storage: InMemoryStorageDriver) -> None:
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        driver.mkdir(f"{AGENTS_ROOT}/agent:bob", parents=True, exist_ok=True)
        response = driver.mkdir(f"{AGENTS_ROOT}/agent:bob", parents=True, exist_ok=True)
        assert response.success


class TestIPCVFSDriverIsDirectory:
    """Tests for directory existence checks."""

    @pytest.fixture
    def storage(self) -> InMemoryStorageDriver:
        return InMemoryStorageDriver()

    @pytest.mark.asyncio
    async def test_existing_dir(self, storage: InMemoryStorageDriver) -> None:
        await _provision(storage, "agent:bob")
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        resp = driver.is_directory(inbox_path("agent:bob"))
        assert resp.success
        assert resp.data is True

    @pytest.mark.asyncio
    async def test_file_is_not_directory(self, storage: InMemoryStorageDriver) -> None:
        await _provision(storage, "agent:bob")
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        resp = driver.is_directory(f"{AGENTS_ROOT}/agent:bob/AGENT.json")
        assert resp.success
        assert resp.data is False

    @pytest.mark.asyncio
    async def test_nonexistent_path(self, storage: InMemoryStorageDriver) -> None:
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        resp = driver.is_directory("/agents/ghost")
        assert resp.success
        assert resp.data is False


class TestIPCVFSDriverDeleteContent:
    """Tests for delete (move to dead_letter)."""

    @pytest.fixture
    def storage(self) -> InMemoryStorageDriver:
        return InMemoryStorageDriver()

    @pytest.mark.asyncio
    async def test_delete_returns_ok(self, storage: InMemoryStorageDriver) -> None:
        """Delete should succeed (IPC doesn't hard-delete, just a no-op for CAS)."""
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        response = driver.delete_content("some_hash")
        assert response.success


class TestIPCVFSDriverContentExists:
    """Tests for content/path existence checks."""

    @pytest.fixture
    def storage(self) -> InMemoryStorageDriver:
        return InMemoryStorageDriver()

    @pytest.mark.asyncio
    async def test_existing_file(self, storage: InMemoryStorageDriver) -> None:
        await _provision(storage, "agent:bob")
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        resp = driver.content_exists(f"{AGENTS_ROOT}/agent:bob/AGENT.json")
        assert resp.success
        assert resp.data is True

    @pytest.mark.asyncio
    async def test_nonexistent_file(self, storage: InMemoryStorageDriver) -> None:
        driver = IPCVFSDriver(storage=storage, zone_id=ZONE)

        resp = driver.content_exists("/agents/ghost/file.json")
        assert resp.success
        assert resp.data is False


class TestIPCVFSDriverReBAC:
    """Tests for ReBAC object type mapping."""

    def test_object_type_is_ipc_message(self) -> None:
        driver = IPCVFSDriver(storage=InMemoryStorageDriver())
        assert driver.get_object_type("agent:bob/inbox/msg.json") == "ipc:message"

    def test_object_type_agent_card(self) -> None:
        driver = IPCVFSDriver(storage=InMemoryStorageDriver())
        assert driver.get_object_type("agent:bob/AGENT.json") == "ipc:agent"

    def test_object_type_directory(self) -> None:
        driver = IPCVFSDriver(storage=InMemoryStorageDriver())
        assert driver.get_object_type("agent:bob/inbox") == "ipc:directory"
