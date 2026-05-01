"""Tests for audit-window export."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

from nexus.bricks.archive.audit_export import write_activity_slice


def test_write_activity_slice_filters_window(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    events = [
        {"id": "e1", "ts": "2026-04-15T00:00:00+00:00", "kind": "search"},
        {"id": "e2", "ts": "2026-04-20T00:00:00+00:00", "kind": "approval"},
        {"id": "e3", "ts": "2026-05-15T00:00:00+00:00", "kind": "search"},
    ]
    activity_store = MagicMock()
    activity_store.iter_events.return_value = events

    written = write_activity_slice(
        bundle_dir,
        activity_store=activity_store,
        window_from=datetime(2026, 4, 1, tzinfo=UTC),
        window_to=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert written == 2
    out_path = bundle_dir / "activity" / "events.jsonl"
    lines = [json.loads(line) for line in out_path.read_text().splitlines() if line]
    ids = [e["id"] for e in lines]
    assert ids == ["e1", "e2"]


def test_write_activity_slice_empty_window(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    activity_store = MagicMock()
    activity_store.iter_events.return_value = []
    n = write_activity_slice(
        bundle_dir,
        activity_store=activity_store,
        window_from=datetime(2026, 4, 1, tzinfo=UTC),
        window_to=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert n == 0
    assert (bundle_dir / "activity" / "events.jsonl").exists()
