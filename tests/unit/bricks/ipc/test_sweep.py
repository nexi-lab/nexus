"""Unit tests for TTLSweeper."""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from nexus.bricks.ipc.conventions import dead_letter_path, inbox_path
from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
from nexus.bricks.ipc.provisioning import AgentProvisioner
from nexus.bricks.ipc.sweep import TTLSweeper
from nexus.cache.inmemory import InMemoryCacheStore

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
        # With shared dead_letter_message, we get both the message and .reason.json
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 1

    @pytest.mark.asyncio
    async def test_sweep_expired_message_has_reason_sidecar(self, vfs: InMemoryVFS) -> None:
        """Sweeper should write .reason.json sidecar (Issue #3197, shared lifecycle)."""
        await _provision_agent(vfs, "agent:bob")
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        msg = _make_message("msg_with_reason", old_ts, ttl_seconds=60)
        filename = f"20200101T000000_{msg.id}.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        await vfs.sys_write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE)
        await sweeper.sweep_once()

        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        reason_files = [f for f in dl_files if f.endswith(".reason.json")]
        assert len(reason_files) == 1

        # Verify reason content
        reason_path = f"{dead_letter_path('agent:bob')}/{reason_files[0]}"
        reason_data = json.loads(await vfs.sys_read(reason_path, ZONE))
        assert reason_data["reason"] == "ttl_expired"
        assert "sweeper" in reason_data["detail"]

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
        await vfs.sys_mkdir("/agents", ZONE)

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

        # Sweep interval = 60s, filename timestamp = now -> should skip
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

    @pytest.mark.asyncio
    async def test_sweep_corrupt_file_does_not_crash(self, vfs: InMemoryVFS) -> None:
        """Issue #3197 (12A): Corrupt but readable files should not crash the sweep."""
        await _provision_agent(vfs, "agent:bob")
        # Write corrupt data that is readable but not valid JSON
        filename = "20200101T000000_msg_corrupt.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        await vfs.sys_write(msg_path, b"not valid json {{{", ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE)
        expired_count = await sweeper.sweep_once()

        # Should not crash, should not count as expired
        assert expired_count == 0
        # File should still be in inbox (not lost)
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1


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
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 1


# ===========================================================================
# Event-driven sweeper tests (#3197 — Issues 3A, 7A, 11A, 13A)
# ===========================================================================


class TestEventDrivenSweeper:
    """Tests for CacheStore pub/sub event-driven TTL sweeping (#3197)."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.fixture
    def cache_store(self) -> InMemoryCacheStore:
        return InMemoryCacheStore()

    @pytest.mark.asyncio
    async def test_pubsub_triggers_sweep(
        self, vfs: InMemoryVFS, cache_store: InMemoryCacheStore
    ) -> None:
        """Pub/sub event should trigger a sweep that catches expired messages."""
        await _provision_agent(vfs, "agent:bob")
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        msg = _make_message("msg_event_sweep", old_ts, ttl_seconds=60)
        filename = f"20200101T000000_{msg.id}.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        await vfs.sys_write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            interval=300,  # Long poll interval — we want event-driven to fire
            cache_store=cache_store,
            debounce_seconds=0.05,  # Short debounce for testing
        )
        await sweeper.start()
        await asyncio.sleep(0.05)  # Let subscriber register

        # Publish TTL schedule event
        await cache_store.publish(
            f"ipc:ttl:schedule:{ZONE}",
            json.dumps({"agent_id": "agent:bob", "msg_id": "msg_event_sweep"}).encode(),
        )

        # Wait for debounce + sweep
        await asyncio.sleep(0.3)
        await sweeper.stop()

        # Message should have been swept
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0

    @pytest.mark.asyncio
    async def test_debounce_coalesces_rapid_events(
        self, vfs: InMemoryVFS, cache_store: InMemoryCacheStore
    ) -> None:
        """Multiple rapid pub/sub events should result in a single sweep."""
        await _provision_agent(vfs, "agent:bob")
        # Write 3 expired messages
        for i in range(3):
            old_ts = datetime(2020, 1, 1, tzinfo=UTC)
            msg = _make_message(f"msg_rapid_{i}", old_ts, ttl_seconds=60)
            filename = f"20200101T00000{i}_{msg.id}.json"
            msg_path = f"{inbox_path('agent:bob')}/{filename}"
            await vfs.sys_write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            interval=300,
            cache_store=cache_store,
            debounce_seconds=0.1,
        )
        await sweeper.start()
        await asyncio.sleep(0.05)  # Let subscriber register

        # Publish 5 rapid events (more than messages — tests coalescing)
        channel = f"ipc:ttl:schedule:{ZONE}"
        for _i in range(5):
            await cache_store.publish(
                channel,
                json.dumps({"agent_id": "agent:bob"}).encode(),
            )

        # Wait for debounce + sweep
        await asyncio.sleep(0.4)
        await sweeper.stop()

        # All 3 expired messages should be swept
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 3

    @pytest.mark.asyncio
    async def test_targeted_sweep_only_sweeps_specified_agent(
        self, vfs: InMemoryVFS, cache_store: InMemoryCacheStore
    ) -> None:
        """Event-driven sweep should only target the agent from the event."""
        await _provision_agent(vfs, "agent:alice")
        await _provision_agent(vfs, "agent:bob")

        # Expired message for alice
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        msg_a = _make_message("msg_alice", old_ts, ttl_seconds=60)
        await vfs.sys_write(
            f"{inbox_path('agent:alice')}/20200101T000000_{msg_a.id}.json",
            msg_a.to_bytes(),
            ZONE,
        )
        # Expired message for bob
        msg_b = _make_message("msg_bob", old_ts, ttl_seconds=60)
        await vfs.sys_write(
            f"{inbox_path('agent:bob')}/20200101T000000_{msg_b.id}.json",
            msg_b.to_bytes(),
            ZONE,
        )

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            interval=300,
            cache_store=cache_store,
            debounce_seconds=0.05,
        )
        await sweeper.start()
        await asyncio.sleep(0.05)  # Let subscriber register

        # Only publish event for alice
        await cache_store.publish(
            f"ipc:ttl:schedule:{ZONE}",
            json.dumps({"agent_id": "agent:alice"}).encode(),
        )

        await asyncio.sleep(0.3)
        await sweeper.stop()

        # Alice's message should be swept
        alice_inbox = await vfs.list_dir(inbox_path("agent:alice"), ZONE)
        assert len(alice_inbox) == 0

        # Bob's message should still be in inbox (not targeted by event)
        bob_inbox = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(bob_inbox) == 1

    @pytest.mark.asyncio
    async def test_fallback_poll_catches_missed_events(
        self, vfs: InMemoryVFS, cache_store: InMemoryCacheStore
    ) -> None:
        """Fallback poll should catch expired messages even without pub/sub events."""
        await _provision_agent(vfs, "agent:bob")
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        msg = _make_message("msg_poll_fallback", old_ts, ttl_seconds=60)
        filename = f"20200101T000000_{msg.id}.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        await vfs.sys_write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            interval=0.05,  # Short poll interval for testing
            cache_store=cache_store,
        )
        await sweeper.start()

        # Don't publish any events — rely on poll fallback
        await asyncio.sleep(0.15)
        await sweeper.stop()

        # Poll fallback should have caught it
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
