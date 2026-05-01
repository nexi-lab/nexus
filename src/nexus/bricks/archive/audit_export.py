"""Audit-window export: slice activity events from #3791 store into bundle."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


class ActivityStoreReader(Protocol):
    def iter_events(self) -> list[dict[str, Any]]: ...


def write_activity_slice(
    bundle_dir: Path,
    *,
    activity_store: ActivityStoreReader,
    window_from: datetime,
    window_to: datetime,
) -> int:
    """Write events with `ts` in `[window_from, window_to)` to `activity/events.jsonl`.

    Returns the number of events written.
    Events with non-string or non-parseable `ts` are silently skipped.
    """
    out_dir = bundle_dir / "activity"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "events.jsonl"
    n = 0
    with out_path.open("w") as f:
        for event in activity_store.iter_events():
            ts_raw = event.get("ts")
            if not isinstance(ts_raw, str):
                continue
            try:
                ts = datetime.fromisoformat(ts_raw)
            except ValueError:
                continue
            if window_from <= ts < window_to:
                f.write(json.dumps(event) + "\n")
                n += 1
    return n


__all__ = ["ActivityStoreReader", "write_activity_slice"]
