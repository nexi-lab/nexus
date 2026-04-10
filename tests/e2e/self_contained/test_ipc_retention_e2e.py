"""E2E tests for IPC retention/compaction features via InMemoryStorageDriver.

Tests the full IPC stack: InMemoryStorageDriver provides NexusFS-compatible
interface for IPC provisioning, sweep, and delivery.

Correctness tests use a short retention window + asyncio.sleep
so files genuinely age past the cutoff.

Performance tests measure actual throughput at production-representative scale.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.ipc.conventions import (
    dead_letter_archive_path,
    dead_letter_path,
    inbox_path,
    outbox_path,
    processed_path,
)
from nexus.bricks.ipc.delivery import MessageProcessor, MessageSender
from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
from nexus.bricks.ipc.exceptions import DLQReason
from nexus.bricks.ipc.provisioning import AgentProvisioner
from nexus.bricks.ipc.sweep import (
    TTLSweeper,
)
from nexus.contracts.constants import ROOT_ZONE_ID
from tests.unit.bricks.ipc.fakes import InMemoryStorageDriver

ZONE = ROOT_ZONE_ID

# Retention window used in correctness tests: short enough to not slow tests,
# long enough to be reliable on a busy CI machine.
_TINY_RETENTION_SECS = 1.5  # files written, sleep 2s, then retention fires
_SLEEP_SECS = 2.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def adapter():
    """InMemoryStorageDriver with NexusFS-compatible interface."""
    return InMemoryStorageDriver()


async def _provision(adapter: InMemoryStorageDriver, *agent_ids: str) -> None:
    prov = AgentProvisioner(adapter, zone_id=ZONE)
    for aid in agent_ids:
        await prov.provision(aid)


def _make_msg(msg_id: str, ttl: int | None = None, ts: datetime | None = None) -> MessageEnvelope:
    return MessageEnvelope(
        sender="agent:alice",
        recipient="agent:bob",
        type=MessageType.TASK,
        id=msg_id,
        timestamp=ts or datetime.now(UTC),
        ttl_seconds=ttl,
    )


# ---------------------------------------------------------------------------
# Correctness: file_mtime through real NexusFS
# ---------------------------------------------------------------------------


class TestFileMtimeReal:
    """Verify file_mtime() returns real server-observed timestamps via NexusFS."""

    @pytest.mark.asyncio
    async def test_mtime_non_none_after_write(self, adapter: InMemoryStorageDriver) -> None:
        await _provision(adapter, "agent:bob")
        path = f"{inbox_path('agent:bob')}/msg_mtime_check.json"

        before = datetime.now(UTC)
        adapter.write(path, b'{"id":"x"}', ZONE)
        after = datetime.now(UTC)

        mtime = adapter.file_mtime(path, ZONE)
        assert mtime is not None, "file_mtime must be non-None for LocalConnector"
        assert before <= mtime <= after + timedelta(seconds=2)

    @pytest.mark.asyncio
    async def test_mtime_advances_on_overwrite(self, adapter: InMemoryStorageDriver) -> None:
        await _provision(adapter, "agent:bob")
        path = f"{inbox_path('agent:bob')}/msg_overwrite.json"
        adapter.write(path, b"v1", ZONE)
        mtime1 = adapter.file_mtime(path, ZONE)

        await asyncio.sleep(0.1)
        adapter.write(path, b"v2", ZONE)
        mtime2 = adapter.file_mtime(path, ZONE)

        assert mtime1 is not None and mtime2 is not None
        assert mtime2 >= mtime1

    @pytest.mark.asyncio
    async def test_mtime_none_for_missing_file(self, adapter: InMemoryStorageDriver) -> None:
        mtime = adapter.file_mtime("/agents/nobody/inbox/ghost.json", ZONE)
        assert mtime is None


# ---------------------------------------------------------------------------
# Correctness: processed/ and outbox/ pruning via real NexusFS
# ---------------------------------------------------------------------------


class TestPruneRetentionReal:
    """Write real files, age them past a tiny retention window, verify deletion."""

    @pytest.mark.asyncio
    async def test_prune_processed_deletes_aged_files(self, adapter: InMemoryStorageDriver) -> None:
        await _provision(adapter, "agent:bob")
        proc = processed_path("agent:bob")

        # Write 5 processed files
        for i in range(5):
            path = f"{proc}/20260101T{i:06d}_msg_{i}.json"
            adapter.write(path, b'{"id":"x"}', ZONE)

        # Confirm files exist
        files_before = adapter.list_dir(proc, ZONE)
        assert len(files_before) == 5

        # Age the files: sleep past the tiny retention window
        await asyncio.sleep(_SLEEP_SECS)

        # Prune with retention = _TINY_RETENTION_SECS / 86400 days
        retention_days = _TINY_RETENTION_SECS / 86400
        sweeper = TTLSweeper(adapter, zone_id=ZONE, processed_retention_days=retention_days)
        await sweeper._prune_dir("agent:bob", "processed", retention_days)

        files_after = adapter.list_dir(proc, ZONE)
        assert len(files_after) == 0, (
            f"Expected 0 files after prune, got {len(files_after)}: {files_after}"
        )

    @pytest.mark.asyncio
    async def test_prune_outbox_deletes_aged_files(self, adapter: InMemoryStorageDriver) -> None:
        await _provision(adapter, "agent:bob")
        out = outbox_path("agent:bob")

        for i in range(3):
            adapter.write(f"{out}/20260101T{i:06d}_msg_{i}.json", b'{"id":"x"}', ZONE)

        await asyncio.sleep(_SLEEP_SECS)

        retention_days = _TINY_RETENTION_SECS / 86400
        sweeper = TTLSweeper(adapter, zone_id=ZONE, outbox_retention_days=retention_days)
        await sweeper._prune_dir("agent:bob", "outbox", retention_days)

        files_after = adapter.list_dir(out, ZONE)
        assert len(files_after) == 0

    @pytest.mark.asyncio
    async def test_prune_does_not_delete_fresh_files(self, adapter: InMemoryStorageDriver) -> None:
        await _provision(adapter, "agent:bob")
        proc = processed_path("agent:bob")
        adapter.write(f"{proc}/20260101T000000_fresh.json", b'{"id":"fresh"}', ZONE)

        # Prune immediately (no sleep) — file is fresh, should NOT be deleted
        retention_days = _TINY_RETENTION_SECS / 86400
        sweeper = TTLSweeper(adapter, zone_id=ZONE, processed_retention_days=retention_days)
        await sweeper._prune_dir("agent:bob", "processed", retention_days)

        files = adapter.list_dir(proc, ZONE)
        assert len(files) == 1, "Fresh file must not be pruned"


# ---------------------------------------------------------------------------
# Correctness: stale inbox drain via real NexusFS
# ---------------------------------------------------------------------------


class TestStaleInboxDrainReal:
    """Write no-TTL inbox messages, age them, verify drain fires correctly."""

    @pytest.mark.asyncio
    async def test_drain_dead_letters_aged_no_ttl_message(
        self, adapter: InMemoryStorageDriver
    ) -> None:
        await _provision(adapter, "agent:bob")
        inbox = inbox_path("agent:bob")
        dl = dead_letter_path("agent:bob")

        msg = _make_msg("msg_drain_real")
        path = f"{inbox}/20260101T000000_{msg.id}.json"
        adapter.write(path, msg.to_bytes(), ZONE)

        await asyncio.sleep(_SLEEP_SECS)

        stale_hours = _TINY_RETENTION_SECS / 3600
        sweeper = TTLSweeper(adapter, zone_id=ZONE, inbox_stale_hours=stale_hours)
        drained = await sweeper._drain_stale_inbox("agent:bob")

        assert drained == 1

        # Inbox empty
        inbox_files = adapter.list_dir(inbox, ZONE)
        assert len(inbox_files) == 0

        # Dead letter has the message
        dl_files = adapter.list_dir(dl, ZONE)
        msg_files = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(msg_files) == 1

        # Reason sidecar written with stale_inbox reason
        reason_files = [f for f in dl_files if f.endswith(".reason.json")]
        assert len(reason_files) == 1
        reason_path = f"{dl}/{reason_files[0]}"
        reason = json.loads(adapter.sys_read(reason_path, ZONE))
        assert reason["reason"] == DLQReason.STALE_INBOX

    @pytest.mark.asyncio
    async def test_drain_skips_fresh_messages(self, adapter: InMemoryStorageDriver) -> None:
        await _provision(adapter, "agent:bob")
        inbox = inbox_path("agent:bob")

        msg = _make_msg("msg_fresh")
        path = f"{inbox}/20260101T000000_{msg.id}.json"
        adapter.write(path, msg.to_bytes(), ZONE)

        # No sleep — message is fresh
        stale_hours = _TINY_RETENTION_SECS / 3600
        sweeper = TTLSweeper(adapter, zone_id=ZONE, inbox_stale_hours=stale_hours)
        drained = await sweeper._drain_stale_inbox("agent:bob")

        assert drained == 0
        inbox_files = adapter.list_dir(inbox, ZONE)
        assert len(inbox_files) == 1

    @pytest.mark.asyncio
    async def test_drain_skips_messages_with_ttl(self, adapter: InMemoryStorageDriver) -> None:
        """Messages with TTL are handled by _sweep_agent, not the stale drain."""
        await _provision(adapter, "agent:bob")
        inbox = inbox_path("agent:bob")

        # Message with a far-future TTL (won't expire)
        msg = _make_msg("msg_with_ttl", ttl=999999)
        path = f"{inbox}/20260101T000000_{msg.id}.json"
        adapter.write(path, msg.to_bytes(), ZONE)

        await asyncio.sleep(_SLEEP_SECS)

        stale_hours = _TINY_RETENTION_SECS / 3600
        sweeper = TTLSweeper(adapter, zone_id=ZONE, inbox_stale_hours=stale_hours)
        drained = await sweeper._drain_stale_inbox("agent:bob")

        assert drained == 0  # TTL message skipped by drain
        inbox_files = adapter.list_dir(inbox, ZONE)
        assert len(inbox_files) == 1

    @pytest.mark.asyncio
    async def test_drain_claim_prevents_double_dead_letter(
        self, adapter: InMemoryStorageDriver
    ) -> None:
        """Two concurrent drain calls for the same message only dead-letter once."""
        await _provision(adapter, "agent:bob")
        inbox = inbox_path("agent:bob")

        msg = _make_msg("msg_concurrent_drain")
        path = f"{inbox}/20260101T000000_{msg.id}.json"
        adapter.write(path, msg.to_bytes(), ZONE)

        await asyncio.sleep(_SLEEP_SECS)

        stale_hours = _TINY_RETENTION_SECS / 3600
        sweeper1 = TTLSweeper(adapter, zone_id=ZONE, inbox_stale_hours=stale_hours)
        sweeper2 = TTLSweeper(adapter, zone_id=ZONE, inbox_stale_hours=stale_hours)

        # Both drains race — only one should win via atomic rename claim
        r1, r2 = await asyncio.gather(
            sweeper1._drain_stale_inbox("agent:bob"),
            sweeper2._drain_stale_inbox("agent:bob"),
        )

        total = r1 + r2
        assert total == 1, f"Expected exactly 1 drained, got r1={r1} r2={r2}"

        dl_files = adapter.list_dir(dead_letter_path("agent:bob"), ZONE)
        msg_files = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(msg_files) == 1, "Message must appear exactly once in dead_letter"


# ---------------------------------------------------------------------------
# Correctness: DLQ compaction via real NexusFS
# ---------------------------------------------------------------------------


class TestDeadLetterCompactionReal:
    """Write DLQ files, age them, compact, verify archive and originals."""

    @pytest.mark.asyncio
    async def test_compaction_preserve_originals_creates_archive(
        self, adapter: InMemoryStorageDriver
    ) -> None:
        await _provision(adapter, "agent:bob")
        dl = dead_letter_path("agent:bob")
        archive_dir = dead_letter_archive_path("agent:bob")

        # Write 10 DLQ files
        for i in range(10):
            fn = f"20260101T{i:06d}_msg_{i:04d}.json"
            adapter.write(f"{dl}/{fn}", json.dumps({"id": f"msg_{i}"}).encode(), ZONE)
            adapter.write(
                f"{dl}/{fn}.reason.json",
                json.dumps({"reason": "handler_error"}).encode(),
                ZONE,
            )

        await asyncio.sleep(_SLEEP_SECS)

        min_age_hours = _TINY_RETENTION_SECS / 3600
        sweeper = TTLSweeper(
            adapter,
            zone_id=ZONE,
            dead_letter_compact_min_files=5,
            dead_letter_compact_min_age_hours=min_age_hours,
            dead_letter_compact_delete_originals=False,  # preserve originals
        )
        archived = await sweeper._compact_dead_letter("agent:bob")

        assert archived == 10

        # Archive segment created
        archive_files = adapter.list_dir(archive_dir, ZONE)
        jsonl_files = [f for f in archive_files if f.endswith(".jsonl")]
        assert len(jsonl_files) == 1, f"Expected 1 archive segment, got {jsonl_files}"

        # Archive is valid JSONL
        content = adapter.sys_read(f"{archive_dir}/{jsonl_files[0]}", ZONE)
        records = [json.loads(line) for line in content.splitlines() if line]
        assert len(records) == 10
        for r in records:
            assert "file" in r and "envelope" in r and "reason" in r

        # Originals preserved (preserve mode)
        dl_files = adapter.list_dir(dl, ZONE)
        msg_files = [
            f
            for f in dl_files
            if f.endswith(".json") and not f.endswith(".reason.json") and ".archived" not in f
        ]
        assert len(msg_files) == 10

    @pytest.mark.asyncio
    async def test_compaction_delete_originals_removes_source_files(
        self, adapter: InMemoryStorageDriver
    ) -> None:
        await _provision(adapter, "agent:bob")
        dl = dead_letter_path("agent:bob")
        archive_dir = dead_letter_archive_path("agent:bob")

        for i in range(10):
            fn = f"20260101T{i:06d}_msg_{i:04d}.json"
            adapter.write(f"{dl}/{fn}", json.dumps({"id": f"msg_{i}"}).encode(), ZONE)

        await asyncio.sleep(_SLEEP_SECS)

        min_age_hours = _TINY_RETENTION_SECS / 3600
        sweeper = TTLSweeper(
            adapter,
            zone_id=ZONE,
            dead_letter_compact_min_files=5,
            dead_letter_compact_min_age_hours=min_age_hours,
            dead_letter_compact_delete_originals=True,
        )
        archived = await sweeper._compact_dead_letter("agent:bob")

        assert archived == 10

        # Originals deleted
        dl_files = adapter.list_dir(dl, ZONE)
        msg_files = [
            f
            for f in dl_files
            if f.endswith(".json") and not f.endswith(".reason.json") and ".archived" not in f
        ]
        assert len(msg_files) == 0

        # Archive exists
        archive_files = adapter.list_dir(archive_dir, ZONE)
        jsonl_files = [f for f in archive_files if f.endswith(".jsonl")]
        assert len(jsonl_files) == 1

    @pytest.mark.asyncio
    async def test_preserve_originals_idempotent(self, adapter: InMemoryStorageDriver) -> None:
        """Second sweep must not re-archive files already marked .archived."""
        await _provision(adapter, "agent:bob")
        dl = dead_letter_path("agent:bob")

        for i in range(5):
            fn = f"20260101T{i:06d}_msg_{i:04d}.json"
            adapter.write(f"{dl}/{fn}", json.dumps({"id": f"msg_{i}"}).encode(), ZONE)

        await asyncio.sleep(_SLEEP_SECS)

        min_age_hours = _TINY_RETENTION_SECS / 3600
        sweeper = TTLSweeper(
            adapter,
            zone_id=ZONE,
            dead_letter_compact_min_files=5,
            dead_letter_compact_min_age_hours=min_age_hours,
            dead_letter_compact_delete_originals=False,
        )

        archived1 = await sweeper._compact_dead_letter("agent:bob")
        assert archived1 == 5

        archived2 = await sweeper._compact_dead_letter("agent:bob")
        assert archived2 == 0, "Second compaction must be idempotent (0 re-archived)"

    @pytest.mark.asyncio
    async def test_compaction_skips_fresh_files(self, adapter: InMemoryStorageDriver) -> None:
        """Files younger than min_age must not be compacted."""
        await _provision(adapter, "agent:bob")
        dl = dead_letter_path("agent:bob")

        for i in range(10):
            fn = f"20260101T{i:06d}_msg_{i:04d}.json"
            adapter.write(f"{dl}/{fn}", json.dumps({"id": f"msg_{i}"}).encode(), ZONE)

        # No sleep — files are fresh
        min_age_hours = _TINY_RETENTION_SECS / 3600
        sweeper = TTLSweeper(
            adapter,
            zone_id=ZONE,
            dead_letter_compact_min_files=5,
            dead_letter_compact_min_age_hours=min_age_hours,
        )
        archived = await sweeper._compact_dead_letter("agent:bob")
        assert archived == 0, "Fresh files must not be compacted"


# ---------------------------------------------------------------------------
# Correctness: full sweep_once() via real NexusFS
# ---------------------------------------------------------------------------


class TestFullSweepOnceReal:
    """Run sweep_once() end-to-end with all retention features enabled."""

    @pytest.mark.asyncio
    async def test_sweep_once_prunes_all_directories(self, adapter: InMemoryStorageDriver) -> None:
        """sweep_once() with all retention enabled cleans processed, outbox, drains inbox."""
        await _provision(adapter, "agent:alice", "agent:bob")

        proc = processed_path("agent:bob")
        out = outbox_path("agent:bob")
        inbox = inbox_path("agent:bob")

        # Write aged processed + outbox files
        for i in range(3):
            adapter.write(f"{proc}/20260101T{i:06d}_p{i}.json", b'{"id":"p"}', ZONE)
            adapter.write(f"{out}/20260101T{i:06d}_o{i}.json", b'{"id":"o"}', ZONE)

        # Write a no-TTL inbox message that should be stale-drained
        msg = _make_msg("msg_stale_sweep")
        adapter.write(f"{inbox}/20260101T000000_{msg.id}.json", msg.to_bytes(), ZONE)

        # Write a FRESH inbox message — must not be touched
        fresh = _make_msg("msg_fresh_sweep")
        adapter.write(f"{inbox}/20260101T999999_{fresh.id}.json", fresh.to_bytes(), ZONE)

        await asyncio.sleep(_SLEEP_SECS)

        # Write fresh message AFTER sleep — should be protected
        very_fresh = _make_msg("msg_very_fresh")
        adapter.write(f"{inbox}/20260101T888888_{very_fresh.id}.json", very_fresh.to_bytes(), ZONE)

        retention = _TINY_RETENTION_SECS / 86400
        stale_hours = _TINY_RETENTION_SECS / 3600
        sweeper = TTLSweeper(
            adapter,
            zone_id=ZONE,
            interval=3600,  # large — so _is_recent_by_filename never skips
            processed_retention_days=retention,
            outbox_retention_days=retention,
            inbox_stale_hours=stale_hours,
        )
        await sweeper.sweep_once()

        # processed/ and outbox/ cleaned up
        assert len(adapter.list_dir(proc, ZONE)) == 0
        assert len(adapter.list_dir(out, ZONE)) == 0

        # Old inbox msg drained
        dl_files = adapter.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert any("msg_stale_sweep" in f for f in dl_msgs)

        # Fresh inbox messages untouched
        inbox_files = adapter.list_dir(inbox, ZONE)
        assert any("msg_very_fresh" in f for f in inbox_files)


# ---------------------------------------------------------------------------
# Correctness: processor .proc_ claim via real NexusFS
# ---------------------------------------------------------------------------


class TestProcClaimReal:
    """Verify .proc_ claim mechanism works through real VFS."""

    @pytest.mark.asyncio
    async def test_full_roundtrip_with_proc_claim(self, adapter: InMemoryStorageDriver) -> None:
        """Full send → claim → handler → processed lifecycle via real adapter."""
        from tests.unit.bricks.ipc.fakes import InMemoryEventPublisher

        await _provision(adapter, "agent:alice", "agent:bob")

        sender = MessageSender(adapter, InMemoryEventPublisher(), zone_id=ZONE)
        msg = _make_msg("msg_proc_real")
        await sender.send(msg)

        received = []

        async def handler(envelope: MessageEnvelope) -> None:
            received.append(envelope.id)

        processor = MessageProcessor(adapter, "agent:bob", handler, zone_id=ZONE)
        count = await processor.process_inbox()

        assert count == 1
        assert "msg_proc_real" in received

        # Inbox empty
        assert len(adapter.list_dir(inbox_path("agent:bob"), ZONE)) == 0

        # Message moved to processed
        proc_files = adapter.list_dir(processed_path("agent:bob"), ZONE)
        assert any("msg_proc_real" in f for f in proc_files)

    @pytest.mark.asyncio
    async def test_proc_claim_prevents_concurrent_drain(
        self, adapter: InMemoryStorageDriver
    ) -> None:
        """Processor claims file; concurrent drain cannot dead-letter it."""
        await _provision(adapter, "agent:bob")
        inbox = inbox_path("agent:bob")

        msg = _make_msg("msg_race")
        path = f"{inbox}/20260101T000000_{msg.id}.json"
        adapter.write(path, msg.to_bytes(), ZONE)

        # Simulate processor claiming the file
        claim_ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        proc_path = f"{path}.proc_{claim_ts}_abcd"
        adapter.rename(path, proc_path, ZONE)

        await asyncio.sleep(_SLEEP_SECS)

        # Drain runs — should not touch the .proc_ file (not a .json)
        stale_hours = _TINY_RETENTION_SECS / 3600
        sweeper = TTLSweeper(adapter, zone_id=ZONE, inbox_stale_hours=stale_hours)
        drained = await sweeper._drain_stale_inbox("agent:bob")

        assert drained == 0
        dl_files = adapter.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 0, "Claimed message must not appear in DLQ"


# ---------------------------------------------------------------------------
# Performance: real NexusFS at production-representative scale
# ---------------------------------------------------------------------------


class TestPerformanceReal:
    """Performance benchmarks using real InMemoryStorageDriver + NexusFS."""

    @pytest.mark.asyncio
    async def test_file_mtime_latency_20_files(self, adapter: InMemoryStorageDriver) -> None:
        """file_mtime() per-call latency via real NexusFS must average < 20ms."""
        await _provision(adapter, "agent:perf_mtime")

        paths = []
        for i in range(20):
            p = f"{inbox_path('agent:perf_mtime')}/msg_{i:03d}.json"
            adapter.write(p, b'{"id":"x"}', ZONE)
            paths.append(p)

        latencies = []
        for p in paths:
            t0 = time.perf_counter()
            mtime = adapter.file_mtime(p, ZONE)
            latencies.append(time.perf_counter() - t0)
            assert mtime is not None

        avg_ms = sum(latencies) / len(latencies) * 1000
        max_ms = max(latencies) * 1000
        print(f"\n[PERF] file_mtime 20 files: avg={avg_ms:.1f}ms  max={max_ms:.1f}ms")
        assert avg_ms < 20.0, f"Avg mtime latency {avg_ms:.1f}ms > 20ms budget"

    @pytest.mark.asyncio
    async def test_prune_50_processed_files_real(self, adapter: InMemoryStorageDriver) -> None:
        """Prune 50 processed files through real NexusFS in < 10 seconds."""
        await _provision(adapter, "agent:perf_prune")
        proc = processed_path("agent:perf_prune")

        for i in range(50):
            adapter.write(f"{proc}/20260101T{i:06d}_msg_{i:04d}.json", b"{}", ZONE)

        await asyncio.sleep(_SLEEP_SECS)

        retention_days = _TINY_RETENTION_SECS / 86400
        sweeper = TTLSweeper(adapter, zone_id=ZONE, processed_retention_days=retention_days)

        t0 = time.perf_counter()
        await sweeper._prune_dir("agent:perf_prune", "processed", retention_days)
        elapsed = time.perf_counter() - t0

        remaining = adapter.list_dir(proc, ZONE)
        assert len(remaining) == 0, f"Expected 0 files, got {len(remaining)}"
        print(f"\n[PERF] Prune 50 processed files (real NexusFS): {elapsed * 1000:.0f}ms")
        assert elapsed < 10.0, f"Prune of 50 files took {elapsed:.2f}s > 10s budget"

    @pytest.mark.asyncio
    async def test_compact_30_dlq_files_real(self, adapter: InMemoryStorageDriver) -> None:
        """Compact 30 DLQ files through real NexusFS in < 10 seconds."""
        await _provision(adapter, "agent:perf_compact")
        dl = dead_letter_path("agent:perf_compact")
        archive_dir = dead_letter_archive_path("agent:perf_compact")

        for i in range(30):
            fn = f"20260101T{i:06d}_msg_{i:04d}.json"
            payload = json.dumps({"id": f"msg_{i}", "data": "x" * 512}).encode()
            adapter.write(f"{dl}/{fn}", payload, ZONE)
            adapter.write(
                f"{dl}/{fn}.reason.json",
                json.dumps({"reason": "handler_error", "detail": f"err {i}"}).encode(),
                ZONE,
            )

        await asyncio.sleep(_SLEEP_SECS)

        min_age_hours = _TINY_RETENTION_SECS / 3600
        sweeper = TTLSweeper(
            adapter,
            zone_id=ZONE,
            dead_letter_compact_min_files=10,
            dead_letter_compact_min_age_hours=min_age_hours,
            dead_letter_compact_delete_originals=True,
        )

        t0 = time.perf_counter()
        archived = await sweeper._compact_dead_letter("agent:perf_compact")
        elapsed = time.perf_counter() - t0

        assert archived == 30
        # _archive subdir will appear in listing — filter it out
        dl_files = adapter.list_dir(dl, ZONE)
        remaining = [f for f in dl_files if f != "_archive"]
        assert len(remaining) == 0, f"Unexpected files remaining: {remaining}"

        archive_files = adapter.list_dir(archive_dir, ZONE)
        assert any(f.endswith(".jsonl") for f in archive_files)

        print(f"\n[PERF] Compact 30 DLQ files (real NexusFS): {elapsed * 1000:.0f}ms")
        assert elapsed < 10.0, f"Compact of 30 files took {elapsed:.2f}s > 10s budget"

    @pytest.mark.asyncio
    async def test_sweep_once_10_agents_5_files_each_real(
        self, adapter: InMemoryStorageDriver
    ) -> None:
        """Full sweep_once() across 10 agents × 5 files via real NexusFS < 15s."""
        N_AGENTS = 10
        N_FILES = 5

        for i in range(N_AGENTS):
            aid = f"agent:sweepperf_{i:02d}"
            await _provision(adapter, aid)
            proc = processed_path(aid)
            for j in range(N_FILES):
                adapter.write(f"{proc}/20260101T{j:06d}_msg_{j:04d}.json", b"{}", ZONE)

        await asyncio.sleep(_SLEEP_SECS)

        retention_days = _TINY_RETENTION_SECS / 86400
        sweeper = TTLSweeper(
            adapter,
            zone_id=ZONE,
            interval=3600,
            processed_retention_days=retention_days,
        )

        t0 = time.perf_counter()
        await sweeper.sweep_once()
        elapsed = time.perf_counter() - t0

        # All processed files deleted across all agents
        for i in range(N_AGENTS):
            remaining = adapter.list_dir(processed_path(f"agent:sweepperf_{i:02d}"), ZONE)
            assert len(remaining) == 0

        print(
            f"\n[PERF] sweep_once() {N_AGENTS} agents × {N_FILES} processed files "
            f"(real NexusFS): {elapsed * 1000:.0f}ms"
        )
        assert elapsed < 15.0, f"sweep_once took {elapsed:.2f}s > 15s budget"
