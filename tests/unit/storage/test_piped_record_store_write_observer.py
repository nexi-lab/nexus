from __future__ import annotations

import pytest

from nexus.storage.piped_record_store_write_observer import RecordStoreWriteObserver


class FakeRecordStore:
    session_factory = object()


def test_on_write_accepts_dict_old_metadata_contract_shape() -> None:
    observer = RecordStoreWriteObserver(FakeRecordStore(), debounce_seconds=60)
    try:
        observer.on_write(
            {"content_id": "new", "path": "/workspace/report.csv"},
            is_new=False,
            path="/workspace/report.csv",
            old_metadata={"content_id": "old", "path": "/workspace/report.csv"},
            zone_id="zone-a",
            agent_id="agent-a",
        )

        with observer._lock:
            event = observer._pending[-1]

        assert event["snapshot_hash"] == "old"
        assert event["metadata_snapshot"] == {
            "content_id": "old",
            "path": "/workspace/report.csv",
        }
    finally:
        if observer._timer is not None:
            observer._timer.cancel()


def test_flush_batch_salvages_per_event_after_final_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#4645: one poison event must not drop the whole batch's audit rows."""
    from nexus.storage.piped_record_store_write_observer import _MAX_RETRIES

    observer = RecordStoreWriteObserver(FakeRecordStore(), debounce_seconds=60)
    poison = {"op": "rename", "path": "/deep/" + "x" * 100, "new_path": "/y"}
    good_a = {"op": "write", "path": "/a", "is_new": True, "metadata": {}}
    good_b = {"op": "delete", "path": "/b"}
    events = [good_a, poison, good_b]

    flushed_batches: list[list[dict]] = []

    def fake_flush(batch: list[dict]) -> None:
        if poison in batch:
            raise RuntimeError("value too long for type character varying(64)")
        flushed_batches.append(batch)

    monkeypatch.setattr(observer, "_flush_batch_sync", fake_flush)
    hook_calls: list[list[dict]] = []
    observer._post_flush_hooks = [hook_calls.append]

    # attempt == _MAX_RETRIES lands in the salvage branch directly.
    observer._flush_batch(events, attempt=_MAX_RETRIES)

    assert flushed_batches == [[good_a], [good_b]]
    assert observer._total_flushed == 2
    assert observer._total_failed == 1
    # Post-flush hooks still fire for the salvaged subset.
    assert hook_calls == [[good_a, good_b]]


def test_flush_sync_salvages_per_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """#4645: shutdown flush drops only the individually failing rows."""
    observer = RecordStoreWriteObserver(FakeRecordStore(), debounce_seconds=60)
    poison = {"op": "rename", "path": "/p", "new_path": "/q"}
    good = {"op": "write", "path": "/a", "is_new": True, "metadata": {}}

    def fake_flush(batch: list[dict]) -> None:
        if poison in batch:
            raise RuntimeError("boom")

    monkeypatch.setattr(observer, "_flush_batch_sync", fake_flush)
    with observer._lock:
        observer._pending.extend([good, poison])

    assert observer.flush_sync() == 1
    assert observer._total_flushed == 1
    assert observer._total_failed == 1


def test_on_delete_accepts_dict_metadata_contract_shape() -> None:
    observer = RecordStoreWriteObserver(FakeRecordStore(), debounce_seconds=60)
    try:
        observer.on_delete(
            path="/workspace/report.csv",
            metadata={"content_id": "old", "path": "/workspace/report.csv"},
            zone_id="zone-a",
            agent_id="agent-a",
        )

        with observer._lock:
            event = observer._pending[-1]

        assert event["snapshot_hash"] == "old"
        assert event["metadata_snapshot"] == {
            "content_id": "old",
            "path": "/workspace/report.csv",
        }
    finally:
        if observer._timer is not None:
            observer._timer.cancel()
