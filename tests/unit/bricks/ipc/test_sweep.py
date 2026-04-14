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
        vfs.write(msg_path, msg.to_bytes(), ZONE)

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
        vfs.sys_write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE)
        await sweeper.sweep_once()

        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        reason_files = [f for f in dl_files if f.endswith(".reason.json")]
        assert len(reason_files) == 1

        # Verify reason content
        reason_path = f"{dead_letter_path('agent:bob')}/{reason_files[0]}"
        reason_data = json.loads(vfs.sys_read(reason_path, ZONE))
        assert reason_data["reason"] == "ttl_expired"
        assert "sweeper" in reason_data["detail"]

    @pytest.mark.asyncio
    async def test_sweep_skips_valid_messages(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        now = datetime.now(UTC)
        msg = _make_message("msg_valid", now, ttl_seconds=3600)
        filename = f"{now.strftime('%Y%m%dT%H%M%S')}_{msg.id}.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        vfs.write(msg_path, msg.to_bytes(), ZONE)

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
        vfs.write(msg_path, msg.to_bytes(), ZONE)

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
            vfs.write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE)
        expired_count = await sweeper.sweep_once()

        assert expired_count == 2

    @pytest.mark.asyncio
    async def test_sweep_empty_agents(self, vfs: InMemoryVFS) -> None:
        vfs.mkdir("/agents", ZONE)

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
        vfs.write(msg_path, msg.to_bytes(), ZONE)

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
        vfs.write(msg_path, msg.to_bytes(), ZONE)

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
        vfs.sys_write(msg_path, b"not valid json {{{", ZONE)

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
        vfs.write(msg_path, msg.to_bytes(), ZONE)

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
        vfs.sys_write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            interval=300,  # Long poll interval — we want event-driven to fire
            cache_store=cache_store,
            debounce_seconds=0.05,  # Short debounce for testing
        )
        await sweeper.start()
        await asyncio.sleep(0.05)  # Let subscriber register

        # Publish TTL schedule event (already expired — expires_at in the past)
        await cache_store.publish(
            f"ipc:ttl:schedule:{ZONE}",
            json.dumps(
                {
                    "agent_id": "agent:bob",
                    "msg_id": "msg_event_sweep",
                    "expires_at": old_ts.timestamp() + 60,  # already expired
                }
            ).encode(),
        )

        # Wait for expiry timer + sweep
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
            vfs.sys_write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            interval=300,
            cache_store=cache_store,
            debounce_seconds=0.05,
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
        await asyncio.sleep(0.2)
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
        vfs.sys_write(
            f"{inbox_path('agent:alice')}/20200101T000000_{msg_a.id}.json",
            msg_a.to_bytes(),
            ZONE,
        )
        # Expired message for bob
        msg_b = _make_message("msg_bob", old_ts, ttl_seconds=60)
        vfs.sys_write(
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
        vfs.sys_write(msg_path, msg.to_bytes(), ZONE)

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

    @pytest.mark.asyncio
    async def test_sweeper_waits_until_expires_at(
        self, vfs: InMemoryVFS, cache_store: InMemoryCacheStore
    ) -> None:
        """Sweeper should wait until expires_at before sweeping, not sweep immediately."""
        import time

        await _provision_agent(vfs, "agent:bob")

        # Create a message with TTL=1s. The sweeper receives an event with
        # expires_at=now+1s, should sleep until that time, then sweep.
        now = datetime.now(UTC)
        msg = _make_message("msg_future_expiry", now, ttl_seconds=1)
        filename = f"{now.strftime('%Y%m%dT%H%M%S')}_{msg.id}.json"
        msg_path = f"{inbox_path('agent:bob')}/{filename}"
        vfs.sys_write(msg_path, msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            interval=300,  # Long poll — only event-driven matters
            cache_store=cache_store,
        )
        await sweeper.start()
        await asyncio.sleep(0.05)  # Let subscriber register

        # Publish event with expires_at = now + 1s
        expires_at = time.time() + 1.0
        await cache_store.publish(
            f"ipc:ttl:schedule:{ZONE}",
            json.dumps(
                {
                    "agent_id": "agent:bob",
                    "msg_id": msg.id,
                    "expires_at": expires_at,
                }
            ).encode(),
        )

        # Check at 0.3s — message should NOT be swept yet
        await asyncio.sleep(0.3)
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1, "Message swept too early!"

        # Wait past expires_at + 100ms buffer + processing
        await asyncio.sleep(1.0)
        await sweeper.stop()

        # Now it should be swept
        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0, "Message not swept after expiry!"


# ===========================================================================
# Retention sweeper tests
# ===========================================================================


from nexus.bricks.ipc.conventions import (  # noqa: E402
    dead_letter_archive_path,
    outbox_path,
    processed_path,
)


def _old_filename(msg_id: str, days_ago: int = 10) -> str:
    """Return a filename with a timestamp N days in the past."""
    from datetime import timedelta

    ts = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y%m%dT%H%M%S")
    return f"{ts}_{msg_id}.json"


def _recent_filename(msg_id: str) -> str:
    """Return a filename with a current timestamp."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{ts}_{msg_id}.json"


async def _write_old(
    vfs: InMemoryVFS, path: str, data: bytes, zone: str, days_ago: int = 10
) -> None:
    """Write a file and back-date its mtime so retention logic treats it as old."""
    from datetime import timedelta

    vfs.write(path, data, zone)
    old_mtime = datetime.now(UTC) - timedelta(days=days_ago)
    vfs.set_mtime(path, zone, old_mtime)


class TestStaleInboxDrain:
    """Tests for _drain_stale_inbox: dead consumer relief valve."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_drains_old_no_ttl_message(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        msg = _make_message("msg_stale", datetime(2020, 1, 1, tzinfo=UTC))
        filename = _old_filename("msg_stale", days_ago=10)
        await _write_old(vfs, f"{inbox_path('agent:bob')}/{filename}", msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, inbox_stale_hours=1)
        await sweeper.sweep_once()

        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 1

    @pytest.mark.asyncio
    async def test_drain_reason_sidecar_is_stale_inbox(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        msg = _make_message("msg_stale_reason", datetime(2020, 1, 1, tzinfo=UTC))
        filename = _old_filename("msg_stale_reason", days_ago=10)
        await _write_old(vfs, f"{inbox_path('agent:bob')}/{filename}", msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, inbox_stale_hours=1)
        await sweeper.sweep_once()

        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        reason_files = [f for f in dl_files if f.endswith(".reason.json")]
        assert len(reason_files) == 1
        reason_path = f"{dead_letter_path('agent:bob')}/{reason_files[0]}"
        reason_data = json.loads(vfs.sys_read(reason_path, ZONE))
        assert reason_data["reason"] == "stale_inbox"

    @pytest.mark.asyncio
    async def test_skips_recent_no_ttl_message(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        msg = _make_message("msg_recent", datetime.now(UTC))
        filename = _recent_filename("msg_recent")
        vfs.write(f"{inbox_path('agent:bob')}/{filename}", msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, inbox_stale_hours=24)
        await sweeper.sweep_once()

        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1  # not touched

    @pytest.mark.asyncio
    async def test_skips_old_message_with_ttl(self, vfs: InMemoryVFS) -> None:
        """Messages with TTL are handled by _sweep_agent, not stale drain."""
        await _provision_agent(vfs, "agent:bob")
        msg = _make_message("msg_ttl", datetime(2020, 1, 1, tzinfo=UTC), ttl_seconds=9999999)
        filename = _old_filename("msg_ttl", days_ago=10)
        await _write_old(vfs, f"{inbox_path('agent:bob')}/{filename}", msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, inbox_stale_hours=1)
        await sweeper._drain_stale_inbox("agent:bob")

        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1  # TTL message not touched by stale drain

    @pytest.mark.asyncio
    async def test_drain_uses_atomic_claim_before_reading(self, vfs: InMemoryVFS) -> None:
        """Drain renames the file before reading — concurrent processor gets FileNotFoundError."""
        await _provision_agent(vfs, "agent:bob")
        msg = _make_message("msg_stale_claim", datetime(2020, 1, 1, tzinfo=UTC))
        filename = _old_filename("msg_stale_claim", days_ago=10)
        await _write_old(vfs, f"{inbox_path('agent:bob')}/{filename}", msg.to_bytes(), ZONE)

        # Simulate processor holding the original path by renaming it first
        # (as if processor read and is executing handler).
        # The drain's rename should then fail and skip the message.
        proc_dest = f"{inbox_path('agent:bob')}/{filename}.in_progress"
        await vfs.rename(f"{inbox_path('agent:bob')}/{filename}", proc_dest, ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, inbox_stale_hours=1)
        drained = await sweeper._drain_stale_inbox("agent:bob")

        # Drain should have skipped — processor "owns" it
        assert drained == 0
        assert not any(
            f.endswith(".reason.json") or "drain_" in f
            for f in await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        )

    @pytest.mark.asyncio
    async def test_drain_crash_recovery_restores_claimed_file(self, vfs: InMemoryVFS) -> None:
        """Orphaned .drain_* files (crash mid-claim) are restored on next cycle."""
        await _provision_agent(vfs, "agent:bob")
        inbox = inbox_path("agent:bob")
        # Simulate stale orphaned drain claim
        old_ts = "20200101T000000"
        claimed = f"{inbox}/20200101T000000_msg_stale.json.drain_{old_ts}_aabbccdd"
        msg = _make_message("msg_stale", datetime(2020, 1, 1, tzinfo=UTC))
        vfs.write(claimed, msg.to_bytes(), ZONE)
        vfs.mkdir(inbox, ZONE)  # ensure inbox dir exists (already done by provision)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, inbox_stale_hours=1)
        # Calling _recover_drain_claims directly with current filenames
        filenames = await vfs.list_dir(inbox, ZONE)
        await sweeper._recover_drain_claims(inbox, filenames)

        inbox_files = await vfs.list_dir(inbox, ZONE)
        assert "20200101T000000_msg_stale.json" in inbox_files  # restored
        assert not any(".drain_" in f for f in inbox_files)

    @pytest.mark.asyncio
    async def test_no_drain_when_mtime_unavailable(self, vfs: InMemoryVFS) -> None:
        """When file_mtime() returns None, drain must not act (safe fail)."""
        await _provision_agent(vfs, "agent:bob")
        msg = _make_message("msg_no_mtime", datetime(2020, 1, 1, tzinfo=UTC))
        filename = _old_filename("msg_no_mtime", days_ago=10)
        # Write without a recorded mtime — simulates metastore miss
        path = f"{inbox_path('agent:bob')}/{filename}"
        vfs.write(path, msg.to_bytes(), ZONE)
        vfs._mtimes.pop((path, ZONE), None)  # remove mtime

        sweeper = TTLSweeper(vfs, zone_id=ZONE, inbox_stale_hours=1)
        await sweeper._drain_stale_inbox("agent:bob")

        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1  # safe fail — not drained

    @pytest.mark.asyncio
    async def test_disabled_when_inbox_stale_hours_is_none(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        msg = _make_message("msg_stale", datetime(2020, 1, 1, tzinfo=UTC))
        filename = _old_filename("msg_stale", days_ago=10)
        await _write_old(vfs, f"{inbox_path('agent:bob')}/{filename}", msg.to_bytes(), ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, inbox_stale_hours=None)
        await sweeper._drain_stale_inbox("agent:bob")

        inbox_files = await vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 1  # drain disabled — untouched


class TestPruneDir:
    """Tests for _prune_dir: TTL delete for processed/ and outbox/."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_deletes_old_processed_files(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        proc_path = processed_path("agent:bob")
        filename = _old_filename("msg_proc", days_ago=10)
        await _write_old(vfs, f"{proc_path}/{filename}", b'{"id":"msg_proc"}', ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, processed_retention_days=7)
        await sweeper.sweep_once()

        files = await vfs.list_dir(proc_path, ZONE)
        assert len(files) == 0

    @pytest.mark.asyncio
    async def test_keeps_recent_processed_files(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        proc_path = processed_path("agent:bob")
        filename = _recent_filename("msg_proc_recent")
        vfs.write(f"{proc_path}/{filename}", b'{"id":"msg_proc_recent"}', ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, processed_retention_days=7)
        await sweeper.sweep_once()

        files = await vfs.list_dir(proc_path, ZONE)
        assert len(files) == 1

    @pytest.mark.asyncio
    async def test_deletes_old_outbox_files(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        out_path = outbox_path("agent:bob")
        filename = _old_filename("msg_out", days_ago=10)
        await _write_old(vfs, f"{out_path}/{filename}", b'{"id":"msg_out"}', ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, outbox_retention_days=7)
        await sweeper.sweep_once()

        files = await vfs.list_dir(out_path, ZONE)
        assert len(files) == 0

    @pytest.mark.asyncio
    async def test_disabled_when_retention_is_none(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        proc_path = processed_path("agent:bob")
        filename = _old_filename("msg_proc", days_ago=10)
        await _write_old(vfs, f"{proc_path}/{filename}", b'{"id":"msg_proc"}', ZONE)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, processed_retention_days=None)
        await sweeper._prune_dir("agent:bob", "processed", None)

        files = await vfs.list_dir(proc_path, ZONE)
        assert len(files) == 1  # disabled — untouched


class TestDeadLetterCompaction:
    """Tests for _compact_dead_letter: two-phase JSONL archive."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    async def _write_dl_files(
        self, vfs: InMemoryVFS, agent_id: str, count: int, days_ago: int = 10
    ) -> tuple[list[str], str]:
        """Write `count` dead_letter message + reason sidecar pairs with old mtime."""
        from datetime import timedelta

        dl = dead_letter_path(agent_id)
        filenames = []
        base_ts = datetime.now(UTC) - timedelta(days=days_ago)
        day = base_ts.strftime("%Y%m%d")
        for i in range(count):
            ts = (base_ts + timedelta(seconds=i)).strftime("%Y%m%dT%H%M%S")
            fn = f"{ts}_msg_{i:03d}.json"
            envelope = {"id": f"msg_{i:03d}", "sender": "a", "recipient": "b"}
            reason = {"reason": "handler_error", "detail": f"error {i}"}
            await _write_old(vfs, f"{dl}/{fn}", json.dumps(envelope).encode(), ZONE, days_ago)
            await _write_old(
                vfs, f"{dl}/{fn}.reason.json", json.dumps(reason).encode(), ZONE, days_ago
            )
            filenames.append(fn)
        return filenames, day

    @pytest.mark.asyncio
    async def test_compacts_when_threshold_met(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        filenames, day = await self._write_dl_files(vfs, "agent:bob", count=5, days_ago=10)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            dead_letter_compact_min_files=5,
            dead_letter_compact_delete_originals=True,
            inbox_stale_hours=1,
        )
        archived = await sweeper._compact_dead_letter("agent:bob")

        assert archived == 5
        # Originals deleted
        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        msg_files = [f for f in dl_files if f.endswith(".json") and not f.endswith(".reason.json")]
        assert len(msg_files) == 0
        # Archive segment created
        archive_dir = dead_letter_archive_path("agent:bob")
        archive_files = await vfs.list_dir(archive_dir, ZONE)
        jsonl_files = [f for f in archive_files if f.endswith(".jsonl")]
        assert len(jsonl_files) == 1

    @pytest.mark.asyncio
    async def test_archive_is_valid_jsonl(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        await self._write_dl_files(vfs, "agent:bob", count=5, days_ago=10)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            dead_letter_compact_min_files=5,
            dead_letter_compact_delete_originals=True,
            inbox_stale_hours=1,
        )
        await sweeper._compact_dead_letter("agent:bob")

        archive_dir = dead_letter_archive_path("agent:bob")
        archive_files = await vfs.list_dir(archive_dir, ZONE)
        jsonl_file = next(f for f in archive_files if f.endswith(".jsonl"))
        content = vfs.sys_read(f"{archive_dir}/{jsonl_file}", ZONE)
        lines = [ln for ln in content.splitlines() if ln]
        assert len(lines) == 5
        for line in lines:
            record = json.loads(line)
            assert "file" in record
            assert "envelope" in record
            assert "reason" in record

    @pytest.mark.asyncio
    async def test_skips_when_below_threshold(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        await self._write_dl_files(vfs, "agent:bob", count=3, days_ago=10)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            dead_letter_compact_min_files=50,
            inbox_stale_hours=1,
        )
        archived = await sweeper._compact_dead_letter("agent:bob")

        assert archived == 0
        dl_files = await vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        msg_files = [f for f in dl_files if f.endswith(".json") and not f.endswith(".reason.json")]
        assert len(msg_files) == 3  # untouched

    @pytest.mark.asyncio
    async def test_no_tmp_left_after_successful_compact(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        await self._write_dl_files(vfs, "agent:bob", count=5, days_ago=10)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            dead_letter_compact_min_files=5,
            dead_letter_compact_delete_originals=True,
            inbox_stale_hours=1,
        )
        await sweeper._compact_dead_letter("agent:bob")

        archive_dir = dead_letter_archive_path("agent:bob")
        archive_files = await vfs.list_dir(archive_dir, ZONE)
        tmp_files = [f for f in archive_files if f.endswith(".tmp")]
        assert len(tmp_files) == 0

    @pytest.mark.asyncio
    async def test_crash_recovery_cleans_orphaned_tmp(self, vfs: InMemoryVFS) -> None:
        """Orphaned .tmp files (crash during phase 4) are deleted on next sweep."""
        await _provision_agent(vfs, "agent:bob")
        archive_dir = dead_letter_archive_path("agent:bob")
        vfs.mkdir(archive_dir, ZONE)
        # Simulate an orphaned .tmp (crash left it behind)
        tmp_path = f"{archive_dir}/20200101_20200101T000000.jsonl.tmp"
        await _write_old(vfs, tmp_path, b"orphaned", ZONE)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            dead_letter_compact_min_files=50,  # high threshold → no new compaction
            inbox_stale_hours=1,
        )
        await sweeper._compact_dead_letter("agent:bob")

        archive_files = await vfs.list_dir(archive_dir, ZONE)
        assert not any(f.endswith(".tmp") for f in archive_files)

    @pytest.mark.asyncio
    async def test_crash_recovery_restores_stale_claimed_files_no_archive(
        self, vfs: InMemoryVFS
    ) -> None:
        """Stale .arch_{claim_ts}_{run_id} with no committed archive → file restored."""
        await _provision_agent(vfs, "agent:bob")
        dl = dead_letter_path("agent:bob")
        archive_dir = dead_letter_archive_path("agent:bob")
        vfs.mkdir(archive_dir, ZONE)

        # claim_ts is old (2020) → stale; run_id has no matching archive
        claimed_path = f"{dl}/20200101T000000_msg_000.json.arch_20200101T000000_deadbeef"
        vfs.write(claimed_path, b'{"id":"msg_000"}', ZONE)

        sweeper = TTLSweeper(
            vfs, zone_id=ZONE, dead_letter_compact_min_files=50, inbox_stale_hours=1
        )
        await sweeper._recover_claimed_files(dl, archive_dir)

        dl_files = await vfs.list_dir(dl, ZONE)
        assert "20200101T000000_msg_000.json" in dl_files  # restored
        assert not any(".arch_" in f for f in dl_files)

    @pytest.mark.asyncio
    async def test_crash_recovery_deletes_claimed_file_when_archive_committed(
        self, vfs: InMemoryVFS
    ) -> None:
        """Stale .arch_{claim_ts}_{run_id} with committed archive → deleted."""
        await _provision_agent(vfs, "agent:bob")
        dl = dead_letter_path("agent:bob")
        archive_dir = dead_letter_archive_path("agent:bob")
        vfs.mkdir(archive_dir, ZONE)

        run_id = "deadbeef"
        # Old claim_ts → stale
        claimed_path = f"{dl}/20200101T000000_msg_000.json.arch_20200101T000000_{run_id}"
        vfs.write(claimed_path, b'{"id":"msg_000"}', ZONE)

        # Committed archive for same run_id exists
        archive_path = f"{archive_dir}/20200101_20200101T120000_{run_id}.jsonl"
        vfs.write(archive_path, b'{"file":"20200101T000000_msg_000.json"}\n', ZONE)

        sweeper = TTLSweeper(
            vfs, zone_id=ZONE, dead_letter_compact_min_files=50, inbox_stale_hours=1
        )
        await sweeper._recover_claimed_files(dl, archive_dir)

        dl_files = await vfs.list_dir(dl, ZONE)
        assert not any(".arch_" in f for f in dl_files)
        assert "20200101T000000_msg_000.json" not in dl_files

    @pytest.mark.asyncio
    async def test_crash_recovery_skips_recent_claims_by_claim_ts(self, vfs: InMemoryVFS) -> None:
        """Recent claim_ts in filename → skip (active sweeper), no mtime needed."""
        await _provision_agent(vfs, "agent:bob")
        dl = dead_letter_path("agent:bob")
        archive_dir = dead_letter_archive_path("agent:bob")
        vfs.mkdir(archive_dir, ZONE)

        # Use a very recent claim_ts (now)
        recent_ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        claimed_path = f"{dl}/20200101T000000_msg_000.json.arch_{recent_ts}_deadbeef"
        vfs.write(claimed_path, b'{"id":"msg_000"}', ZONE)
        # Deliberately remove mtime to prove filename-based check is used
        vfs._mtimes.pop((claimed_path, ZONE), None)

        sweeper = TTLSweeper(
            vfs, zone_id=ZONE, dead_letter_compact_min_files=50, inbox_stale_hours=1
        )
        await sweeper._recover_claimed_files(dl, archive_dir)

        # Recent claim should NOT be touched
        dl_files = await vfs.list_dir(dl, ZONE)
        assert any(".arch_" in f for f in dl_files)

    @pytest.mark.asyncio
    async def test_prune_archives_fallback_parses_creation_timestamp(
        self, vfs: InMemoryVFS
    ) -> None:
        """Archive pruning fallback correctly parses the creation ts, not the day prefix."""
        await _provision_agent(vfs, "agent:bob")
        archive_dir = dead_letter_archive_path("agent:bob")
        vfs.mkdir(archive_dir, ZONE)

        # Archive for an OLD message day but RECENTLY created — should NOT be pruned.
        # file_mtime returns None → fallback must parse creation ts (20260404T...) not day (20200101).
        recent_archive = f"{archive_dir}/20200101_20260404T120000_abc12345.jsonl"
        vfs.write(recent_archive, b"data", ZONE)
        # Simulate mtime=None by removing it from the fake's mtime store
        vfs._mtimes.pop((recent_archive, ZONE), None)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            dead_letter_compact_min_files=50,
            dead_letter_archive_retention_days=7,
            inbox_stale_hours=1,
        )
        await sweeper._prune_archives(archive_dir, 7)

        archive_files = await vfs.list_dir(archive_dir, ZONE)
        assert recent_archive.split("/")[-1] in archive_files, (
            "Recently created archive was incorrectly pruned using message day instead of creation ts"
        )

    @pytest.mark.asyncio
    async def test_archive_retention_prunes_old_segments(self, vfs: InMemoryVFS) -> None:
        """Archive segments older than retention are deleted (based on archive write time)."""
        await _provision_agent(vfs, "agent:bob")
        archive_dir = dead_letter_archive_path("agent:bob")
        vfs.mkdir(archive_dir, ZONE)
        # Old archive segment — backdate mtime so retention logic treats it as old
        old_seg = f"{archive_dir}/20200101_20200101T000000_deadbeef.jsonl"
        await _write_old(vfs, old_seg, b"old archive data", ZONE, days_ago=40)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            dead_letter_compact_min_files=50,
            dead_letter_archive_retention_days=30,
            inbox_stale_hours=1,
        )
        await sweeper._compact_dead_letter("agent:bob")

        archive_files = await vfs.list_dir(archive_dir, ZONE)
        assert not any(f.endswith(".jsonl") for f in archive_files)

    @pytest.mark.asyncio
    async def test_fresh_archive_of_old_messages_survives_retention(self, vfs: InMemoryVFS) -> None:
        """A just-created archive for an old DLQ day must NOT be pruned immediately.

        Regression test for the bug where _prune_archives used the source-message
        day (old) instead of the archive file's write time (now) to decide deletion.
        """
        await _provision_agent(vfs, "agent:bob")
        # Write old DLQ files (10 days ago by message timestamp AND mtime)
        await self._write_dl_files(vfs, "agent:bob", count=5, days_ago=10)

        sweeper = TTLSweeper(
            vfs,
            zone_id=ZONE,
            dead_letter_compact_min_files=5,
            dead_letter_compact_delete_originals=True,
            dead_letter_archive_retention_days=7,  # 7-day archive retention
            inbox_stale_hours=1,
        )
        # Compact → creates a fresh archive (mtime = now, day prefix = old day)
        archived = await sweeper._compact_dead_letter("agent:bob")
        assert archived == 5

        # Archive should survive because its mtime is now (< 7-day cutoff)
        archive_dir = dead_letter_archive_path("agent:bob")
        archive_files = await vfs.list_dir(archive_dir, ZONE)
        jsonl_files = [f for f in archive_files if f.endswith(".jsonl")]
        assert len(jsonl_files) == 1, "Fresh archive was incorrectly pruned"

    @pytest.mark.asyncio
    async def test_disabled_when_min_files_is_none(self, vfs: InMemoryVFS) -> None:
        await _provision_agent(vfs, "agent:bob")
        await self._write_dl_files(vfs, "agent:bob", count=5, days_ago=10)

        sweeper = TTLSweeper(vfs, zone_id=ZONE, dead_letter_compact_min_files=None)
        archived = await sweeper._compact_dead_letter("agent:bob")

        assert archived == 0
