from __future__ import annotations

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
