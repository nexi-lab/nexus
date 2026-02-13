"""Unit tests for TTLSweeper."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nexus.ipc.conventions import dead_letter_path, inbox_path
from nexus.ipc.envelope import MessageEnvelope, MessageType
from nexus.ipc.sweep import TTLSweeper

from .fakes import InMemoryVFS

ZONE = "test-zone"


async def _provision_agent(vfs: InMemoryVFS, agent_id: str) -> None:
    """Helper to create agent directories."""
    await vfs.mkdir("/agents", ZONE)
    await vfs.mkdir(f"/agents/{agent_id}", ZONE)
    await vfs.mkdir(f"/agents/{agent_id}/inbox", ZONE)
    await vfs.mkdir(f"/agents/{agent_id}/dead_letter", ZONE)


def _make_message(
    msg_id: str,
    timestamp: datetime,
    ttl_seconds: int | None = None,
) -> MessageEnvelope:
    return MessageEnvelope(
        sender="agent:sender",
        recipient="agent:receiver",
        type=MessageType.TASK,
        id=msg_id,
        timestamp=timestamp,
        ttl_seconds=ttl_seconds,
    )


class TestTTLSweeper:
    """Tests for TTL expiry background sweep."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_sweep_expired_message(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        msg = _make_message("msg_expired", old_ts, ttl_seconds=60)
        filename = f"20200101T000000_{msg.id}.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        await vfs.write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE)
        expired_count = await sweeper.sweep_once()

        assert expired_count == 1
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        assert len(dl_files) == 1

    @pytest.mark.asyncio
    async def test_sweep_skips_valid_messages(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        now = datetime.now(UTC)
        msg = _make_message("msg_valid", now, ttl_seconds=3600)
        filename = f"{now.strftime('%Y%m%dT%H%M%S')}_{msg.id}.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        await vfs.write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE)
        expired_count = await sweeper.sweep_once()

        assert expired_count == 0
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1

    @pytest.mark.asyncio
    async def test_sweep_skips_messages_without_ttl(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        msg = _make_message("msg_no_ttl", old_ts)  # No TTL
        filename = f"20200101T000000_{msg.id}.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        await vfs.write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE)
        expired_count = await sweeper.sweep_once()

        assert expired_count == 0  # No TTL = never expires

    @pytest.mark.asyncio
    async def test_sweep_multiple_agents(self, vfs: InMemoryVFS) -> None:
        for agent_id in ["agent:alice", "agent:bob"]:
            await _provision_agent(vfs, agent_id)
            old_ts = datetime(2020, 1, 1, tzinfo=UTC)
            msg = _make_message(f"msg_{agent_id}", old_ts, ttl_seconds=1)
            filename = f"20200101T000000_msg_{agent_id}.json"
            msg_path = f"{inbox_path(agent_id)}/{filename}"
            await vfs.write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE)
        expired_count = await sweeper.sweep_once()

        assert expired_count == 2

    @pytest.mark.asyncio
    async def test_sweep_empty_agents(self, vfs: InMemoryVFS) -> None:
        await vfs.mkdir("/agents", ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE)
        expired_count = await sweeper.sweep_once()
        assert expired_count == 0
