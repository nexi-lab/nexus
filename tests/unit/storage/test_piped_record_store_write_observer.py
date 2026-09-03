"""Unit tests for the write-through group-commit observer (#4738).

The observer commits operation_log / file_paths / version_history before
``on_write`` returns and hands back the projection sequence; MCL rows and
post-flush hooks run on the deferred thread.  These tests stub the commit
(``_flush_batch_sync``) so they exercise the queueing, grouping, strict-mode
and never-drop-silently policies without a database.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pytest

from nexus.contracts.exceptions import AuditLogError
from nexus.storage import piped_record_store_write_observer as mod
from nexus.storage.piped_record_store_write_observer import RecordStoreWriteObserver


class FakeRecordStore:
    session_factory = object()


class _StubCommit:
    """Replaces ``_flush_batch_sync``: records batches, stamps sequences."""

    def __init__(self, *, poison: set[str] | None = None, block: threading.Event | None = None):
        self.batches: list[list[dict[str, Any]]] = []
        self.poison = poison or set()
        self.block = block
        self._seq = 0
        self._lock = threading.Lock()

    def __call__(self, events: list[dict[str, Any]]) -> None:
        if self.block is not None:
            self.block.wait()
        for event in events:
            if event.get("path") in self.poison:
                raise RuntimeError(f"value too long for {event['path']}")
        with self._lock:
            for event in events:
                self._seq += 1
                event["projection_seq"] = self._seq
            self.batches.append(list(events))


def _observer(
    monkeypatch: pytest.MonkeyPatch,
    stub: _StubCommit,
    *,
    strict: bool = True,
    timeout: float = 5.0,
) -> RecordStoreWriteObserver:
    observer = RecordStoreWriteObserver(
        FakeRecordStore(), strict_mode=strict, write_through_timeout_s=timeout
    )
    monkeypatch.setattr(observer, "_flush_batch_sync", stub)
    # MCL recording needs a real session factory; not under test here.
    monkeypatch.setattr(observer, "_record_mcl_batch", lambda events: None)
    # Retries sleep with exponential backoff; keep the poison tests fast.
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    return observer


def _write(observer: RecordStoreWriteObserver, path: str, **kw: Any) -> int | None:
    return observer.on_write(
        {"content_id": "new", "path": path},
        is_new=kw.pop("is_new", True),
        path=path,
        **kw,
    )


# ── intake contract ───────────────────────────────────────────────────


def test_on_write_commits_before_returning_and_returns_projection_seq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubCommit()
    observer = _observer(monkeypatch, stub)
    try:
        seq = observer.on_write(
            {"content_id": "new", "path": "/workspace/report.csv"},
            is_new=False,
            path="/workspace/report.csv",
            old_metadata={"content_id": "old", "path": "/workspace/report.csv"},
            zone_id="zone-a",
            agent_id="agent-a",
        )
        assert seq == 1
        assert len(stub.batches) == 1
        event = stub.batches[0][0]
        assert event["op"] == "write"
        assert event["snapshot_hash"] == "old"
        assert event["metadata_snapshot"] == {
            "content_id": "old",
            "path": "/workspace/report.csv",
        }
        assert event["zone_id"] == "zone-a" and event["agent_id"] == "agent-a"
        assert observer.metrics["total_flushed"] == 1
        assert observer.metrics["applied_seq"] == 1
        assert observer.metrics["pending_events"] == 0
    finally:
        observer.cancel()


def test_on_delete_accepts_dict_metadata_contract_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubCommit()
    observer = _observer(monkeypatch, stub)
    try:
        seq = observer.on_delete(
            path="/workspace/report.csv",
            metadata={"content_id": "old", "path": "/workspace/report.csv"},
            zone_id="zone-a",
            agent_id="agent-a",
        )
        assert seq == 1
        event = stub.batches[0][0]
        assert event["op"] == "delete"
        assert event["snapshot_hash"] == "old"
        assert event["metadata_snapshot"] == {
            "content_id": "old",
            "path": "/workspace/report.csv",
        }
    finally:
        observer.cancel()


def test_on_write_batch_returns_one_seq_per_item_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.contracts.metadata import FileMetadata

    stub = _StubCommit()
    observer = _observer(monkeypatch, stub)
    try:
        items = [
            (FileMetadata(path="/a", size=1, content_id="ca"), True),
            (FileMetadata(path="/b", size=1, content_id="cb"), False),
        ]
        seqs = observer.on_write_batch(items, zone_id="z", agent_id="ag")
        assert seqs == [1, 2]
        assert len(stub.batches) == 1, "one transaction for the whole batch"
        assert [e["path"] for e in stub.batches[0]] == ["/a", "/b"]
        assert observer.on_write_batch([], zone_id="z") == []
    finally:
        observer.cancel()


# ── group commit ──────────────────────────────────────────────────────


def test_concurrent_writers_share_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    stub = _StubCommit(block=gate)
    observer = _observer(monkeypatch, stub)
    results: dict[str, int | None] = {}

    def writer(path: str) -> None:
        results[path] = _write(observer, path)

    try:
        # First writer opens a commit that blocks on the gate...
        first = threading.Thread(target=writer, args=("/first",))
        first.start()
        deadline = time.monotonic() + 2
        while observer.metrics["pending_events"] != 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        # ...meanwhile eight more writers queue up behind it.
        rest = [threading.Thread(target=writer, args=(f"/p{i}",)) for i in range(8)]
        for t in rest:
            t.start()
        deadline = time.monotonic() + 2
        while observer.metrics["pending_events"] < 8 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert observer.metrics["pending_events"] == 8
        gate.set()
        for t in [first, *rest]:
            t.join(timeout=5)

        assert len(stub.batches) == 2, "first alone, then the eight queued writers together"
        assert len(stub.batches[1]) == 8
        # Every writer got the sequence of its own row.
        assert sorted(results.values()) == list(range(1, 10))
        for batch in stub.batches:
            for event in batch:
                assert results[event["path"]] == event["projection_seq"]
    finally:
        gate.set()
        observer.cancel()


# ── failure policy ────────────────────────────────────────────────────


def test_poison_event_is_salvaged_per_event_and_strict_writer_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#4645 + #4738: one poison row loses only itself; its writer gets the typed error."""
    gate = threading.Event()
    stub = _StubCommit(poison={"/deep/poison"}, block=gate)
    observer = _observer(monkeypatch, stub)
    hook_calls: list[list[dict[str, Any]]] = []
    observer.register_post_flush_hook(hook_calls.append)
    outcome: dict[str, Any] = {}

    def good(path: str) -> None:
        outcome[path] = _write(observer, path)

    def bad() -> None:
        try:
            _write(observer, "/deep/poison")
        except AuditLogError as exc:
            outcome["/deep/poison"] = exc

    try:
        threads = [threading.Thread(target=good, args=("/a",))]
        threads[0].start()
        deadline = time.monotonic() + 2
        while observer.metrics["pending_events"] != 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        threads += [threading.Thread(target=bad), threading.Thread(target=good, args=("/b",))]
        for t in threads[1:]:
            t.start()
        deadline = time.monotonic() + 2
        while observer.metrics["pending_events"] < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        with caplog.at_level(logging.ERROR, logger=mod.__name__):
            gate.set()
            for t in threads:
                t.join(timeout=5)
            observer.flush_sync()  # run deferred hooks inline

        assert isinstance(outcome["/deep/poison"], AuditLogError)
        assert outcome["/a"] == 1 and outcome["/b"] == 2
        assert observer.metrics["total_flushed"] == 2
        assert observer.metrics["total_failed"] == 1
        assert observer.metrics["total_retries"] == mod._MAX_RETRIES
        assert any(
            "dropping audit event" in r.message and "/deep/poison" in r.message
            for r in caplog.records
        )
        # Hooks fire for the salvaged subset only.
        assert [sorted(e["path"] for e in call) for call in hook_calls] == [["/a"], ["/b"]]
    finally:
        gate.set()
        observer.cancel()


def test_non_strict_failure_returns_none_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubCommit(poison={"/poison"})
    observer = _observer(monkeypatch, stub, strict=False)
    try:
        assert _write(observer, "/poison") is None
        assert observer.metrics["total_failed"] == 1
    finally:
        observer.cancel()


def test_timeout_strict_raises_and_non_strict_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    stub = _StubCommit(block=gate)

    strict = _observer(monkeypatch, stub, strict=True, timeout=0.05)
    try:
        with pytest.raises(AuditLogError, match="not confirmed within"):
            _write(strict, "/slow")
        assert strict.metrics["total_timeouts"] == 1
    finally:
        gate.set()
        strict.cancel()

    gate.clear()
    stub2 = _StubCommit(block=gate)
    lenient = _observer(monkeypatch, stub2, strict=False, timeout=0.05)
    try:
        assert _write(lenient, "/slow") is None
        assert lenient.metrics["total_timeouts"] == 1
        # The event was NOT lost: it commits once the store answers.
        gate.set()
        deadline = time.monotonic() + 2
        while lenient.metrics["total_flushed"] < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert lenient.metrics["total_flushed"] == 1
    finally:
        gate.set()
        lenient.cancel()


# ── never drop silently ───────────────────────────────────────────────


def test_deferred_queue_overflow_drops_loudly_with_metric(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(mod, "_DEFERRED_MAX_EVENTS", 2)
    monkeypatch.setattr(mod, "_BACKPRESSURE_S", 0.01)
    stub = _StubCommit()
    observer = _observer(monkeypatch, stub)
    # Stall the deferred worker so batches pile up.
    stall = threading.Event()
    monkeypatch.setattr(observer, "_run_deferred", lambda events: stall.wait())
    dropped_metric: list[tuple[str, int]] = []
    monkeypatch.setattr(
        mod.io_metrics, "record_projection_dropped", lambda q, n: dropped_metric.append((q, n))
    )
    try:
        with caplog.at_level(logging.ERROR, logger=mod.__name__):
            for i in range(5):
                assert _write(observer, f"/f{i}") == i + 1
        # Critical rows all committed — only deferred work was shed.
        assert observer.metrics["total_flushed"] == 5
        assert observer.metrics["total_dropped"] >= 1
        assert dropped_metric and all(q == "deferred" for q, _ in dropped_metric)
        assert any("dropped" in r.message and "deferred queue" in r.message for r in caplog.records)
    finally:
        stall.set()
        observer.cancel()


def test_pending_queue_overflow_releases_oldest_ticket_with_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(mod, "_PENDING_MAX_EVENTS", 2)
    gate = threading.Event()
    stub = _StubCommit(block=gate)
    observer = _observer(monkeypatch, stub, strict=False, timeout=0.02)
    dropped_metric: list[tuple[str, int]] = []
    monkeypatch.setattr(
        mod.io_metrics, "record_projection_dropped", lambda q, n: dropped_metric.append((q, n))
    )
    try:
        with caplog.at_level(logging.ERROR, logger=mod.__name__):
            # Commit thread is stuck on the first event; the rest time out
            # (non-strict → None) and stay queued until the bound trips.
            for i in range(5):
                assert _write(observer, f"/p{i}") is None
        assert observer.metrics["total_dropped"] >= 1
        assert dropped_metric and all(q == "pending" for q, _ in dropped_metric)
        assert any("unconfirmed event" in r.message for r in caplog.records)
    finally:
        gate.set()
        observer.cancel()


# ── flush / shutdown ──────────────────────────────────────────────────


def test_flush_sync_commits_queued_events_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubCommit()
    observer = _observer(monkeypatch, stub)
    # Bypass the commit thread entirely: queue without waiting, then flush.
    monkeypatch.setattr(observer, "_ensure_threads_locked", lambda: None)
    try:
        observer._enqueue({"op": "write", "path": "/a", "is_new": True, "metadata": {}})
        observer._enqueue({"op": "delete", "path": "/b"})
        assert observer.metrics["pending_events"] == 2
        assert observer.flush_sync() == 2
        assert observer.metrics["pending_events"] == 0
        assert observer.metrics["total_flushed"] == 2
        assert observer.flush_sync() == 0
    finally:
        observer.cancel()


def test_flush_sync_salvages_per_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """#4645: shutdown flush drops only the individually failing rows."""
    stub = _StubCommit(poison={"/p"})
    observer = _observer(monkeypatch, stub)
    monkeypatch.setattr(observer, "_ensure_threads_locked", lambda: None)
    try:
        observer._enqueue({"op": "write", "path": "/a", "is_new": True, "metadata": {}})
        observer._enqueue({"op": "rename", "path": "/p", "new_path": "/q"})
        assert observer.flush_sync() == 1
        assert observer.metrics["total_flushed"] == 1
        assert observer.metrics["total_failed"] == 1
    finally:
        observer.cancel()


def test_metrics_keys_and_legacy_debounce_attribute() -> None:
    observer = RecordStoreWriteObserver(FakeRecordStore(), debounce_seconds=60)
    assert observer._debounce == 60
    assert set(observer.metrics) == {
        "total_flushed",
        "total_failed",
        "total_retries",
        "total_dropped",
        "total_timeouts",
        "pending_events",
        "deferred_events",
        "applied_seq",
    }
    observer.cancel()  # no threads started — must be a no-op
