"""Async projection mode (``NEXUS_PROJECTION_MODE=async``) for the group-commit observer.

``write_through=False`` keeps the #4738 machinery (group commit thread, bounded
queues, retry/salvage, metrics, reconcile) but lets the writer return as soon as
its ticket is queued: no wait, ``projection_seq`` is ``None``.  Tenants that
keep their own version history and never fence on ``projection_seq`` use it to
take the projection transaction (one round of DB round-trips + fsync) off the
write path.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from nexus.factory._system import resolve_projection_write_through
from nexus.storage import piped_record_store_write_observer as mod
from nexus.storage.piped_record_store_write_observer import RecordStoreWriteObserver


class FakeRecordStore:
    session_factory = object()


class _StubCommit:
    def __init__(self, *, block: threading.Event | None = None, fail: bool = False):
        self.batches: list[list[dict[str, Any]]] = []
        self.block = block
        self.fail = fail
        self._seq = 0
        self._lock = threading.Lock()

    def __call__(self, events: list[dict[str, Any]]) -> None:
        if self.block is not None:
            self.block.wait()
        if self.fail:
            raise RuntimeError("record store down")
        with self._lock:
            for event in events:
                self._seq += 1
                event["projection_seq"] = self._seq
            self.batches.append(list(events))


def _observer(
    monkeypatch: pytest.MonkeyPatch, stub: _StubCommit, *, strict: bool = True
) -> RecordStoreWriteObserver:
    observer = RecordStoreWriteObserver(
        FakeRecordStore(), strict_mode=strict, write_through_timeout_s=5.0, write_through=False
    )
    monkeypatch.setattr(observer, "_flush_batch_sync", stub)
    monkeypatch.setattr(observer, "_record_mcl_batch", lambda events: None)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    return observer


def _write(observer: RecordStoreWriteObserver, path: str) -> int | None:
    return observer.on_write({"content_id": path, "path": path}, is_new=True, path=path)


def _wait_flushed(observer: RecordStoreWriteObserver, n: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while observer.metrics["total_flushed"] < n and time.monotonic() < deadline:
        time.sleep(0.005)


def test_async_write_returns_immediately_without_seq_and_still_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()  # commit blocked → a write-through writer would wait here
    stub = _StubCommit(block=gate)
    observer = _observer(monkeypatch, stub)
    try:
        t0 = time.monotonic()
        assert _write(observer, "/a") is None
        assert time.monotonic() - t0 < 1.0  # did not wait for the (blocked) commit
        assert observer.metrics["pending_events"] == 1
        assert observer.metrics["total_timeouts"] == 0  # not a timeout: nobody waited

        gate.set()
        _wait_flushed(observer, 1)
        assert observer.metrics["total_flushed"] == 1
        assert observer.metrics["applied_seq"] == 1
        assert stub.batches[0][0]["op"] == "write"
    finally:
        gate.set()
        observer.cancel()


def test_async_burst_is_group_committed(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    stub = _StubCommit(block=gate)
    observer = _observer(monkeypatch, stub)
    try:
        for i in range(25):
            assert _write(observer, f"/burst/{i}") is None
        gate.set()
        _wait_flushed(observer, 25)
        assert observer.metrics["total_flushed"] == 25
        # Fewer transactions than writes: the drain grouped the queued tickets.
        assert 1 <= len(stub.batches) < 25
        assert observer.metrics["pending_events"] == 0
    finally:
        gate.set()
        observer.cancel()


def test_async_coalesces_a_burst_within_the_debounce_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody waits in async mode, so tickets arriving inside ``debounce_seconds``
    share one transaction (pre-#4738 batching) instead of one commit per write."""
    stub = _StubCommit()
    observer = RecordStoreWriteObserver(
        FakeRecordStore(), strict_mode=True, write_through=False, debounce_seconds=0.3
    )
    monkeypatch.setattr(observer, "_flush_batch_sync", stub)
    monkeypatch.setattr(observer, "_record_mcl_batch", lambda events: None)
    try:
        for i in range(10):
            assert _write(observer, f"/coalesce/{i}") is None
        _wait_flushed(observer, 10)
        assert observer.metrics["total_flushed"] == 10
        assert len(stub.batches) == 1
        assert [e["path"] for e in stub.batches[0]] == [f"/coalesce/{i}" for i in range(10)]
    finally:
        observer.cancel()


def test_async_cancel_flushes_queued_tickets_without_waiting_out_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubCommit()
    observer = RecordStoreWriteObserver(
        FakeRecordStore(), strict_mode=True, write_through=False, debounce_seconds=5.0
    )
    monkeypatch.setattr(observer, "_flush_batch_sync", stub)
    monkeypatch.setattr(observer, "_record_mcl_batch", lambda events: None)
    assert _write(observer, "/late") is None
    t0 = time.monotonic()
    observer.cancel()
    assert time.monotonic() - t0 < 3.0  # stop wakes the coalescing wait
    assert observer.metrics["total_flushed"] == 1


def test_async_commit_failure_is_logged_and_counted_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stub = _StubCommit(fail=True)
    observer = _observer(monkeypatch, stub, strict=True)
    try:
        # strict_mode cannot raise at the writer in async mode (nobody waits) …
        assert _write(observer, "/poison") is None
        deadline = time.monotonic() + 2
        while observer.metrics["total_failed"] < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        # … but the loss is never silent: ERROR log + failed counter.
        assert observer.metrics["total_failed"] == 1
        assert any(
            "dropping audit event" in r.getMessage() and r.levelname == "ERROR"
            for r in caplog.records
        )
    finally:
        observer.cancel()


def test_write_through_default_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubCommit()
    observer = RecordStoreWriteObserver(FakeRecordStore(), strict_mode=True)
    monkeypatch.setattr(observer, "_flush_batch_sync", stub)
    monkeypatch.setattr(observer, "_record_mcl_batch", lambda events: None)
    try:
        assert _write(observer, "/sync") == 1
    finally:
        observer.cancel()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True),
        ("", True),
        ("write_through", True),
        ("WRITE_THROUGH", True),
        (" async ", False),
        ("Async", False),
    ],
)
def test_resolve_projection_write_through(raw: str | None, expected: bool) -> None:
    assert resolve_projection_write_through(raw) is expected


def test_resolve_projection_write_through_rejects_unknown_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        assert resolve_projection_write_through("eventually") is True
    assert any("NEXUS_PROJECTION_MODE" in r.getMessage() for r in caplog.records)
