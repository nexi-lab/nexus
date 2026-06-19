from __future__ import annotations

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
