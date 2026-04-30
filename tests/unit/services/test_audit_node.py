"""Unit tests for ``services.audit_node.AuditNode``.

These tests use a mock kernel — they verify the bootstrap +
collect-loop logic without touching real federation / stream
infrastructure.  The docker-based long-workflow integration test
covers the live federated path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from nexus.services.audit_node import AuditNode


class _MockStream:
    """Append-only list with offset-based read.  Mirrors
    ``Kernel::stream_read_batch`` semantics used by the real WAL
    backend: ``read_batch(offset, count) -> (entries, new_offset)``.
    """

    def __init__(self) -> None:
        self.records: list[bytes] = []

    def append(self, data: bytes) -> int:
        self.records.append(data)
        return len(self.records)

    def read_batch(self, offset: int, count: int) -> tuple[list[bytes], int]:
        end = min(offset + count, len(self.records))
        return self.records[offset:end], end


class _MockKernel:
    """Minimal kernel surface ``AuditNode`` consumes.

    Tracks per-path streams + per-path file content (for the offset
    persistence file).  No real audit / federation behavior — just
    enough surface for the service to drive against.
    """

    def __init__(self) -> None:
        self._streams: dict[str, _MockStream] = {}
        self._files: dict[str, bytes] = {}
        self.created_zones: list[str] = []
        self.joined_zones: list[tuple[str, bool]] = []
        self.prepared_streams: list[tuple[str, str]] = []

    # ── stream surface ──────────────────────────────────────────────

    def _stream(self, path: str) -> _MockStream:
        return self._streams.setdefault(path, _MockStream())

    def stream_read_batch(self, path: str, offset: int, count: int) -> tuple[list[bytes], int]:
        return self._stream(path).read_batch(offset, count)

    def stream_write_nowait(self, path: str, data: bytes) -> int:
        return self._stream(path).append(data)

    # ── file surface (for offset persistence) ──────────────────────

    def sys_read(self, path: str) -> bytes | None:
        return self._files.get(path)

    def sys_write(self, path: str, data: bytes) -> int:
        self._files[path] = data
        return len(data)


@pytest.fixture
def kernel() -> _MockKernel:
    return _MockKernel()


@pytest.fixture
def audit_node(kernel: _MockKernel) -> AuditNode:
    return AuditNode(
        kernel,
        audit_zone_id="audit",
        stream_path="/audit/traces/",
        batch_size=10,
        poll_interval_secs=0.01,
    )


# ── bootstrap ──────────────────────────────────────────────────────


def test_bootstrap_creates_audit_zone_and_joins_production_as_learner(
    audit_node: AuditNode, kernel: _MockKernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bootstrap should create the audit zone, join each production
    zone as a learner, and register the audit DT_STREAM locally for
    every joined zone.  No AuditHook installation — audit-node is a
    consumer."""

    create_calls: list[str] = []
    join_calls: list[tuple[str, bool]] = []
    prepare_calls: list[tuple[str, str]] = []

    class _StubRuntime:
        @staticmethod
        def federation_create_zone(_kernel: Any, zone_id: str) -> str:
            create_calls.append(zone_id)
            return zone_id

        @staticmethod
        def federation_join_zone(_kernel: Any, zone_id: str, *, as_learner: bool = False) -> str:
            join_calls.append((zone_id, as_learner))
            return zone_id

        @staticmethod
        def prepare_audit_stream_only(_kernel: Any, zone_id: str, stream_path: str) -> None:
            prepare_calls.append((zone_id, stream_path))

    monkeypatch.setitem(__import__("sys").modules, "nexus_runtime", _StubRuntime)

    audit_node.bootstrap(["corp", "family"])

    # Audit zone created exactly once.
    assert create_calls == ["audit"]
    # Every production zone joined as learner.
    assert join_calls == [("corp", True), ("family", True)]
    # Audit stream registered locally on every joined zone.
    assert prepare_calls == [
        ("corp", "/audit/traces/"),
        ("family", "/audit/traces/"),
    ]
    # Both source zones now have a checkpoint at offset 0.
    assert set(audit_node._checkpoints.keys()) == {"corp", "family"}
    assert audit_node._checkpoints["corp"].offset == 0
    assert audit_node._checkpoints["family"].offset == 0


# ── collect / drain ────────────────────────────────────────────────


def test_drain_zone_appends_records_to_local_zone_and_advances_offset(
    audit_node: AuditNode, kernel: _MockKernel
) -> None:
    """Drain-once: source zone has 3 audit records → audit-node
    appends them to ``/audit/collect/corp/traces`` and advances the
    offset checkpoint to 3."""
    # Seed source-zone audit stream with 3 records.
    for i in range(3):
        kernel._stream("/corp/audit/traces").append(json.dumps({"op": "write", "n": i}).encode())

    # Inject checkpoint manually (bypasses real bootstrap which would
    # need the nexus_runtime module).
    audit_node._checkpoints["corp"] = audit_node._checkpoints.get("corp") or __import__(
        "nexus.services.audit_node.service", fromlist=["AuditCheckpoint"]
    ).AuditCheckpoint("corp", 0)

    collected = audit_node._poll_once()

    assert collected == 3
    # Audit-node's local zone got every record appended in order.
    target = kernel._streams["/audit/collect/corp/traces"]
    assert len(target.records) == 3
    assert json.loads(target.records[0])["n"] == 0
    assert json.loads(target.records[2])["n"] == 2
    # Offset persisted.
    assert audit_node._checkpoints["corp"].offset == 3
    persisted = json.loads(kernel._files["/audit/collect/corp/offset"])
    assert persisted == {"offset": 3}


def test_drain_zone_resumes_from_persisted_offset(
    audit_node: AuditNode, kernel: _MockKernel
) -> None:
    """A second call after the source produces more records starts
    from the saved offset (no duplicate replay)."""
    AuditCheckpoint = __import__(
        "nexus.services.audit_node.service", fromlist=["AuditCheckpoint"]
    ).AuditCheckpoint

    for i in range(3):
        kernel._stream("/corp/audit/traces").append(json.dumps({"n": i}).encode())
    audit_node._checkpoints["corp"] = AuditCheckpoint("corp", 0)
    audit_node._poll_once()  # drains 3, offset → 3

    # Source produces 2 more records.
    for i in range(3, 5):
        kernel._stream("/corp/audit/traces").append(json.dumps({"n": i}).encode())

    collected = audit_node._poll_once()
    assert collected == 2
    # Local stream now holds the original 3 + the new 2, no
    # duplicates.
    target = kernel._streams["/audit/collect/corp/traces"]
    assert [json.loads(r)["n"] for r in target.records] == [0, 1, 2, 3, 4]
    assert audit_node._checkpoints["corp"].offset == 5


def test_drain_zone_idempotent_when_source_empty(
    audit_node: AuditNode, kernel: _MockKernel
) -> None:
    AuditCheckpoint = __import__(
        "nexus.services.audit_node.service", fromlist=["AuditCheckpoint"]
    ).AuditCheckpoint
    audit_node._checkpoints["corp"] = AuditCheckpoint("corp", 0)
    assert audit_node._poll_once() == 0
    # No local stream / no offset file should have been created.
    assert "/audit/collect/corp/traces" not in kernel._streams
    assert "/audit/collect/corp/offset" not in kernel._files


# ── run loop ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_loop_drains_then_stops_on_signal(
    audit_node: AuditNode, kernel: _MockKernel
) -> None:
    """The async run loop should poll until ``stop()`` is called."""
    AuditCheckpoint = __import__(
        "nexus.services.audit_node.service", fromlist=["AuditCheckpoint"]
    ).AuditCheckpoint
    for i in range(5):
        kernel._stream("/corp/audit/traces").append(json.dumps({"n": i}).encode())
    audit_node._checkpoints["corp"] = AuditCheckpoint("corp", 0)

    task = asyncio.create_task(audit_node.run())
    # Give it one or two iterations (poll_interval=0.01s).
    await asyncio.sleep(0.05)
    audit_node.stop()
    await asyncio.wait_for(task, timeout=1.0)

    # All 5 records collected.
    target = kernel._streams["/audit/collect/corp/traces"]
    assert len(target.records) == 5
    assert audit_node._checkpoints["corp"].offset == 5
