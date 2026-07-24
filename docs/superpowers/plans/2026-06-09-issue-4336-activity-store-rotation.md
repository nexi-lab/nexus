# Activity Store Rotation + Shedding + Sampling (issue #4336) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound the activity store's disk usage (per-day segment rotation + WAL caps), shed events under disk pressure before user writes fail, and add per-kind sampling — per spec `docs/superpowers/specs/2026-06-09-issue-4336-activity-db-rotation-design.md`.

**Architecture:** `SQLiteSink` writes to per-UTC-day segment files in `NEXUS_ACTIVITY_DIR`; retention unlinks expired segments (no DELETE/VACUUM) and auto-unlinks the frozen legacy `activity.db` once fully stale. The sink checks free disk (cached statvfs) and sheds batches below `NEXUS_ACTIVITY_MIN_FREE_MB`. `QueueEmitter` samples `Result.OK` events per kind, keeping Prometheus exact.

**Tech Stack:** Python stdlib `sqlite3`/`shutil`, `prometheus_client`, pytest + pytest-asyncio (`asyncio_mode=auto`, but existing tests use explicit `@pytest.mark.asyncio` — follow that style), `uv` for env/test runs.

**Spec:** `docs/superpowers/specs/2026-06-09-issue-4336-activity-db-rotation-design.md` — read it first.

---

## Task 0: Worktree environment setup

**Files:** none (environment only)

- [ ] **Step 0.1: Sync the venv** (worktrees start without one; a bare/stale `.venv` also breaks pre-commit — remove it first if present)

```bash
cd /Users/tafeng/nexus/.claude/worktrees/epic-mcclintock-ab608f
rm -rf .venv
uv sync
```

- [ ] **Step 0.2: Baseline — activity tests pass before any change**

Run: `uv run pytest tests/unit/services/activity tests/integration/services/activity -q`
Expected: all pass, 0 failures. If baseline fails, STOP and report — do not start on a broken base.

---

## Task 1: Config — segment dir, shedding threshold, sampling knobs

**Files:**
- Modify: `src/nexus/services/activity/config.py`
- Test: `tests/unit/services/activity/test_config.py`

- [ ] **Step 1.1: Write failing tests** — append to `tests/unit/services/activity/test_config.py`:

```python
def test_new_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "NEXUS_ACTIVITY_DB_PATH",
        "NEXUS_ACTIVITY_DIR",
        "NEXUS_ACTIVITY_MIN_FREE_MB",
        "NEXUS_ACTIVITY_SAMPLE_RATE",
        "NEXUS_ACTIVITY_SAMPLE_RATES",
        "NEXUS_DATA_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = ActivityConfig.from_env()
    assert cfg.segment_dir.name == "activity"
    # Segment dir is anchored next to the (default) db_path.
    assert cfg.segment_dir.parent == cfg.db_path.parent
    assert cfg.min_free_mb == 1024
    assert cfg.sample_rate == 1.0
    assert cfg.sample_rates == {}


def test_segment_dir_follows_custom_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators who pointed NEXUS_ACTIVITY_DB_PATH at a dedicated volume
    get segments on the same volume without setting NEXUS_ACTIVITY_DIR."""
    monkeypatch.delenv("NEXUS_ACTIVITY_DIR", raising=False)
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", "/mnt/telemetry/activity.db")
    cfg = ActivityConfig.from_env()
    assert str(cfg.segment_dir) == "/mnt/telemetry/activity"


def test_segment_dir_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_DIR", "/var/activity-segments")
    cfg = ActivityConfig.from_env()
    assert str(cfg.segment_dir) == "/var/activity-segments"


def test_min_free_mb_zero_allowed_negative_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_MIN_FREE_MB", "0")
    assert ActivityConfig.from_env().min_free_mb == 0
    monkeypatch.setenv("NEXUS_ACTIVITY_MIN_FREE_MB", "-1")
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_MIN_FREE_MB"):
        ActivityConfig.from_env()


@pytest.mark.parametrize("bad_value", ["-0.1", "1.5", "nan", "inf"])
def test_sample_rate_out_of_range_rejected(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_SAMPLE_RATE", bad_value)
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_SAMPLE_RATE"):
        ActivityConfig.from_env()


def test_sample_rates_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_SAMPLE_RATES", "search=0.05, mcp_tool_call=0.2")
    cfg = ActivityConfig.from_env()
    assert cfg.sample_rates == {"search": 0.05, "mcp_tool_call": 0.2}


def test_sample_rates_unknown_kind_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_SAMPLE_RATES", "bogus_kind=0.5")
    with pytest.raises(ValueError, match="unknown event kind"):
        ActivityConfig.from_env()


def test_sample_rates_malformed_entry_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_SAMPLE_RATES", "search:0.5")
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_SAMPLE_RATES"):
        ActivityConfig.from_env()


def test_sample_rates_out_of_range_value_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_SAMPLE_RATES", "search=2.0")
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_SAMPLE_RATES"):
        ActivityConfig.from_env()
```

- [ ] **Step 1.2: Run to verify failure**

Run: `uv run pytest tests/unit/services/activity/test_config.py -q`
Expected: new tests FAIL (`AttributeError: ... no attribute 'segment_dir'` / `TypeError` / no ValueError raised); pre-existing tests still pass.

- [ ] **Step 1.3: Implement** — in `src/nexus/services/activity/config.py`:

Add imports (top of file, after existing ones):

```python
from dataclasses import dataclass, field

from nexus.contracts.protocols.activity import EventKind
```

(replace the existing `from dataclasses import dataclass` line; keep `math`, `os`, `Path` imports.)

Add two helpers after `_parse_float`:

```python
def _validate_rate(name: str, rate: float) -> None:
    # NaN fails both comparisons, inf fails the upper bound — no separate
    # isfinite check needed.
    if not (0.0 <= rate <= 1.0):
        raise ValueError(f"{name} must be in [0.0, 1.0], got {rate}")


def _parse_sample_rates(raw: str | None) -> dict[str, float]:
    """Parse 'kind=rate,kind=rate' (e.g. 'search=0.05,mcp_tool_call=0.2')."""
    if raw is None or not raw.strip():
        return {}
    rates: dict[str, float] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"NEXUS_ACTIVITY_SAMPLE_RATES entries must be kind=rate, got {part!r}")
        rates[key] = _parse_float("NEXUS_ACTIVITY_SAMPLE_RATES", value.strip(), 1.0)
    return rates
```

Add fields to `ActivityConfig` (after `batch_timeout_s`):

```python
    segment_dir: Path = Path("./activity")
    min_free_mb: int = 1024
    sample_rate: float = 1.0
    sample_rates: dict[str, float] = field(default_factory=dict)
```

Append to `__post_init__` (after the retention_days check):

```python
if self.min_free_mb < 0:
    raise ValueError(f"NEXUS_ACTIVITY_MIN_FREE_MB must be >= 0, got {self.min_free_mb}")
_validate_rate("NEXUS_ACTIVITY_SAMPLE_RATE", self.sample_rate)
valid_kinds = {k.value for k in EventKind}
for key, rate in self.sample_rates.items():
    if key not in valid_kinds:
        raise ValueError(
            f"NEXUS_ACTIVITY_SAMPLE_RATES has unknown event kind {key!r}; "
            f"valid kinds: {sorted(valid_kinds)}"
        )
    _validate_rate(f"NEXUS_ACTIVITY_SAMPLE_RATES[{key!r}]", rate)
```

Rework `from_env` so `db_path` is computed first and anchors the segment-dir default:

```python
    @classmethod
    def from_env(cls) -> ActivityConfig:
        data_dir = os.environ.get("NEXUS_DATA_DIR", ".")
        default_db = Path(data_dir) / "activity.db"
        db_path = Path(os.environ.get("NEXUS_ACTIVITY_DB_PATH", str(default_db)))
        # Segments default to a sibling dir of the (possibly operator-moved)
        # db_path so a dedicated telemetry volume keeps everything together.
        default_segment_dir = db_path.parent / "activity"
        return cls(
            enabled=_parse_bool(os.environ.get("NEXUS_ACTIVITY_ENABLED"), True),
            db_path=db_path,
            segment_dir=Path(os.environ.get("NEXUS_ACTIVITY_DIR", str(default_segment_dir))),
            retention_days=_parse_int(
                "NEXUS_ACTIVITY_RETENTION_DAYS",
                os.environ.get("NEXUS_ACTIVITY_RETENTION_DAYS"),
                30,
            ),
            queue_size=_parse_int(
                "NEXUS_ACTIVITY_QUEUE_SIZE",
                os.environ.get("NEXUS_ACTIVITY_QUEUE_SIZE"),
                10_000,
            ),
            batch_size=_parse_int(
                "NEXUS_ACTIVITY_BATCH_SIZE",
                os.environ.get("NEXUS_ACTIVITY_BATCH_SIZE"),
                200,
            ),
            batch_timeout_s=_parse_float(
                "NEXUS_ACTIVITY_BATCH_TIMEOUT_S",
                os.environ.get("NEXUS_ACTIVITY_BATCH_TIMEOUT_S"),
                0.5,
            ),
            min_free_mb=_parse_int(
                "NEXUS_ACTIVITY_MIN_FREE_MB",
                os.environ.get("NEXUS_ACTIVITY_MIN_FREE_MB"),
                1024,
            ),
            sample_rate=_parse_float(
                "NEXUS_ACTIVITY_SAMPLE_RATE",
                os.environ.get("NEXUS_ACTIVITY_SAMPLE_RATE"),
                1.0,
            ),
            sample_rates=_parse_sample_rates(os.environ.get("NEXUS_ACTIVITY_SAMPLE_RATES")),
        )
```

Note: docstring of the module mentions env vars — extend it if it lists them. Update the dataclass docstring/comments only where they exist; don't invent new prose blocks.

- [ ] **Step 1.4: Run to verify pass**

Run: `uv run pytest tests/unit/services/activity/test_config.py -q`
Expected: ALL pass (old + new).

- [ ] **Step 1.5: Commit**

```bash
git add src/nexus/services/activity/config.py tests/unit/services/activity/test_config.py
git commit -m "feat(#4336): activity config for segment dir, disk floor, sampling"
```

---

## Task 2: Metrics — shed/sampled/segment counters + disk gauges

**Files:**
- Modify: `src/nexus/services/activity/metrics.py`
- Test: `tests/unit/services/activity/test_metrics.py`

- [ ] **Step 2.1: Write failing test** — append to `tests/unit/services/activity/test_metrics.py` (it already defines a module-level `_sample(metric, **labels)` helper — reuse it):

```python
def test_4336_metrics_registered_and_incrementable() -> None:
    from nexus.services.activity.metrics import (
        ACTIVITY_DISK_FREE_BYTES,
        ACTIVITY_SAMPLED_OUT,
        ACTIVITY_SEGMENTS_DELETED,
        ACTIVITY_SHED,
        ACTIVITY_STORE_BYTES,
    )

    before_shed = _sample(ACTIVITY_SHED)
    ACTIVITY_SHED.inc(5)
    assert _sample(ACTIVITY_SHED) == before_shed + 5

    before_sampled = _sample(ACTIVITY_SAMPLED_OUT)
    ACTIVITY_SAMPLED_OUT.inc()
    assert _sample(ACTIVITY_SAMPLED_OUT) == before_sampled + 1

    before_seg = _sample(ACTIVITY_SEGMENTS_DELETED)
    ACTIVITY_SEGMENTS_DELETED.inc(2)
    assert _sample(ACTIVITY_SEGMENTS_DELETED) == before_seg + 2

    ACTIVITY_DISK_FREE_BYTES.set(123456)
    assert _sample(ACTIVITY_DISK_FREE_BYTES) == 123456

    ACTIVITY_STORE_BYTES.set(789)
    assert _sample(ACTIVITY_STORE_BYTES) == 789
```

- [ ] **Step 2.2: Run to verify failure**

Run: `uv run pytest tests/unit/services/activity/test_metrics.py -q`
Expected: FAIL with `ImportError: cannot import name 'ACTIVITY_DISK_FREE_BYTES'`.

- [ ] **Step 2.3: Implement** — in `src/nexus/services/activity/metrics.py`, append after `ACTIVITY_RETENTION_PRUNED` (and add this comment line directly above `ACTIVITY_RETENTION_PRUNED`):

```python
# Superseded by ACTIVITY_SEGMENTS_DELETED (#4336): retention now deletes
# whole segment files, so a row-count metric no longer applies. Kept
# registered for dashboard compatibility; no longer incremented.
```

```python
ACTIVITY_SHED = Counter(
    "nexus_activity_shed_total",
    "Activity events dropped under disk pressure (#4336 shedding)",
)

ACTIVITY_SAMPLED_OUT = Counter(
    "nexus_activity_sampled_out_total",
    "Activity events skipped by sampling (Prometheus metrics still recorded)",
)

ACTIVITY_SEGMENTS_DELETED = Counter(
    "nexus_activity_segments_deleted_total",
    "Activity segment files (and the legacy activity.db) deleted by retention",
)

ACTIVITY_DISK_FREE_BYTES = Gauge(
    "nexus_activity_disk_free_bytes",
    "Last observed free bytes on the activity segment volume",
)

ACTIVITY_STORE_BYTES = Gauge(
    "nexus_activity_store_bytes",
    "Total bytes of activity segment files, WALs, and the legacy activity.db",
)
```

- [ ] **Step 2.4: Run to verify pass**

Run: `uv run pytest tests/unit/services/activity/test_metrics.py -q`
Expected: ALL pass.

- [ ] **Step 2.5: Commit**

```bash
git add src/nexus/services/activity/metrics.py tests/unit/services/activity/test_metrics.py
git commit -m "feat(#4336): activity metrics for shedding, sampling, segment retention"
```

---

## Task 3: SQLiteSink — per-day segment rotation + WAL cap + periodic checkpoint

**Files:**
- Modify: `src/nexus/services/activity/sinks/sqlite.py` (rewrite)
- Test: `tests/unit/services/activity/test_sqlite_sink.py` (rewrite)

- [ ] **Step 3.1: Rewrite the test file** — replace the full contents of `tests/unit/services/activity/test_sqlite_sink.py` with:

```python
"""Unit tests for the segmented SQLiteSink (#4336)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus.services.activity.events import ActivityEvent, Actor, EventKind, Result, Subject
from nexus.services.activity.sinks.sqlite import SQLiteSink, segment_path


class FakeClock:
    """Mutable UTC clock for rollover tests."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _clock(iso: str) -> FakeClock:
    return FakeClock(datetime.fromisoformat(iso).replace(tzinfo=UTC))


def _event(eid: str, **kwargs: object) -> ActivityEvent:
    defaults: dict[str, object] = {
        "id": eid,
        "ts": "2026-06-09T00:00:00+00:00",
        "kind": EventKind.SEARCH,
        "result": Result.OK,
    }
    defaults.update(kwargs)
    return ActivityEvent(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_schema_bootstrapped_in_todays_segment(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        seg = segment_path(tmp_path, clock.now.date())
        assert seg.name == "activity-2026-06-09.db"
        assert seg.exists()
        conn = sqlite3.connect(seg)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_events'"
        )
        assert cursor.fetchone() is not None
        idx = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='activity_events'"
            )
        }
        assert {"idx_ae_ts", "idx_ae_kind_ts", "idx_ae_token_ts", "idx_ae_zone_ts"} <= idx
        conn.close()
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_pragmas_applied_including_wal_cap(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        seg = segment_path(tmp_path, clock.now.date())
        conn = sqlite3.connect(seg)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        conn.close()
        # Per-connection pragmas checked through the sink's own connection.
        assert sink._conn is not None  # noqa: SLF001
        assert sink._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # noqa: SLF001
        assert (
            sink._conn.execute("PRAGMA journal_size_limit").fetchone()[0]  # noqa: SLF001
            == 67108864
        )
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_batch_insert_roundtrip(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        events = [
            _event(
                "1",
                latency_ms=10,
                trace_id="t1",
                actor=Actor(token_hash="aaa", agent="claude", user="alice"),
                subject=Subject(zone="eng", extra={"q": "foo"}),
                meta={"x": 1},
            ),
            _event("2", kind=EventKind.MCP_TOOL_CALL),
        ]
        await sink.write_batch(events)
    finally:
        await sink.close()

    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    rows = list(
        conn.execute(
            "SELECT id, kind, result, subject_zone, subject_extra, meta "
            "FROM activity_events ORDER BY id"
        )
    )
    conn.close()
    assert rows[0][0] == "1"
    assert rows[0][1] == "search"
    assert rows[0][3] == "eng"
    assert json.loads(rows[0][4]) == {"q": "foo"}
    assert json.loads(rows[0][5]) == {"x": 1}
    assert rows[1][0] == "2"
    assert rows[1][3] is None


@pytest.mark.asyncio
async def test_midnight_rollover_splits_segments(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T23:59:59")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        await sink.write_batch([_event("d1")])
        clock.now = datetime.fromisoformat("2026-06-10T00:00:01").replace(tzinfo=UTC)
        await sink.write_batch([_event("d2")])
    finally:
        await sink.close()

    seg1 = segment_path(tmp_path, datetime.fromisoformat("2026-06-09").date())
    seg2 = segment_path(tmp_path, datetime.fromisoformat("2026-06-10").date())
    assert seg1.exists() and seg2.exists()

    def _ids(p: Path) -> set[str]:
        conn = sqlite3.connect(p)
        ids = {r[0] for r in conn.execute("SELECT id FROM activity_events")}
        conn.close()
        return ids

    assert _ids(seg1) == {"d1"}
    assert _ids(seg2) == {"d2"}


@pytest.mark.asyncio
async def test_rollover_removes_old_segments_wal(tmp_path: Path) -> None:
    """Closing the previous segment checkpoints it: its -wal must be gone
    (or empty) so closed segments hold no hidden WAL bytes."""
    clock = _clock("2026-06-09T23:59:59")
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    try:
        await sink.write_batch([_event("d1")])
        clock.now = datetime.fromisoformat("2026-06-10T00:00:01").replace(tzinfo=UTC)
        await sink.write_batch([_event("d2")])
        wal1 = Path(f"{segment_path(tmp_path, datetime.fromisoformat('2026-06-09').date())}-wal")
        assert not wal1.exists() or wal1.stat().st_size == 0
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_periodic_checkpoint_truncates_wal(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    # interval 0 → checkpoint after every batch.
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock, checkpoint_interval_s=0.0)
    try:
        await sink.write_batch([_event(f"e{i}") for i in range(50)])
        wal = Path(f"{segment_path(tmp_path, clock.now.date())}-wal")
        assert not wal.exists() or wal.stat().st_size == 0
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_open_idempotent_on_existing_segment(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    sink1 = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    await sink1.write_batch([_event("1")])
    await sink1.close()
    sink2 = SQLiteSink(segment_dir=tmp_path, now_fn=clock)
    await sink2.write_batch([_event("2")])
    await sink2.close()
    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    count = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    conn.close()
    assert count == 2


@pytest.mark.asyncio
async def test_corrupt_todays_segment_raises_at_construction(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    segment_path(tmp_path, clock.now.date()).write_bytes(b"not a sqlite file")
    with pytest.raises(sqlite3.DatabaseError):
        SQLiteSink(segment_dir=tmp_path, now_fn=clock)
```

- [ ] **Step 3.2: Run to verify failure**

Run: `uv run pytest tests/unit/services/activity/test_sqlite_sink.py -q`
Expected: FAIL with `ImportError: cannot import name 'segment_path'` (and `TypeError: ... unexpected keyword argument 'segment_dir'` once imports resolve).

- [ ] **Step 3.3: Implement** — replace the full contents of `src/nexus/services/activity/sinks/sqlite.py` with:

```python
"""Append-only SQLite sink for activity events, segmented per UTC day.

Single-writer connection (``check_same_thread=False`` because writes are
dispatched via ``asyncio.to_thread`` so the executor thread, not the loop
thread, owns each ``executemany`` call). Caller is the activity worker,
which serializes calls — there is never more than one writer thread at a
time. Keeping I/O off the loop ensures a SQLite busy-wait cannot stall
unrelated request handlers.

Issue #4336: a single ever-growing activity.db made disk reclaim
impossible (DELETE never shrinks the file; VACUUM needs ~db-size free
space). The sink therefore writes to per-day segment files
(``<segment_dir>/activity-YYYY-MM-DD.db``) that retention deletes whole —
O(1) reclaim, no VACUUM. ``journal_size_limit`` plus a periodic
``wal_checkpoint(TRUNCATE)`` (run on the writer's own executor thread, so
it never contends with a concurrent writer) keep the WAL bounded. Under
disk pressure the sink sheds batches instead of filling the volume that
user file payloads share.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from nexus.services.activity.events import ActivityEvent

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_events (
    id              TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    kind            TEXT NOT NULL,
    result          TEXT NOT NULL,
    latency_ms      INTEGER,
    trace_id        TEXT,
    actor_token_hash TEXT,
    actor_agent     TEXT,
    actor_user      TEXT,
    subject_zone    TEXT,
    subject_extra   TEXT,
    meta            TEXT
) STRICT;
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_ae_ts        ON activity_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_ae_kind_ts   ON activity_events(kind, ts)",
    "CREATE INDEX IF NOT EXISTS idx_ae_token_ts  ON activity_events(actor_token_hash, ts)",
    "CREATE INDEX IF NOT EXISTS idx_ae_zone_ts   ON activity_events(subject_zone, ts)",
)

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA busy_timeout=5000",
    # #4336: cap the WAL high-water mark — TRUNCATE checkpoints shrink the
    # file back under this limit instead of leaving a multi-GB WAL behind.
    "PRAGMA journal_size_limit=67108864",
)

_INSERT_SQL = (
    "INSERT OR IGNORE INTO activity_events "
    "(id, ts, kind, result, latency_ms, trace_id, "
    " actor_token_hash, actor_agent, actor_user, "
    " subject_zone, subject_extra, meta) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# How often the (cheap but syscall-bound) statvfs runs at most.
_FREE_CHECK_INTERVAL_S = 10.0


def segment_path(segment_dir: Path, day: date) -> Path:
    """Canonical segment file path for a UTC day (shared with retention)."""
    return segment_dir / f"activity-{day.isoformat()}.db"


class SQLiteSink:
    """Durable append-only sink over per-day segments. Bootstrap idempotent."""

    def __init__(
        self,
        *,
        segment_dir: Path | str,
        min_free_bytes: int = 0,
        checkpoint_interval_s: float = 300.0,
        now_fn: Callable[[], datetime] | None = None,
        disk_usage_fn: Callable[[Path], Any] | None = None,
    ) -> None:
        self._segment_dir = Path(segment_dir)
        self._segment_dir.mkdir(parents=True, exist_ok=True)
        self._min_free_bytes = min_free_bytes
        self._checkpoint_interval_s = checkpoint_interval_s
        self._now_fn = now_fn or (lambda: datetime.now(tz=UTC))
        self._disk_usage_fn = disk_usage_fn or shutil.disk_usage
        self._conn: sqlite3.Connection | None = None
        self._segment_date: date | None = None
        self._last_checkpoint = time.monotonic()
        self._free_cached: int | None = None
        self._free_checked_at: float | None = None
        self._shedding = False
        # Eager open keeps lifespan's NoopSink-fallback contract: a broken
        # store directory fails construction, not the first write.
        self._open_segment(self._today())

    def _today(self) -> date:
        return self._now_fn().date()

    def _open_segment(self, day: date) -> None:
        path = segment_path(self._segment_dir, day)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            for pragma in _PRAGMAS:
                conn.execute(pragma)
            conn.execute(_SCHEMA)
            for stmt in _INDEXES:
                conn.execute(stmt)
        except sqlite3.Error:
            conn.close()
            raise
        self._conn = conn
        self._segment_date = day
        logger.info("activity segment open at %s", path)

    async def write_batch(self, events: Sequence[ActivityEvent]) -> None:
        if not events:
            return
        rows = [
            (
                e.id,
                e.ts,
                e.kind.value,
                e.result.value,
                e.latency_ms,
                e.trace_id,
                e.actor.token_hash,
                e.actor.agent,
                e.actor.user,
                e.subject.zone,
                json.dumps(e.subject.extra) if e.subject.extra is not None else None,
                json.dumps(e.meta) if e.meta is not None else None,
            )
            for e in events
        ]
        # Shield the executor write from task cancellation: the to_thread
        # future cannot stop the underlying thread, so cancelling here would
        # leave a partial executemany running while shutdown closes the same
        # connection in close(). asyncio.shield ensures the future completes
        # before the awaiter is cancelled.
        try:
            await asyncio.shield(asyncio.to_thread(self._write_rows, rows))
        except sqlite3.Error:
            logger.warning("activity SQLiteSink batch insert failed", exc_info=True)
            raise

    def _write_rows(self, rows: Sequence[tuple[object, ...]]) -> None:
        """Executor-thread write path: shed check → rollover → insert.

        A failed segment open propagates: the worker counts it in
        ACTIVITY_SINK_ERRORS and the next batch retries the open, so the
        worker itself never crashes.
        """
        if self._shed(len(rows)):
            return
        today = self._today()
        if self._conn is None or today != self._segment_date:
            self._rollover(today)
        conn = self._conn
        if conn is None:  # pragma: no cover - _rollover either set it or raised
            raise sqlite3.OperationalError("activity segment connection unavailable")
        conn.executemany(_INSERT_SQL, rows)
        self._maybe_checkpoint(conn)

    def _rollover(self, day: date) -> None:
        if self._conn is not None:
            old = self._segment_date
            try:
                # Closing the last connection checkpoints and removes the
                # old segment's WAL — closed segments carry no WAL bytes.
                self._conn.close()
            except sqlite3.Error:
                logger.warning("activity segment close failed for %s", old, exc_info=True)
            self._conn = None
            self._segment_date = None
        self._open_segment(day)

    def _maybe_checkpoint(self, conn: sqlite3.Connection) -> None:
        now = time.monotonic()
        if now - self._last_checkpoint < self._checkpoint_interval_s:
            return
        self._last_checkpoint = now
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            # Non-fatal: a busy checkpoint simply retries next interval.
            logger.debug("activity WAL checkpoint failed", exc_info=True)

    def _shed(self, n: int) -> bool:
        """True when the batch must be dropped to protect the shared volume."""
        if self._min_free_bytes <= 0:
            return False
        free = self._free_bytes()
        if free is None:
            return False  # cannot stat → never shed on monitoring failure
        if free >= self._min_free_bytes:
            if self._shedding:
                self._shedding = False
                logger.info(
                    "activity disk pressure cleared (%d bytes free); resuming writes",
                    free,
                )
            return False
        if not self._shedding:
            self._shedding = True
            logger.error(
                "activity disk pressure: %d bytes free < %d minimum — "
                "shedding activity events to protect user file writes",
                free,
                self._min_free_bytes,
            )
        try:
            from nexus.services.activity.metrics import ACTIVITY_SHED

            ACTIVITY_SHED.inc(n)
        except Exception:
            pass
        return True

    def _free_bytes(self) -> int | None:
        now = time.monotonic()
        if (
            self._free_checked_at is not None
            and now - self._free_checked_at < _FREE_CHECK_INTERVAL_S
        ):
            return self._free_cached
        self._free_checked_at = now
        try:
            free = int(self._disk_usage_fn(self._segment_dir).free)
        except OSError:
            logger.debug("activity disk_usage failed", exc_info=True)
            self._free_cached = None
            return None
        self._free_cached = free
        try:
            from nexus.services.activity.metrics import ACTIVITY_DISK_FREE_BYTES

            ACTIVITY_DISK_FREE_BYTES.set(free)
        except Exception:
            pass
        return free

    async def close(self) -> None:
        conn = self._conn
        self._conn = None
        self._segment_date = None
        if conn is None:
            return
        try:
            await asyncio.to_thread(conn.close)
        except sqlite3.Error:
            logger.warning("activity SQLiteSink close failed", exc_info=True)
```

(Note: `_shed`/`_free_bytes` land fully here even though their tests come in Task 4 — keeping the file whole avoids a second rewrite. `min_free_bytes` defaults to 0 = disabled, so Task 3 tests are unaffected by shedding.)

- [ ] **Step 3.4: Run to verify pass**

Run: `uv run pytest tests/unit/services/activity/test_sqlite_sink.py -q`
Expected: ALL pass.

- [ ] **Step 3.5: Commit**

```bash
git add src/nexus/services/activity/sinks/sqlite.py tests/unit/services/activity/test_sqlite_sink.py
git commit -m "feat(#4336): per-day segment rotation + WAL cap in activity SQLiteSink"
```

---

## Task 4: SQLiteSink — disk-pressure shedding tests

**Files:**
- Test: `tests/unit/services/activity/test_sqlite_sink.py` (append; implementation landed in Task 3)

- [ ] **Step 4.1: Write the shedding tests** — append to `tests/unit/services/activity/test_sqlite_sink.py`:

```python
class FakeDiskUsage:
    """Mutable disk_usage stand-in; .free is the only consumed field."""

    def __init__(self, free: int) -> None:
        self.free = free

    def __call__(self, _path: Path) -> FakeDiskUsage:
        return self


@pytest.mark.asyncio
async def test_shedding_drops_batch_below_min_free(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _clock("2026-06-09T12:00:00")
    disk = FakeDiskUsage(free=10)
    sink = SQLiteSink(
        segment_dir=tmp_path,
        now_fn=clock,
        min_free_bytes=1024,
        disk_usage_fn=disk,
    )
    try:
        with caplog.at_level("ERROR", logger="nexus.services.activity.sinks.sqlite"):
            await sink.write_batch([_event("1"), _event("2")])
            await sink.write_batch([_event("3")])
    finally:
        await sink.close()

    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    count = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    conn.close()
    assert count == 0  # everything shed
    shed_logs = [r for r in caplog.records if "disk pressure" in r.message]
    assert len(shed_logs) == 1  # edge-triggered: one ERROR, not one per batch


@pytest.mark.asyncio
async def test_shedding_recovers_when_space_returns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _clock("2026-06-09T12:00:00")
    disk = FakeDiskUsage(free=10)
    sink = SQLiteSink(
        segment_dir=tmp_path,
        now_fn=clock,
        min_free_bytes=1024,
        disk_usage_fn=disk,
    )
    try:
        await sink.write_batch([_event("dropped")])
        disk.free = 10 * 1024 * 1024
        sink._free_checked_at = None  # noqa: SLF001 — bypass the 10s statvfs cache
        with caplog.at_level("INFO", logger="nexus.services.activity.sinks.sqlite"):
            await sink.write_batch([_event("kept")])
    finally:
        await sink.close()

    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    ids = {r[0] for r in conn.execute("SELECT id FROM activity_events")}
    conn.close()
    assert ids == {"kept"}
    assert any("disk pressure cleared" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_shedding_disabled_with_zero_min_free(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    disk = FakeDiskUsage(free=0)
    sink = SQLiteSink(segment_dir=tmp_path, now_fn=clock, min_free_bytes=0, disk_usage_fn=disk)
    try:
        await sink.write_batch([_event("1")])
    finally:
        await sink.close()
    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    count = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    conn.close()
    assert count == 1


@pytest.mark.asyncio
async def test_statvfs_failure_never_sheds(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")

    def _broken_disk_usage(_path: Path) -> object:
        raise OSError("statvfs unavailable")

    sink = SQLiteSink(
        segment_dir=tmp_path,
        now_fn=clock,
        min_free_bytes=1024,
        disk_usage_fn=_broken_disk_usage,
    )
    try:
        await sink.write_batch([_event("1")])
    finally:
        await sink.close()
    conn = sqlite3.connect(segment_path(tmp_path, clock.now.date()))
    count = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    conn.close()
    assert count == 1


@pytest.mark.asyncio
async def test_free_space_check_is_cached(tmp_path: Path) -> None:
    clock = _clock("2026-06-09T12:00:00")
    calls = {"n": 0}

    class CountingDisk:
        free = 10 * 1024 * 1024

        def __call__(self, _path: Path) -> CountingDisk:
            calls["n"] += 1
            return self

    sink = SQLiteSink(
        segment_dir=tmp_path,
        now_fn=clock,
        min_free_bytes=1024,
        disk_usage_fn=CountingDisk(),
    )
    try:
        await sink.write_batch([_event("1")])
        await sink.write_batch([_event("2")])
        await sink.write_batch([_event("3")])
    finally:
        await sink.close()
    assert calls["n"] == 1  # 10s cache → one statvfs for three batches
```

- [ ] **Step 4.2: Run to verify pass** (implementation already exists from Task 3 — these tests validate it; if any fail, fix `_shed`/`_free_bytes` in `sqlite.py`, not the tests)

Run: `uv run pytest tests/unit/services/activity/test_sqlite_sink.py -q`
Expected: ALL pass.

- [ ] **Step 4.3: Commit**

```bash
git add tests/unit/services/activity/test_sqlite_sink.py
git commit -m "test(#4336): disk-pressure shedding coverage for SQLiteSink"
```

---

## Task 5: Retention — segment sweep + legacy freeze/auto-unlink

**Files:**
- Modify: `src/nexus/services/activity/retention.py` (rewrite)
- Test: `tests/unit/services/activity/test_retention.py` (rewrite)

- [ ] **Step 5.1: Rewrite the test file** — replace the full contents of `tests/unit/services/activity/test_retention.py` with:

```python
"""Unit tests for segment-based retention (#4336)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.services.activity.retention import RetentionTask, sweep_expired

_NOW = datetime(2026, 6, 9, 18, 0, 0, tzinfo=UTC)


def _now_fn() -> datetime:
    return _NOW


def _make_db(path: Path, ts_values: list[str] | None = None) -> None:
    """Create a minimal activity_events db with optional rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE activity_events (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT, result TEXT,
            latency_ms INTEGER, trace_id TEXT, actor_token_hash TEXT,
            actor_agent TEXT, actor_user TEXT, subject_zone TEXT,
            subject_extra TEXT, meta TEXT
        ) STRICT"""
    )
    for i, ts in enumerate(ts_values or []):
        conn.execute("INSERT INTO activity_events (id, ts) VALUES (?, ?)", (f"r{i}", ts))
    conn.commit()
    conn.close()


def _make_segment(seg_dir: Path, day: str) -> Path:
    path = seg_dir / f"activity-{day}.db"
    _make_db(path, [f"{day}T12:00:00+00:00"])
    return path


def test_sweep_unlinks_expired_segments_and_siblings(tmp_path: Path) -> None:
    seg_dir = tmp_path / "activity"
    old = _make_segment(seg_dir, "2026-05-01")
    Path(f"{old}-wal").write_bytes(b"wal")
    Path(f"{old}-shm").write_bytes(b"shm")
    boundary = _make_segment(seg_dir, "2026-05-10")  # cutoff date itself: kept
    fresh = _make_segment(seg_dir, "2026-06-08")
    deleted = sweep_expired(
        segment_dir=seg_dir, legacy_db_path=None, retention_days=30, now_fn=_now_fn
    )
    assert deleted == 1
    assert not old.exists()
    assert not Path(f"{old}-wal").exists()
    assert not Path(f"{old}-shm").exists()
    assert boundary.exists()  # may still hold rows newer than cutoff ts
    assert fresh.exists()


def test_sweep_retention_zero_is_noop(tmp_path: Path) -> None:
    seg_dir = tmp_path / "activity"
    old = _make_segment(seg_dir, "2020-01-01")
    deleted = sweep_expired(
        segment_dir=seg_dir, legacy_db_path=None, retention_days=0, now_fn=_now_fn
    )
    assert deleted == 0
    assert old.exists()


def test_sweep_missing_dir_is_noop(tmp_path: Path) -> None:
    deleted = sweep_expired(
        segment_dir=tmp_path / "nope",
        legacy_db_path=None,
        retention_days=30,
        now_fn=_now_fn,
    )
    assert deleted == 0


def test_sweep_skips_non_segment_filenames(tmp_path: Path) -> None:
    seg_dir = tmp_path / "activity"
    seg_dir.mkdir()
    odd = seg_dir / "activity-notadate.db"
    odd.write_bytes(b"")
    deleted = sweep_expired(
        segment_dir=seg_dir, legacy_db_path=None, retention_days=30, now_fn=_now_fn
    )
    assert deleted == 0
    assert odd.exists()


def test_legacy_unlinked_when_fully_stale(tmp_path: Path) -> None:
    legacy = tmp_path / "activity.db"
    _make_db(legacy, ["2026-04-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00"])
    Path(f"{legacy}-wal").write_bytes(b"wal")
    deleted = sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=_now_fn,
    )
    assert deleted == 1
    assert not legacy.exists()
    assert not Path(f"{legacy}-wal").exists()


def test_legacy_kept_while_fresh_rows_remain(tmp_path: Path) -> None:
    legacy = tmp_path / "activity.db"
    _make_db(legacy, ["2026-04-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"])
    deleted = sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=_now_fn,
    )
    assert deleted == 0
    assert legacy.exists()


def test_legacy_empty_table_unlinked(tmp_path: Path) -> None:
    legacy = tmp_path / "activity.db"
    _make_db(legacy, [])
    deleted = sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=_now_fn,
    )
    assert deleted == 1
    assert not legacy.exists()


def test_legacy_corrupt_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = tmp_path / "activity.db"
    legacy.write_bytes(b"not a sqlite file")
    with caplog.at_level("WARNING"):
        deleted = sweep_expired(
            segment_dir=tmp_path / "activity",
            legacy_db_path=legacy,
            retention_days=30,
            now_fn=_now_fn,
        )
    assert deleted == 0
    assert legacy.exists()
    assert any("unreadable" in r.message for r in caplog.records)


def test_legacy_missing_table_counts_as_expired(tmp_path: Path) -> None:
    legacy = tmp_path / "activity.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    deleted = sweep_expired(
        segment_dir=tmp_path / "activity",
        legacy_db_path=legacy,
        retention_days=30,
        now_fn=_now_fn,
    )
    assert deleted == 1
    assert not legacy.exists()


@pytest.mark.asyncio
async def test_retention_task_zero_disables_start(tmp_path: Path) -> None:
    task = RetentionTask(
        segment_dir=tmp_path / "activity",
        legacy_db_path=None,
        retention_days=0,
        interval_s=0.01,
    )
    await task.start()
    assert task._task is None  # noqa: SLF001
    await task.stop()


@pytest.mark.asyncio
async def test_retention_task_runs_and_deletes(tmp_path: Path) -> None:
    seg_dir = tmp_path / "activity"
    old = _make_segment(seg_dir, "2020-01-01")
    fresh_day = datetime.now(tz=UTC).date().isoformat()
    fresh = _make_segment(seg_dir, fresh_day)
    task = RetentionTask(
        segment_dir=seg_dir, legacy_db_path=None, retention_days=30, interval_s=0.05
    )
    await task.start()
    # Poll instead of fixed sleep — under xdist load the task body may not
    # run within a small hard-coded window.
    for _ in range(200):
        if task.total_deleted >= 1:
            break
        await asyncio.sleep(0.02)
    await task.stop()
    assert task.total_deleted == 1
    assert not old.exists()
    assert fresh.exists()


@pytest.mark.asyncio
async def test_retention_task_stop_before_start_is_safe(tmp_path: Path) -> None:
    task = RetentionTask(
        segment_dir=tmp_path / "activity",
        legacy_db_path=None,
        retention_days=30,
        interval_s=0.01,
    )
    await task.stop()  # must not raise


@pytest.mark.asyncio
async def test_retention_task_double_start_is_idempotent(tmp_path: Path) -> None:
    task = RetentionTask(
        segment_dir=tmp_path / "activity",
        legacy_db_path=None,
        retention_days=30,
        interval_s=0.01,
    )
    await task.start()
    first_task = task._task  # noqa: SLF001
    await task.start()
    assert task._task is first_task  # noqa: SLF001
    await task.stop()
```

- [ ] **Step 5.2: Run to verify failure**

Run: `uv run pytest tests/unit/services/activity/test_retention.py -q`
Expected: FAIL with `ImportError: cannot import name 'sweep_expired'`.

- [ ] **Step 5.3: Implement** — replace the full contents of `src/nexus/services/activity/retention.py` with:

```python
"""Retention for the segmented activity store: delete expired segment files.

Issue #4336: DELETE-based pruning never returned disk (SQLite keeps pages
until VACUUM, and VACUUM needs ~db-size free space — impossible once the
db passes ~50% of the volume). Per-day segment files flip that: a whole
expired day is reclaimed with one unlink, O(1) and VACUUM-free.

The legacy single-file ``activity.db`` from pre-segment deployments is
frozen (nothing writes to it anymore) and unlinked once its newest row is
older than the retention cutoff — the same data-loss semantics the old
DELETE prune already enforced, without DELETE/WAL churn on a potentially
huge file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_SEGMENT_RE = re.compile(r"^activity-(\d{4}-\d{2}-\d{2})\.db$")


def _unlink_db_files(db: Path) -> None:
    """Remove a SQLite db plus its -wal/-shm siblings, ignoring absences."""
    for path in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _legacy_expired(db: Path, cutoff_iso: str) -> bool:
    """True when every row in the legacy db is older than the cutoff.

    Missing table and empty table both count as expired (nothing left to
    retain). Locked/corrupt files return False so the sweep retries on a
    later tick instead of deleting data it could not inspect.
    """
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT max(ts) FROM activity_events").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return True
        logger.warning("activity legacy db %s unreadable; skipping", db, exc_info=True)
        return False
    except sqlite3.Error:
        logger.warning("activity legacy db %s unreadable; skipping", db, exc_info=True)
        return False
    max_ts = row[0] if row else None
    # ts is ISO-8601 UTC (see emitter._now_iso), so lexicographic
    # comparison matches chronological order.
    return max_ts is None or max_ts < cutoff_iso


def _update_store_bytes(seg_dir: Path, legacy: Path | None) -> None:
    total = 0
    try:
        if seg_dir.is_dir():
            for path in seg_dir.glob("activity-*.db*"):
                with contextlib.suppress(OSError):
                    total += path.stat().st_size
        if legacy is not None:
            for path in (legacy, Path(f"{legacy}-wal"), Path(f"{legacy}-shm")):
                with contextlib.suppress(OSError):
                    if path.is_file():
                        total += path.stat().st_size
        from nexus.services.activity.metrics import ACTIVITY_STORE_BYTES

        ACTIVITY_STORE_BYTES.set(total)
    except Exception:  # metrics must never break retention
        pass


def sweep_expired(
    *,
    segment_dir: Path | str,
    legacy_db_path: Path | str | None,
    retention_days: int,
    now_fn: Callable[[], datetime] | None = None,
) -> int:
    """Delete expired segment files (and the legacy db once fully stale).

    Returns the number of database files unlinked. ``retention_days <= 0``
    disables retention entirely.

    A segment ``activity-D.db`` holds rows up to ``D 23:59:59``, so it is
    deleted only when ``D < (now - retention_days).date()`` — i.e. when its
    newest possible row has expired. Worst-case over-retention is <24h
    (the old DELETE prune was exact-to-the-hour; acceptable coarsening).
    """
    if retention_days <= 0:
        return 0
    now = (now_fn or (lambda: datetime.now(tz=UTC)))()
    cutoff = now - timedelta(days=retention_days)
    cutoff_date = cutoff.date()
    seg_dir = Path(segment_dir)
    deleted = 0

    if seg_dir.is_dir():
        for path in sorted(seg_dir.glob("activity-*.db")):
            match = _SEGMENT_RE.match(path.name)
            if match is None:
                continue
            try:
                seg_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if seg_date >= cutoff_date:
                continue
            try:
                _unlink_db_files(path)
                deleted += 1
            except OSError:
                logger.warning("activity segment unlink failed for %s", path, exc_info=True)

    legacy = Path(legacy_db_path) if legacy_db_path is not None else None
    if legacy is not None and legacy.is_file() and _legacy_expired(legacy, cutoff.isoformat()):
        try:
            _unlink_db_files(legacy)
            deleted += 1
            logger.info("activity legacy db %s fully expired; deleted", legacy)
        except OSError:
            logger.error("activity legacy db unlink failed for %s", legacy, exc_info=True)

    if deleted:
        try:
            from nexus.services.activity.metrics import ACTIVITY_SEGMENTS_DELETED

            ACTIVITY_SEGMENTS_DELETED.inc(deleted)
        except Exception:
            pass
    _update_store_bytes(seg_dir, legacy)
    return deleted


class RetentionTask:
    """Async task wrapping sweep_expired on a fixed cadence."""

    def __init__(
        self,
        *,
        segment_dir: Path | str,
        legacy_db_path: Path | str | None,
        retention_days: int,
        interval_s: float = 3600.0,
    ) -> None:
        self._segment_dir = segment_dir
        self._legacy_db_path = legacy_db_path
        self._retention_days = retention_days
        self._interval_s = interval_s
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._total_deleted = 0

    @property
    def total_deleted(self) -> int:
        return self._total_deleted

    async def start(self) -> None:
        if self._retention_days <= 0:
            logger.info("activity retention disabled (retention_days=%d)", self._retention_days)
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Wait for the in-flight sweep (if any) to finish, then exit.

        Cancellation cannot stop the executor thread mid-sweep, so a cancel
        here would let the thread keep unlinking files after stop()
        returns. Setting the stopping flag is sufficient: ``_run`` checks
        the flag between iterations and waits on it instead of sleeping, so
        the loop exits as soon as the current sweep finishes.
        """
        self._stopping.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                deleted = await asyncio.to_thread(
                    sweep_expired,
                    segment_dir=self._segment_dir,
                    legacy_db_path=self._legacy_db_path,
                    retention_days=self._retention_days,
                )
                self._total_deleted += deleted
            except Exception:
                logger.warning("activity retention loop tick failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue
```

- [ ] **Step 5.4: Run to verify pass** (lifespan.py still passes old kwargs — its tests break until Task 7; only run the retention tests here)

Run: `uv run pytest tests/unit/services/activity/test_retention.py -q`
Expected: ALL pass.

- [ ] **Step 5.5: Commit**

```bash
git add src/nexus/services/activity/retention.py tests/unit/services/activity/test_retention.py
git commit -m "feat(#4336): segment-unlink retention + legacy activity.db auto-cleanup"
```

---

## Task 6: QueueEmitter — per-kind sampling (audit-exempt, metrics-exact)

**Files:**
- Modify: `src/nexus/services/activity/emitter.py`
- Test: `tests/unit/services/activity/test_emitter.py`

- [ ] **Step 6.1: Write failing tests** — append to `tests/unit/services/activity/test_emitter.py`:

```python
class _FixedRandom:
    """random.Random stand-in returning a fixed value from random()."""

    def __init__(self, value: float) -> None:
        self._value = value

    def random(self) -> float:
        return self._value


def test_sampling_drops_ok_events_below_rate() -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=16)
    emitter = QueueEmitter(queue=queue, sample_rate=0.5, rng=_FixedRandom(0.9))
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    assert queue.qsize() == 0
    assert emitter.drop_count == 0  # sampled-out is intentional, not a drop


def test_sampling_keeps_ok_events_at_or_above_rate() -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=16)
    emitter = QueueEmitter(queue=queue, sample_rate=0.5, rng=_FixedRandom(0.1))
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    assert queue.qsize() == 1


def test_per_kind_rate_overrides_global() -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=16)
    emitter = QueueEmitter(
        queue=queue,
        sample_rate=1.0,
        sample_rates={"search": 0.0},
        rng=_FixedRandom(0.5),
    )
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    emitter.emit(kind=EventKind.MCP_TOOL_CALL, result=Result.OK)
    assert queue.qsize() == 1  # search sampled out, mcp kept


def test_non_ok_results_never_sampled_out() -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=16)
    emitter = QueueEmitter(queue=queue, sample_rate=0.0, rng=_FixedRandom(0.5))
    emitter.emit(kind=EventKind.POLICY_BLOCK, result=Result.BLOCKED)
    emitter.emit(kind=EventKind.APPROVAL, result=Result.PENDING_APPROVAL)
    assert queue.qsize() == 2


def test_sampled_out_still_records_prometheus() -> None:
    from prometheus_client import REGISTRY

    def _value(name: str, **labels: str) -> float:
        return REGISTRY.get_sample_value(name, labels or None) or 0.0

    queue: asyncio.Queue = asyncio.Queue(maxsize=16)
    emitter = QueueEmitter(queue=queue, sample_rate=0.0, rng=_FixedRandom(0.5))
    before_req = _value("nexus_search_requests_total", zone="sampled-zone", status="ok")
    before_out = _value("nexus_activity_sampled_out_total")
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK, subject_zone="sampled-zone")
    assert queue.qsize() == 0
    assert _value("nexus_search_requests_total", zone="sampled-zone", status="ok") == before_req + 1
    assert _value("nexus_activity_sampled_out_total") == before_out + 1
```

(Imports: this test file already imports `asyncio`, `EventKind`, `Result`, and `QueueEmitter` — verify at the top of the file, add any that are missing.)

- [ ] **Step 6.2: Run to verify failure**

Run: `uv run pytest tests/unit/services/activity/test_emitter.py -q`
Expected: new tests FAIL with `TypeError: ... unexpected keyword argument 'sample_rate'`.

- [ ] **Step 6.3: Implement** — in `src/nexus/services/activity/emitter.py`:

Add `import random` to the imports (stdlib group, after `import asyncio`).

Extend `QueueEmitter.__init__` signature and body:

```python
    def __init__(
        self,
        *,
        queue: asyncio.Queue[ActivityEvent],
        loop: asyncio.AbstractEventLoop | None = None,
        sample_rate: float = 1.0,
        sample_rates: dict[str, float] | None = None,
        rng: random.Random | None = None,
    ) -> None:
```

and add after the existing assignments in `__init__`:

```python
        # #4336 sampling knobs. rng is injectable for deterministic tests.
        self._sample_rate = sample_rate
        self._sample_rates = dict(sample_rates or {})
        self._rng = rng or random.Random()
```

At the very top of `emit()` (before the lifecycle/capacity gate `with self._lock:` block), insert:

```python
        # #4336 sampling: durable-row reduction for high-volume kinds.
        # Non-OK results (policy blocks, pending approvals) are audit data
        # and are never sampled out. Prometheus metrics are still recorded
        # so the counters stay exact; only the SQLite row is skipped.
        if result is Result.OK:
            rate = self._sample_rates.get(kind.value, self._sample_rate)
            if rate < 1.0 and self._rng.random() >= rate:
                try:
                    from nexus.services.activity.metrics import (
                        ACTIVITY_SAMPLED_OUT,
                        record_metrics,
                    )

                    record_metrics(
                        kind=kind,
                        result=result,
                        actor_token_hash=actor_token_hash,
                        subject_zone=subject_zone,
                        subject_extra=subject_extra,
                        latency_ms=latency_ms,
                    )
                    ACTIVITY_SAMPLED_OUT.inc()
                except Exception:  # metrics must never break the hot path
                    pass
                return
```

(`Result` is already imported in this module.)

- [ ] **Step 6.4: Run to verify pass**

Run: `uv run pytest tests/unit/services/activity/test_emitter.py tests/unit/services/activity/test_metrics.py -q`
Expected: ALL pass.

- [ ] **Step 6.5: Commit**

```bash
git add src/nexus/services/activity/emitter.py tests/unit/services/activity/test_emitter.py
git commit -m "feat(#4336): per-kind sampling in QueueEmitter, audit results exempt"
```

---

## Task 7: Lifespan wiring + integration tests

**Files:**
- Modify: `src/nexus/services/activity/lifespan.py`
- Modify: `tests/integration/services/activity/test_emit_to_sqlite_e2e.py`
- Verify only: `tests/integration/services/activity/test_lifespan_supervision.py` (env-driven; should pass unchanged)

- [ ] **Step 7.1: Update the e2e tests** — in `tests/integration/services/activity/test_emit_to_sqlite_e2e.py`:

Replace the two db-path setups and assertions. Updated full file:

```python
"""End-to-end: setup_activity → emit → drained to SQLite segment → queryable."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from nexus.services.activity import EventKind, Result, emit
from nexus.services.activity.emitter import NoopEmitter
from nexus.services.activity.lifespan import setup_activity, shutdown_activity


def _segment_rows(seg_dir: Path, query: str) -> list[tuple]:
    segments = sorted(seg_dir.glob("activity-*.db"))
    assert segments, f"no segment files in {seg_dir}"
    rows: list[tuple] = []
    for seg in segments:
        conn = sqlite3.connect(seg)
        rows.extend(conn.execute(query))
        conn.close()
    return rows


@pytest.mark.asyncio
async def test_emit_to_sqlite_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seg_dir = tmp_path / "segments"
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DIR", str(seg_dir))
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "0")  # disable sweep
    monkeypatch.setenv("NEXUS_ACTIVITY_QUEUE_SIZE", "1024")
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_SIZE", "10")
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_TIMEOUT_S", "0.01")

    await setup_activity()
    try:
        for i in range(50):
            emit(
                kind=EventKind.SEARCH,
                result=Result.OK,
                actor_token_hash=f"tok{i % 3}",
                subject_zone=f"zone{i % 2}",
                latency_ms=i,
            )
        await asyncio.sleep(0.5)
    finally:
        await shutdown_activity()

    rows = _segment_rows(seg_dir, "SELECT kind, result, subject_zone FROM activity_events")
    assert len(rows) == 50
    assert all(r[0] == "search" and r[1] == "ok" for r in rows)


@pytest.mark.asyncio
async def test_off_loop_emit_then_shutdown_does_not_lose_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An off-loop emit scheduled immediately before shutdown must either
    persist to SQLite or count as dropped — never silently disappear.
    Regression for the call_soon_threadsafe vs worker.stop race."""
    import threading

    seg_dir = tmp_path / "segments"
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DIR", str(seg_dir))
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "0")

    await setup_activity()
    try:
        from nexus.contracts.protocols.activity import get_emitter

        emitter = get_emitter()
        ready = threading.Event()

        def _emit_off_loop() -> None:
            ready.set()
            emitter.emit(
                kind=EventKind.SEARCH,
                result=Result.OK,
                subject_zone="off-loop",
            )

        t = threading.Thread(target=_emit_off_loop)
        t.start()
        ready.wait()
        t.join()
    finally:
        await shutdown_activity()

    rows = _segment_rows(seg_dir, "SELECT subject_zone FROM activity_events")
    zones = [r[0] for r in rows]
    assert "off-loop" in zones, f"off-loop event missing from durable store; got {zones}"


@pytest.mark.asyncio
async def test_disabled_installs_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "0")
    await setup_activity()
    try:
        from nexus.services.activity import get_emitter

        assert isinstance(get_emitter(), NoopEmitter)
    finally:
        await shutdown_activity()


@pytest.mark.asyncio
async def test_stale_legacy_db_unlinked_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-segment activity.db whose newest row is past retention must be
    deleted by the retention task's first sweep — the #4336 reclaim path."""
    legacy = tmp_path / "activity.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        """CREATE TABLE activity_events (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT, result TEXT,
            latency_ms INTEGER, trace_id TEXT, actor_token_hash TEXT,
            actor_agent TEXT, actor_user TEXT, subject_zone TEXT,
            subject_extra TEXT, meta TEXT
        ) STRICT"""
    )
    conn.execute("INSERT INTO activity_events (id, ts) VALUES ('old', '2020-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DIR", str(tmp_path / "segments"))
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(legacy))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "30")

    await setup_activity()
    try:
        for _ in range(100):  # first sweep runs immediately on start
            if not legacy.exists():
                break
            await asyncio.sleep(0.05)
    finally:
        await shutdown_activity()
    assert not legacy.exists()
```

- [ ] **Step 7.2: Run to verify failure**

Run: `uv run pytest tests/integration/services/activity/test_emit_to_sqlite_e2e.py -q`
Expected: FAIL — lifespan still constructs `SQLiteSink(path=...)` / `RetentionTask(db_path=...)`, raising `TypeError`, which trips the NoopSink fallback (roundtrip asserts then fail on missing segment files).

- [ ] **Step 7.3: Implement** — in `src/nexus/services/activity/lifespan.py`:

Replace the sink construction block inside `setup_activity` (the `try:` around `sinks.append(...)`):

```python
    sinks: list[SinkProtocol] = []
    try:
        sinks.append(
            SQLiteSink(
                segment_dir=cfg.segment_dir,
                min_free_bytes=cfg.min_free_mb * 1024 * 1024,
            )
        )
        logger.info("activity SQLite segment store at %s", cfg.segment_dir)
    except Exception:
        logger.error(
            "activity SQLiteSink failed to open at %s — falling back to NoopSink. "
            "Durable activity_events store is DISABLED for this process.",
            cfg.segment_dir,
            exc_info=True,
        )
```

(keep the existing `ACTIVITY_SINK_ERRORS` fallback block and `sinks.append(NoopSink())` unchanged.)

Replace the retention construction:

```python
    retention = RetentionTask(
        segment_dir=cfg.segment_dir,
        legacy_db_path=cfg.db_path,
        retention_days=cfg.retention_days,
    )
```

Replace the emitter construction:

```python
    queue_emitter = QueueEmitter(
        queue=queue,
        loop=loop,
        sample_rate=cfg.sample_rate,
        sample_rates=cfg.sample_rates,
    )
```

Also update the module docstring's shutdown note if it mentions "VACUUM" (shutdown ordering comment in `shutdown_activity` says "retention's VACUUM cannot hold the SQLite write lock" — reword to "retention's sweep cannot race the final drain").

- [ ] **Step 7.4: Run to verify pass**

Run: `uv run pytest tests/integration/services/activity tests/unit/services/activity -q`
Expected: ALL pass (including `test_lifespan_supervision.py`, which sets `NEXUS_ACTIVITY_DB_PATH=/nonexistent/forbidden/...` — the segment dir derives to `/nonexistent/forbidden/path/activity`, `mkdir` raises `PermissionError`/`OSError`, caught by the `except Exception` fallback).

- [ ] **Step 7.5: Commit**

```bash
git add src/nexus/services/activity/lifespan.py tests/integration/services/activity/test_emit_to_sqlite_e2e.py
git commit -m "feat(#4336): wire segment store, shedding, sampling through lifespan"
```

---

## Task 8: Operator docs

**Files:**
- Create: `docs/operations/activity-store.md`

- [ ] **Step 8.1: Write the doc** — create `docs/operations/activity-store.md`:

```markdown
# Activity store operations

The activity subsystem records telemetry events (search, MCP tool calls,
policy blocks, approvals) to SQLite. Since issue #4336 it writes per-UTC-day
segment files instead of one ever-growing `activity.db`:

```
$NEXUS_ACTIVITY_DIR/activity-2026-06-09.db   (+ -wal / -shm siblings)
```

Retention deletes whole expired segment files — O(1) disk reclaim, no VACUUM.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `NEXUS_ACTIVITY_ENABLED` | `1` | Master switch for the subsystem. |
| `NEXUS_ACTIVITY_DIR` | `<dir of NEXUS_ACTIVITY_DB_PATH>/activity` | Segment directory. Point at a dedicated volume to isolate telemetry I/O and capacity from user payload data. |
| `NEXUS_ACTIVITY_DB_PATH` | `$NEXUS_DATA_DIR/activity.db` | Legacy pre-segment db location; only used to find and clean up that file. Also anchors the `NEXUS_ACTIVITY_DIR` default. |
| `NEXUS_ACTIVITY_RETENTION_DAYS` | `30` | Segments older than this are deleted (whole files). `0` disables retention. Granularity is one day: a segment is removed once its newest possible row passes the cutoff, so worst-case over-retention is <24h. |
| `NEXUS_ACTIVITY_MIN_FREE_MB` | `1024` | Disk-pressure floor: when free space on the segment volume drops below this, the sink **sheds** activity batches (counted, alerted) instead of consuming the space user file writes need. `0` disables shedding. |
| `NEXUS_ACTIVITY_SAMPLE_RATE` | `1.0` | Global keep-probability for `result=ok` events. |
| `NEXUS_ACTIVITY_SAMPLE_RATES` | unset | Per-kind overrides, e.g. `search=0.05,mcp_tool_call=0.2`. Kinds: `search`, `fetch`, `mcp_tool_call`, `zone_access`, `policy_block`, `approval`, `op`, `exec`. |
| `NEXUS_ACTIVITY_QUEUE_SIZE` / `..._BATCH_SIZE` / `..._BATCH_TIMEOUT_S` | `10000` / `200` / `0.5` | In-process queue/batching (unchanged by #4336). |

Sampling never drops non-`ok` results (policy blocks, pending approvals are
audit data), and Prometheus counters are recorded before sampling, so
`/metrics` stays exact regardless of the rate.

## Sizing

Steady-state disk ≈ `retention_days × daily event volume × ~400 B/event`,
plus a ≤64 MB WAL on the active segment (`journal_size_limit`, truncate
checkpoints every 5 min). At 12 M events/day and 30-day retention that is
~150 GB — set `NEXUS_ACTIVITY_SAMPLE_RATES` and/or lower
`NEXUS_ACTIVITY_RETENTION_DAYS` to fit your volume. Shedding is the
backstop, not the sizing mechanism.

## Monitoring

| Metric | Alert on |
|---|---|
| `nexus_activity_shed_total` | any sustained increase — telemetry is being dropped to protect user writes |
| `nexus_activity_disk_free_bytes` | approaching `NEXUS_ACTIVITY_MIN_FREE_MB` |
| `nexus_activity_store_bytes` | unexpected growth vs. your sizing budget |
| `nexus_activity_segments_deleted_total` | flatlining at 0 with non-zero retention (sweep not running) |
| `nexus_activity_sink_errors_total` | write/open failures |

Shedding transitions also emit edge-triggered `ERROR`/`INFO` log lines
(`activity disk pressure ...`).

## Upgrading from the single-file store

The old `activity.db` is frozen on first boot after the upgrade (no new
writes) and deleted automatically once its newest row is older than
`NEXUS_ACTIVITY_RETENTION_DAYS` — full reclaim within one retention window,
with no VACUUM and no DELETE churn. To reclaim immediately instead, delete
`activity.db`, `activity.db-wal`, and `activity.db-shm` yourself; the
server never reopens them.
```

- [ ] **Step 8.2: Commit**

```bash
git add docs/operations/activity-store.md
git commit -m "docs(#4336): activity store operations guide"
```

---

## Task 9: Full verification

**Files:** none (verification only)

- [ ] **Step 9.1: Full activity test sweep**

Run: `uv run pytest tests/unit/services/activity tests/integration/services/activity -q`
Expected: ALL pass.

- [ ] **Step 9.2: Anything else importing the changed APIs?**

Run: `grep -rn "prune_older_than\|total_pruned\|SQLiteSink(path=\|RetentionTask(db_path=" src/ tests/ --include="*.py"`
Expected: NO matches (all call sites migrated). If matches appear, migrate them and re-run step 9.1.

- [ ] **Step 9.3: Lint + typecheck the touched files** (use the repo's configured tools)

Run: `uv run ruff check src/nexus/services/activity tests/unit/services/activity tests/integration/services/activity && uv run ruff format --check src/nexus/services/activity`
Expected: clean. Fix and amend if not.
If the repo's pre-commit config runs mypy on these paths, also run: `uv run mypy src/nexus/services/activity` and fix any errors.

- [ ] **Step 9.4: Broader unit suite for regressions** (activity imports appear in server lifespan)

Run: `uv run pytest tests/unit -q -x --timeout=300`
Expected: pass (or only failures demonstrably pre-existing on the base commit — verify by `git stash && uv run pytest <failing test> && git stash pop`).

- [ ] **Step 9.5: Final commit if any fixups were needed**

```bash
git add -A && git commit -m "fix(#4336): post-verification fixups" || echo "nothing to fix"
```
