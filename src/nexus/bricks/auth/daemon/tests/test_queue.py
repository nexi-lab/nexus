from __future__ import annotations

import threading
from pathlib import Path

from nexus.bricks.auth.daemon.queue import PushQueue


def test_enqueue_and_list_pending(tmp_path: Path) -> None:
    q = PushQueue(tmp_path / "queue.db")
    q.enqueue("codex/u@x", payload_hash="aaaa")
    pending = q.list_pending()
    assert len(pending) == 1
    assert pending[0].profile_id == "codex/u@x"
    assert pending[0].attempts == 0


def test_dedupe_on_same_hash(tmp_path: Path) -> None:
    q = PushQueue(tmp_path / "queue.db")
    q.enqueue("codex/u@x", payload_hash="aaaa")
    q.enqueue("codex/u@x", payload_hash="aaaa")  # same hash → no-op dedupe
    assert len(q.list_pending()) == 1


def test_different_hash_updates(tmp_path: Path) -> None:
    q = PushQueue(tmp_path / "queue.db")
    q.enqueue("codex/u@x", payload_hash="aaaa")
    q.enqueue("codex/u@x", payload_hash="bbbb")
    pending = q.list_pending()
    assert len(pending) == 1
    assert pending[0].payload_hash == "bbbb"
    assert pending[0].attempts == 0  # reset on new content


def test_mark_success_clears(tmp_path: Path) -> None:
    q = PushQueue(tmp_path / "queue.db")
    q.enqueue("codex/u@x", payload_hash="aaaa")
    q.mark_success("codex/u@x", payload_hash="aaaa")
    assert q.list_pending() == []


def test_record_attempt_increments(tmp_path: Path) -> None:
    q = PushQueue(tmp_path / "queue.db")
    q.enqueue("codex/u@x", payload_hash="aaaa")
    q.record_attempt("codex/u@x", error="network")
    q.record_attempt("codex/u@x", error="network")
    pending = q.list_pending()
    assert pending[0].attempts == 2
    assert pending[0].last_error == "network"


def test_queue_is_thread_safe_under_concurrent_ops(tmp_path: Path) -> None:
    """Multiple threads hammering the queue must not raise SQLite errors.

    The daemon runs watcher + retry + subprocess-poll threads that all call
    into the same PushQueue. Before adding the RLock, this mix produced
    ``ProgrammingError: SQLite objects created in a thread can only be used
    in that same thread`` or interleaved commits under load. The test
    deliberately interleaves enqueue / record_attempt / mark_success to
    exercise the lock.
    """
    q = PushQueue(tmp_path / "queue.db")

    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            pid = f"codex/u{idx}@x"
            for h in range(20):
                q.enqueue(pid, payload_hash=f"h{idx}-{h}")
                q.record_attempt(pid, error="transient")
                if h % 3 == 0:
                    q.mark_success(pid, payload_hash=f"h{idx}-{h}")
                q.list_pending()
                q.last_pushed_hash(pid)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert errors == [], f"queue raised under concurrency: {errors!r}"
