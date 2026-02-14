"""Unit tests for TTLSweeper."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from nexus.ipc.conventions import dead_letter_path, inbox_path
from nexus.ipc.envelope import MessageEnvelope, MessageType
from nexus.ipc.provisioning import AgentProvisioner
from nexus.ipc.sweep import TTLSweeper

from .fakes import InMemoryVFS

ZONE = "test-zone"


async def _provision_agent(vfs: InMemoryVFS, agent_id: str) -> None:
    """Provision agent directories using AgentProvisioner (DRY)."""
    provisioner = AgentProvisioner(vfs, zone_id=ZONE)
    await provisioner.provision(agent_id)


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

    @pytest.mark.asyncio
    async def test_sweep_skips_recent_messages_by_filename(self, vfs: InMemoryVFS) -> None:
        """P1: Messages with recent timestamps in filename are skipped."""
        await _provision_agent(vfs, "agent:bob")
        # Create an expired message with a recent-looking filename
        now = datetime.now(UTC)
        msg = _make_message("msg_recent_filename", now, ttl_seconds=1)
        # Use current timestamp in filename — sweeper should skip this
        filename = f"{now.strftime('%Y%m%dT%H%M%S')}_{msg.id}.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        await vfs.write(msg_path, msg.to_bytes(), ZONE)

        # Sweep interval = 60s, filename timestamp = now → should skip
        sweeper = TTLSweeper(vfs, zone_id=ZONE, interval=60)
        expired_count = await sweeper.sweep_once()

        assert expired_count == 0  # Skipped due to recent filename
        # Message should still be in inbox
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1

    @pytest.mark.asyncio
    async def test_sweep_does_not_skip_old_filenames(self, vfs: InMemoryVFS) -> None:
        """P1: Messages with old timestamps in filename are NOT skipped."""
        await _provision_agent(vfs, "agent:bob")
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        msg = _make_message("msg_old_filename", old_ts, ttl_seconds=60)
        filename = f"20200101T000000_{msg.id}.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        await vfs.write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, interval=60)
        expired_count = await sweeper.sweep_once()

        assert expired_count == 1


class TestTTLSweeperLifecycle:
    """T4: Tests for sweeper start/stop/restart lifecycle."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_start_creates_background_task(self, vfs: InMemoryVFS) -> None:
        sweeper = TTLSweeper(vfs, zone_id=ZONE, interval=0.05)
        assert sweeper._task is None

        await sweeper.start()
        assert sweeper._running is True
        assert sweeper._task is not None

        await sweeper.stop()
        assert sweeper._running is False
        assert sweeper._task is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, vfs: InMemoryVFS) -> None:
        sweeper = TTLSweeper(vfs, zone_id=ZONE, interval=0.05)
        await sweeper.start()
        first_task = sweeper._task

        await sweeper.start()  # Second start should be a no-op
        assert sweeper._task is first_task

        await sweeper.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(self, vfs: InMemoryVFS) -> None:
        sweeper = TTLSweeper(vfs, zone_id=ZONE)
        await sweeper.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_restart_after_stop(self, vfs: InMemoryVFS) -> None:
        sweeper = TTLSweeper(vfs, zone_id=ZONE, interval=0.05)

        await sweeper.start()
        await sweeper.stop()
        assert sweeper._task is None

        # Should be able to start again
        await sweeper.start()
        assert sweeper._running is True
        assert sweeper._task is not None

        await sweeper.stop()

    @pytest.mark.asyncio
    async def test_sweep_loop_runs_periodically(self, vfs: InMemoryVFS) -> None:
        """Verify the background loop actually sweeps expired messages."""
        await _provision_agent(vfs, "agent:bob")
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        msg = _make_message("msg_loop_test", old_ts, ttl_seconds=1)
        filename = f"20200101T000000_{msg.id}.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        await vfs.write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, interval=0.05)
        await sweeper.start()

        # Give the sweep loop time to run at least once
        await asyncio.sleep(0.15)
        await sweeper.stop()

        # Message should have been swept to dead_letter
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        assert len(dl_files) == 1
