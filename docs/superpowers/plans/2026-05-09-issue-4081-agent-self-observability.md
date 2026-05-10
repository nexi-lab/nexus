# Agent Self-Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface every agent op + shell command as JSONL lines at `/.activity/{utc_date}/{agent_id}.jsonl`, ReBAC-isolated per agent, RAM-backed and capped.

**Architecture:** Reuse the existing `services/activity/` emitter→worker→sink pipeline. Add (a) a new `MemoryBackend` data store with ring-buffered per-(agent, date) deques, (b) a new `JsonlActivitySink` that writes lines into the store directly (bypassing dispatch — recursion-safe by construction), (c) a new `bricks/agent_log/` that registers the store as an FS mount at `/.activity/` and owns ReBAC grants, and (d) two new `EventKind` enum values (`OP`, `EXEC`) so existing emit-paths can fan out op + exec records.

**Tech Stack:** Python 3.12, dataclasses, `prometheus_client`, `pytest`, existing Nexus contracts (`nexus.contracts.protocols.activity`).

**Reference spec:** `docs/superpowers/specs/2026-05-09-issue-4081-agent-self-observability-design.md`

---

## File Map

**Create:**
- `src/nexus/services/activity/agent_log_store.py` — `MemoryBackend` ring buffer
- `src/nexus/services/activity/sinks/jsonl.py` — `JsonlActivitySink`
- `src/nexus/bricks/agent_log/__init__.py` — package
- `src/nexus/bricks/agent_log/brick.py` — startup brick (mount + ReBAC)
- `tests/unit/services/activity/test_agent_log_store.py`
- `tests/unit/services/activity/sinks/test_jsonl_sink.py`
- `tests/unit/bricks/agent_log/test_brick_startup.py`
- `tests/integration/agent_log/test_rebac_isolation.py`
- `tests/integration/agent_log/test_recursion_smoke.py`
- `tests/integration/agent_log/test_exec_record.py`
- `docs/agents/self-observability.md`

**Modify:**
- `src/nexus/contracts/protocols/activity.py` — extend `EventKind` with `OP`, `EXEC`
- `src/nexus/services/activity/config.py` — add `agent_log_*` config fields
- `src/nexus/services/activity/lifespan.py` — instantiate store + register `JsonlActivitySink`
- `src/nexus/services/activity/metrics.py` — add agent-log counters
- `src/nexus/services/activity/retention.py` — sweep memory store
- `src/nexus/core/dispatch.py` (or wherever post-hooks fire) — emit `OP` events with meta
- `src/nexus/bricks/sandbox/...` — emit `EXEC` events (call site discovered in Task 8)
- `src/nexus/bricks/identity/...` — onboarding hook to add ReBAC grant for new agents

---

## Task 1: Extend EventKind enum with OP and EXEC

**Files:**
- Modify: `src/nexus/contracts/protocols/activity.py`
- Test: `tests/unit/contracts/protocols/test_activity_eventkind.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/contracts/protocols/test_activity_eventkind.py`:

```python
from nexus.contracts.protocols.activity import EventKind


def test_op_kind_present():
    assert EventKind.OP.value == "op"


def test_exec_kind_present():
    assert EventKind.EXEC.value == "exec"


def test_existing_kinds_unchanged():
    assert EventKind.SEARCH.value == "search"
    assert EventKind.FETCH.value == "fetch"
    assert EventKind.MCP_TOOL_CALL.value == "mcp_tool_call"
```

- [ ] **Step 2: Run test, verify failure**

```bash
pytest tests/unit/contracts/protocols/test_activity_eventkind.py -v
```

Expected: FAIL — `AttributeError: OP`.

- [ ] **Step 3: Add enum members**

In `src/nexus/contracts/protocols/activity.py`, extend `EventKind`:

```python
class EventKind(StrEnum):
    SEARCH = "search"
    FETCH = "fetch"
    MCP_TOOL_CALL = "mcp_tool_call"
    ZONE_ACCESS = "zone_access"
    POLICY_BLOCK = "policy_block"
    APPROVAL = "approval"
    OP = "op"
    EXEC = "exec"
```

- [ ] **Step 4: Run test, verify pass**

```bash
pytest tests/unit/contracts/protocols/test_activity_eventkind.py -v
```

Expected: PASS, 3/3.

- [ ] **Step 5: Run wider activity test suite to confirm no fallout**

```bash
pytest tests/unit/services/activity/ tests/unit/contracts/ -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/contracts/protocols/activity.py tests/unit/contracts/protocols/test_activity_eventkind.py
git commit -m "feat(activity): add OP and EXEC event kinds for agent self-observability"
```

---

## Task 2: MemoryBackend ring-buffered store

**Files:**
- Create: `src/nexus/services/activity/agent_log_store.py`
- Test: `tests/unit/services/activity/test_agent_log_store.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/services/activity/test_agent_log_store.py`:

```python
from nexus.services.activity.agent_log_store import MemoryBackend


def test_append_and_read_one_line():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b'{"ts":"x"}\n')
    out = store.read_path("/.activity/2026-05-09/alice.jsonl")
    assert out == b'{"ts":"x"}\n'


def test_per_agent_isolation():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b'a\n')
    store.append_line("bob",   "2026-05-09", b'b\n')
    assert store.read_path("/.activity/2026-05-09/alice.jsonl") == b'a\n'
    assert store.read_path("/.activity/2026-05-09/bob.jsonl") == b'b\n'


def test_per_date_isolation():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b'd9\n')
    store.append_line("alice", "2026-05-10", b'd10\n')
    assert store.read_path("/.activity/2026-05-09/alice.jsonl") == b'd9\n'
    assert store.read_path("/.activity/2026-05-10/alice.jsonl") == b'd10\n'


def test_ring_buffer_evicts_oldest():
    store = MemoryBackend(cap_bytes=10)
    for i in range(5):
        store.append_line("alice", "2026-05-09", f"line{i}\n".encode())
    out = store.read_path("/.activity/2026-05-09/alice.jsonl")
    # Last lines preserved, earliest evicted, total <= cap
    assert b"line4\n" in out
    assert len(out) <= 10
    # Always at least one line
    assert out


def test_ring_buffer_keeps_at_least_one_line_when_single_line_exceeds_cap():
    store = MemoryBackend(cap_bytes=4)
    store.append_line("alice", "2026-05-09", b"this_line_is_long\n")
    out = store.read_path("/.activity/2026-05-09/alice.jsonl")
    assert out == b"this_line_is_long\n"


def test_read_unknown_path_returns_empty():
    store = MemoryBackend(cap_bytes=1024)
    assert store.read_path("/.activity/2026-05-09/ghost.jsonl") == b""


def test_list_dir_root_returns_dates():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b"a\n")
    store.append_line("alice", "2026-05-10", b"a\n")
    assert sorted(store.list_dir("/.activity/")) == ["2026-05-09", "2026-05-10"]


def test_list_dir_date_returns_agent_files():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b"a\n")
    store.append_line("bob",   "2026-05-09", b"b\n")
    assert sorted(store.list_dir("/.activity/2026-05-09/")) == ["alice.jsonl", "bob.jsonl"]


def test_evicted_count_increments():
    store = MemoryBackend(cap_bytes=8)
    store.append_line("alice", "2026-05-09", b"line1\n")  # 6 bytes
    store.append_line("alice", "2026-05-09", b"line2\n")  # +6 = 12, evict line1
    assert store.lines_evicted == 1


def test_drop_date_removes_buffer():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b"a\n")
    store.drop_date("2026-05-09")
    assert store.read_path("/.activity/2026-05-09/alice.jsonl") == b""
```

- [ ] **Step 2: Run tests, verify failure**

```bash
pytest tests/unit/services/activity/test_agent_log_store.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `MemoryBackend`**

Create `src/nexus/services/activity/agent_log_store.py`:

```python
"""In-memory ring-buffered store for the agent activity log mount.

Backs the /.activity/{date}/{agent_id}.jsonl files. One bounded deque per
(agent_id, date). Sink writes here directly; FS reads route through the
mount registered by bricks/agent_log/.

Concurrency: ActivityWorker is the single writer; reads can race with
writes from request threads. We hold a per-key lock for both append and
read so a snapshot returned to a reader is internally consistent.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

_MOUNT_PREFIX = "/.activity/"


@dataclass(frozen=True, slots=True)
class _Key:
    agent_id: str
    date: str


class MemoryBackend:
    def __init__(self, *, cap_bytes: int) -> None:
        if cap_bytes <= 0:
            raise ValueError(f"cap_bytes must be > 0, got {cap_bytes}")
        self._cap = cap_bytes
        self._buffers: dict[_Key, deque[bytes]] = {}
        self._sizes: dict[_Key, int] = {}
        self._locks: dict[_Key, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self.lines_evicted = 0

    def _lock_for(self, key: _Key) -> threading.Lock:
        # Fast path: already exists.
        lock = self._locks.get(key)
        if lock is not None:
            return lock
        with self._global_lock:
            return self._locks.setdefault(key, threading.Lock())

    def append_line(self, agent_id: str, date: str, line: bytes) -> None:
        key = _Key(agent_id, date)
        with self._lock_for(key):
            buf = self._buffers.setdefault(key, deque())
            buf.append(line)
            self._sizes[key] = self._sizes.get(key, 0) + len(line)
            while self._sizes[key] > self._cap and len(buf) > 1:
                old = buf.popleft()
                self._sizes[key] -= len(old)
                self.lines_evicted += 1

    def read_path(self, path: str) -> bytes:
        parsed = _parse_file_path(path)
        if parsed is None:
            return b""
        key = _Key(parsed[0], parsed[1])
        with self._lock_for(key):
            buf = self._buffers.get(key)
            if buf is None:
                return b""
            return b"".join(buf)

    def list_dir(self, path: str) -> Iterable[str]:
        if path == _MOUNT_PREFIX or path == _MOUNT_PREFIX.rstrip("/"):
            return sorted({k.date for k in self._buffers})
        date = _parse_date_dir(path)
        if date is None:
            return []
        return sorted(f"{k.agent_id}.jsonl" for k in self._buffers if k.date == date)

    def drop_date(self, date: str) -> None:
        with self._global_lock:
            keys = [k for k in self._buffers if k.date == date]
            for k in keys:
                with self._lock_for(k):
                    self._buffers.pop(k, None)
                    self._sizes.pop(k, None)


def _parse_file_path(path: str) -> tuple[str, str] | None:
    if not path.startswith(_MOUNT_PREFIX):
        return None
    rest = path[len(_MOUNT_PREFIX):]
    if "/" not in rest:
        return None
    date, _, fname = rest.partition("/")
    if not fname.endswith(".jsonl"):
        return None
    agent_id = fname[: -len(".jsonl")]
    if not agent_id or not date:
        return None
    return agent_id, date


def _parse_date_dir(path: str) -> str | None:
    if not path.startswith(_MOUNT_PREFIX):
        return None
    rest = path[len(_MOUNT_PREFIX):].rstrip("/")
    if "/" in rest or not rest:
        return None
    return rest
```

- [ ] **Step 4: Run tests, verify pass**

```bash
pytest tests/unit/services/activity/test_agent_log_store.py -v
```

Expected: PASS, 10/10.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/agent_log_store.py tests/unit/services/activity/test_agent_log_store.py
git commit -m "feat(activity): add MemoryBackend ring-buffered agent log store"
```

---

## Task 3: JsonlActivitySink

**Files:**
- Create: `src/nexus/services/activity/sinks/jsonl.py`
- Modify: `src/nexus/services/activity/sinks/__init__.py`
- Test: `tests/unit/services/activity/sinks/test_jsonl_sink.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/services/activity/sinks/test_jsonl_sink.py`:

```python
import json

import pytest

from nexus.contracts.protocols.activity import EventKind, Result
from nexus.services.activity.agent_log_store import MemoryBackend
from nexus.services.activity.events import ActivityEvent, Actor
from nexus.services.activity.sinks.jsonl import JsonlActivitySink


def _evt(*, kind, agent="alice", ts="2026-05-09T12:00:00.000Z", meta=None,
         result=Result.OK, latency_ms=10):
    return ActivityEvent(
        id="e1",
        ts=ts,
        kind=kind,
        result=result,
        latency_ms=latency_ms,
        actor=Actor(agent=agent),
        meta=meta or {},
    )


@pytest.mark.asyncio
async def test_op_event_writes_line():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(kind=EventKind.OP, meta={"op": "read", "path": "/s3/foo.txt", "bytes": 1234})
    await sink.write_batch([evt])
    raw = store.read_path("/.activity/2026-05-09/alice.jsonl")
    assert raw
    rec = json.loads(raw.strip())
    assert rec == {
        "ts": "2026-05-09T12:00:00.000Z",
        "kind": "op",
        "op": "read",
        "path": "/s3/foo.txt",
        "bytes": 1234,
        "ms": 10,
    }


@pytest.mark.asyncio
async def test_exec_event_writes_line():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(kind=EventKind.EXEC, meta={"cmd": "grep x /a", "exit_code": 0})
    await sink.write_batch([evt])
    raw = store.read_path("/.activity/2026-05-09/alice.jsonl")
    rec = json.loads(raw.strip())
    assert rec == {
        "ts": "2026-05-09T12:00:00.000Z",
        "kind": "exec",
        "cmd": "grep x /a",
        "exit_code": 0,
        "ms": 10,
    }


@pytest.mark.asyncio
async def test_recursion_guard_drops_op_under_activity_prefix():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(kind=EventKind.OP, meta={"op": "read", "path": "/.activity/2026-05-09/alice.jsonl", "bytes": 0})
    await sink.write_batch([evt])
    assert store.read_path("/.activity/2026-05-09/alice.jsonl") == b""


@pytest.mark.asyncio
async def test_no_agent_drops_event():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(kind=EventKind.OP, agent=None, meta={"op": "read", "path": "/x"})
    await sink.write_batch([evt])
    # No file written for any agent.
    assert list(store.list_dir("/.activity/")) == []


@pytest.mark.asyncio
async def test_non_op_non_exec_kinds_skipped():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    evt = _evt(kind=EventKind.SEARCH)
    await sink.write_batch([evt])
    assert list(store.list_dir("/.activity/")) == []


@pytest.mark.asyncio
async def test_cmd_truncation_marker():
    store = MemoryBackend(cap_bytes=64 * 1024)
    sink = JsonlActivitySink(store=store, cmd_max_bytes=8)
    evt = _evt(kind=EventKind.EXEC, meta={"cmd": "0123456789ABCDEF", "exit_code": 0})
    await sink.write_batch([evt])
    rec = json.loads(store.read_path("/.activity/2026-05-09/alice.jsonl").strip())
    assert rec["cmd_truncated"] is True
    assert rec["cmd"].endswith("…")
    # Cmd byte length, including the ellipsis suffix, fits in budget.
    assert len(rec["cmd"].encode("utf-8")) <= 8 + len("…".encode("utf-8"))


@pytest.mark.asyncio
async def test_close_is_noop():
    store = MemoryBackend(cap_bytes=1024)
    sink = JsonlActivitySink(store=store)
    await sink.close()  # must not raise
```

- [ ] **Step 2: Run tests, verify failure**

```bash
pytest tests/unit/services/activity/sinks/test_jsonl_sink.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement sink**

Create `src/nexus/services/activity/sinks/jsonl.py`:

```python
"""Sink that writes ActivityEvents into MemoryBackend as JSONL lines.

Only handles EventKind.OP and EventKind.EXEC. Other kinds are silently
skipped — they continue to flow to other sinks (SQLite etc).

Recursion-safety: this sink writes to MemoryBackend.append_line directly,
NOT through OpsRegistry, so its writes never produce ActivityEvents. The
path predicate inside `write_batch` is a second line of defense for the
read path and for any future emitter that does go through dispatch.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from nexus.contracts.protocols.activity import EventKind
from nexus.services.activity.agent_log_store import MemoryBackend
from nexus.services.activity.events import ActivityEvent

logger = logging.getLogger(__name__)

_MOUNT_PREFIX = "/.activity/"
_DEFAULT_CMD_MAX = 4096


class JsonlActivitySink:
    def __init__(self, *, store: MemoryBackend, cmd_max_bytes: int = _DEFAULT_CMD_MAX) -> None:
        self._store = store
        self._cmd_max = cmd_max_bytes
        self.recursion_skipped = 0
        self.no_agent_dropped = 0

    async def write_batch(self, events: Sequence[ActivityEvent]) -> None:
        for e in events:
            if e.kind not in (EventKind.OP, EventKind.EXEC):
                continue
            agent = e.actor.agent if e.actor else None
            if not agent:
                self.no_agent_dropped += 1
                continue
            meta = e.meta or {}
            path = meta.get("path")
            if isinstance(path, str) and path.startswith(_MOUNT_PREFIX):
                self.recursion_skipped += 1
                continue
            line = self._build_line(e, meta)
            date = _utc_date(e.ts)
            try:
                self._store.append_line(agent, date, line)
            except Exception:  # never break the worker
                logger.warning("agent_log append failed", exc_info=True)

    async def close(self) -> None:
        return None

    def _build_line(self, e: ActivityEvent, meta: dict) -> bytes:
        if e.kind == EventKind.OP:
            rec = {
                "ts": e.ts,
                "kind": "op",
                "op": meta.get("op", ""),
                "path": meta.get("path", ""),
                "bytes": int(meta.get("bytes", 0)),
                "ms": int(e.latency_ms or 0),
            }
        else:  # EXEC
            cmd = str(meta.get("cmd", ""))
            cmd_b = cmd.encode("utf-8")
            truncated = False
            if len(cmd_b) > self._cmd_max:
                cmd = cmd_b[: self._cmd_max].decode("utf-8", errors="ignore") + "…"
                truncated = True
            rec = {
                "ts": e.ts,
                "kind": "exec",
                "cmd": cmd,
                "exit_code": int(meta.get("exit_code", 0)),
                "ms": int(e.latency_ms or 0),
            }
            if truncated:
                rec["cmd_truncated"] = True
        return (json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8")


def _utc_date(ts_iso: str) -> str:
    # Expected: "YYYY-MM-DDTHH:MM:SS.sssZ" — caller controls format.
    return ts_iso[:10]
```

Update `src/nexus/services/activity/sinks/__init__.py` to export it:

```python
"""Activity sink implementations: SinkProtocol, NoopSink, RecordingSink, SQLiteSink, JsonlActivitySink."""

from nexus.services.activity.sinks.jsonl import JsonlActivitySink
from nexus.services.activity.sinks.noop import NoopSink
from nexus.services.activity.sinks.protocol import SinkProtocol
from nexus.services.activity.sinks.recording import RecordingSink
from nexus.services.activity.sinks.sqlite import SQLiteSink

__all__ = ["JsonlActivitySink", "NoopSink", "RecordingSink", "SQLiteSink", "SinkProtocol"]
```

- [ ] **Step 4: Run tests, verify pass**

```bash
pytest tests/unit/services/activity/sinks/test_jsonl_sink.py -v
```

Expected: PASS, 7/7.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/sinks/jsonl.py src/nexus/services/activity/sinks/__init__.py tests/unit/services/activity/sinks/test_jsonl_sink.py
git commit -m "feat(activity): add JsonlActivitySink for agent self-observability"
```

---

## Task 4: Activity config — agent_log fields

**Files:**
- Modify: `src/nexus/services/activity/config.py`
- Test: `tests/unit/services/activity/test_config_agent_log.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/services/activity/test_config_agent_log.py`:

```python
import os

import pytest

from nexus.services.activity.config import ActivityConfig


def test_defaults_when_unset(monkeypatch):
    for k in (
        "NEXUS_ACTIVITY_AGENT_LOG_ENABLED",
        "NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES",
        "NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS",
        "NEXUS_ACTIVITY_AGENT_LOG_CMD_MAX_BYTES",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = ActivityConfig.from_env()
    assert cfg.agent_log_enabled is True
    assert cfg.agent_log_cap_bytes == 10 * 1024 * 1024
    assert cfg.agent_log_retention_days == 7
    assert cfg.agent_log_cmd_max_bytes == 4 * 1024


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "0")
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES", "1048576")
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS", "3")
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_CMD_MAX_BYTES", "256")
    cfg = ActivityConfig.from_env()
    assert cfg.agent_log_enabled is False
    assert cfg.agent_log_cap_bytes == 1_048_576
    assert cfg.agent_log_retention_days == 3
    assert cfg.agent_log_cmd_max_bytes == 256


def test_invalid_cap_bytes_rejected(monkeypatch):
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES", "0")
    with pytest.raises(ValueError):
        ActivityConfig.from_env()
```

- [ ] **Step 2: Run, verify failure**

```bash
pytest tests/unit/services/activity/test_config_agent_log.py -v
```

Expected: FAIL — `agent_log_enabled` attribute missing.

- [ ] **Step 3: Extend config**

In `src/nexus/services/activity/config.py`:

Add fields to `ActivityConfig` (after existing fields):

```python
    agent_log_enabled: bool = True
    agent_log_cap_bytes: int = 10 * 1024 * 1024
    agent_log_retention_days: int = 7
    agent_log_cmd_max_bytes: int = 4 * 1024
```

Add validation in `__post_init__` (append at end):

```python
        if self.agent_log_cap_bytes <= 0:
            raise ValueError(
                f"NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES must be > 0, "
                f"got {self.agent_log_cap_bytes}"
            )
        if self.agent_log_retention_days < 0:
            raise ValueError(
                f"NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS must be >= 0, "
                f"got {self.agent_log_retention_days}"
            )
        if self.agent_log_cmd_max_bytes <= 0:
            raise ValueError(
                f"NEXUS_ACTIVITY_AGENT_LOG_CMD_MAX_BYTES must be > 0, "
                f"got {self.agent_log_cmd_max_bytes}"
            )
```

In `from_env`, append the four new keyword args:

```python
            agent_log_enabled=_parse_bool(
                os.environ.get("NEXUS_ACTIVITY_AGENT_LOG_ENABLED"), True
            ),
            agent_log_cap_bytes=_parse_int(
                "NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES",
                os.environ.get("NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES"),
                10 * 1024 * 1024,
            ),
            agent_log_retention_days=_parse_int(
                "NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS",
                os.environ.get("NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS"),
                7,
            ),
            agent_log_cmd_max_bytes=_parse_int(
                "NEXUS_ACTIVITY_AGENT_LOG_CMD_MAX_BYTES",
                os.environ.get("NEXUS_ACTIVITY_AGENT_LOG_CMD_MAX_BYTES"),
                4 * 1024,
            ),
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/unit/services/activity/test_config_agent_log.py -v
```

Expected: PASS, 3/3.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/config.py tests/unit/services/activity/test_config_agent_log.py
git commit -m "feat(activity): add agent_log_* config fields"
```

---

## Task 5: Wire JsonlActivitySink into lifespan

**Files:**
- Modify: `src/nexus/services/activity/lifespan.py`
- Test: `tests/unit/services/activity/test_lifespan_agent_log.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/services/activity/test_lifespan_agent_log.py`:

```python
import pytest

from nexus.services.activity import lifespan
from nexus.services.activity.sinks.jsonl import JsonlActivitySink


@pytest.mark.asyncio
async def test_setup_registers_jsonl_sink(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "1")
    await lifespan.setup_activity()
    try:
        worker = lifespan._STATE["worker"]
        assert any(isinstance(s, JsonlActivitySink) for s in worker._sinks)
    finally:
        await lifespan.shutdown_activity()


@pytest.mark.asyncio
async def test_setup_skips_jsonl_sink_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "0")
    await lifespan.setup_activity()
    try:
        worker = lifespan._STATE["worker"]
        assert not any(isinstance(s, JsonlActivitySink) for s in worker._sinks)
    finally:
        await lifespan.shutdown_activity()


@pytest.mark.asyncio
async def test_setup_exposes_store(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "1")
    await lifespan.setup_activity()
    try:
        assert lifespan.get_agent_log_store() is not None
    finally:
        await lifespan.shutdown_activity()
```

- [ ] **Step 2: Run, verify failure**

```bash
pytest tests/unit/services/activity/test_lifespan_agent_log.py -v
```

Expected: FAIL.

- [ ] **Step 3: Update lifespan**

In `src/nexus/services/activity/lifespan.py`:

Add imports:

```python
from nexus.services.activity.agent_log_store import MemoryBackend
from nexus.services.activity.sinks.jsonl import JsonlActivitySink
```

After the existing `sinks.append(SQLiteSink(...))` block in `setup_activity`, before `worker = ActivityWorker(...)`, add:

```python
    if cfg.agent_log_enabled:
        store = MemoryBackend(cap_bytes=cfg.agent_log_cap_bytes)
        sinks.append(JsonlActivitySink(store=store, cmd_max_bytes=cfg.agent_log_cmd_max_bytes))
        _STATE["agent_log_store"] = store
        logger.info(
            "activity agent_log enabled (cap=%d bytes/agent/day)", cfg.agent_log_cap_bytes
        )
    else:
        _STATE["agent_log_store"] = None
```

Add accessor at module level:

```python
def get_agent_log_store() -> "MemoryBackend | None":
    return _STATE.get("agent_log_store")  # type: ignore[return-value]
```

In `shutdown_activity`, clear the entry:

```python
    _STATE["agent_log_store"] = None
```

(Locate `shutdown_activity` in the same file; if not present, the existing `_STATE`-clearing logic applies.)

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/unit/services/activity/test_lifespan_agent_log.py -v
```

Expected: PASS, 3/3.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/lifespan.py tests/unit/services/activity/test_lifespan_agent_log.py
git commit -m "feat(activity): register JsonlActivitySink in lifespan"
```

---

## Task 6: Activity metrics — agent_log counters

**Files:**
- Modify: `src/nexus/services/activity/metrics.py`
- Test: `tests/unit/services/activity/test_metrics_agent_log.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/services/activity/test_metrics_agent_log.py`:

```python
from nexus.services.activity import metrics


def test_agent_log_counters_present():
    assert metrics.AGENT_LOG_LINES_DROPPED is not None
    # Underlying type exposes ._labelnames; touch the labels we need.
    assert "reason" in metrics.AGENT_LOG_LINES_DROPPED._labelnames


def test_agent_log_bytes_gauge_present():
    assert metrics.AGENT_LOG_BYTES is not None
    assert "agent_id" in metrics.AGENT_LOG_BYTES._labelnames
```

- [ ] **Step 2: Run, verify failure**

```bash
pytest tests/unit/services/activity/test_metrics_agent_log.py -v
```

Expected: FAIL — attributes missing.

- [ ] **Step 3: Add counters**

In `src/nexus/services/activity/metrics.py` (append before any final block):

```python
AGENT_LOG_LINES_DROPPED = Counter(
    "nexus_activity_agent_log_lines_dropped_total",
    "Lines not written to agent_log mount, by reason",
    ["reason"],  # ring_evict | recursion | no_agent
)

AGENT_LOG_BYTES = Gauge(
    "nexus_activity_agent_log_bytes",
    "Current bytes held in the agent_log MemoryBackend",
    ["agent_id"],
)
```

- [ ] **Step 4: Wire counters into JsonlActivitySink**

In `src/nexus/services/activity/sinks/jsonl.py`:

Add import and increment at drop sites:

```python
from nexus.services.activity.metrics import AGENT_LOG_LINES_DROPPED
```

In `write_batch`, replace bare counter increments:

```python
        if not agent:
            self.no_agent_dropped += 1
            AGENT_LOG_LINES_DROPPED.labels(reason="no_agent").inc()
            continue
        ...
        if isinstance(path, str) and path.startswith(_MOUNT_PREFIX):
            self.recursion_skipped += 1
            AGENT_LOG_LINES_DROPPED.labels(reason="recursion").inc()
            continue
```

In `MemoryBackend.append_line`, after each eviction:

```python
                self.lines_evicted += 1
                AGENT_LOG_LINES_DROPPED.labels(reason="ring_evict").inc()
```

(Add the import at top of `agent_log_store.py`:
`from nexus.services.activity.metrics import AGENT_LOG_LINES_DROPPED`)

- [ ] **Step 5: Run unit tests across the touched modules**

```bash
pytest tests/unit/services/activity/test_metrics_agent_log.py \
       tests/unit/services/activity/sinks/test_jsonl_sink.py \
       tests/unit/services/activity/test_agent_log_store.py -v
```

Expected: PASS for all.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/services/activity/metrics.py src/nexus/services/activity/sinks/jsonl.py src/nexus/services/activity/agent_log_store.py tests/unit/services/activity/test_metrics_agent_log.py
git commit -m "feat(activity): expose agent_log drop counters via Prometheus"
```

---

## Task 7: Emit OP events from the dispatch post-hook

**Discovery sub-task:** before writing code, identify the post-hook in `src/nexus/core/dispatch.py` (and `src/nexus/core/nexus_fs_internal.py` per Explore findings) where each op completes and an `ActivityEvent` would be the right place to emit. Search:

```bash
grep -n "emit\|EventKind\|ZONE_ACCESS\|FETCH" src/nexus/core/dispatch.py src/nexus/core/nexus_fs_internal.py src/nexus/core/nexus_fs_content.py
```

Document the chosen call site in the commit message.

**Files:**
- Modify: dispatch post-hook (file located in discovery; expected `src/nexus/core/nexus_fs_content.py:156` per Explore findings, or `dispatch.py`)
- Test: `tests/unit/core/test_dispatch_emits_op_event.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/core/test_dispatch_emits_op_event.py`:

```python
"""Verify dispatch emits a kind=OP ActivityEvent with path/op/bytes meta."""
from unittest.mock import patch

import pytest

from nexus.contracts.protocols.activity import EventKind


@pytest.mark.asyncio
async def test_cat_path_emits_op_read_event(monkeypatch):
    captured = []

    def fake_emit(**kw):
        captured.append(kw)

    # Patch the contract-level emit. Once the post-hook is in place this
    # captures every dispatch.
    with patch("nexus.contracts.protocols.activity._EMITTER") as em:
        em.emit.side_effect = fake_emit
        # Drive the smallest possible dispatch path — depends on test
        # harness for nexus_fs. If your harness exposes a synthetic backend,
        # use it; otherwise call the post-hook helper directly:
        from nexus.core import nexus_fs_content as fs

        # Use whichever helper the discovery step revealed for synthesizing
        # an op-completion event. Replace `_emit_op_complete` with the actual
        # symbol; if the post-hook is inline, refactor it into a helper
        # named `emit_op_completed` and call that.
        fs.emit_op_completed(
            agent_id="alice",
            op="read",
            path="/s3/bucket/foo.txt",
            bytes_count=1234,
            latency_ms=42,
            ts="2026-05-09T12:00:00.000Z",
        )

    assert any(
        c.get("kind") == EventKind.OP
        and c.get("actor_agent") == "alice"
        and (c.get("meta") or {}).get("path") == "/s3/bucket/foo.txt"
        and (c.get("meta") or {}).get("op") == "read"
        and (c.get("meta") or {}).get("bytes") == 1234
        for c in captured
    )
```

- [ ] **Step 2: Run, verify failure**

```bash
pytest tests/unit/core/test_dispatch_emits_op_event.py -v
```

Expected: FAIL — helper does not exist or post-hook does not emit kind=OP.

- [ ] **Step 3: Implement the post-hook helper and call it**

In the file located by discovery (assume `src/nexus/core/nexus_fs_content.py`):

```python
def emit_op_completed(
    *,
    agent_id: str | None,
    op: str,
    path: str,
    bytes_count: int,
    latency_ms: int,
    ts: str,
    trace_id: str | None = None,
) -> None:
    """Emit an OP ActivityEvent for self-observability.

    Called from the dispatch post-hook on success and error paths. Path
    predicates and agent-id checks live in JsonlActivitySink, not here —
    keep this helper unconditional so other sinks (SQLite, etc.) get
    every record.
    """
    from nexus.contracts.protocols.activity import EventKind, Result, emit

    emit(
        kind=EventKind.OP,
        result=Result.OK,
        actor_agent=agent_id,
        latency_ms=latency_ms,
        trace_id=trace_id,
        meta={"op": op, "path": path, "bytes": bytes_count},
    )
```

In the existing post-hook site (where `ReadHookContext` finishes — the one referenced in the spec at `nexus_fs_content.py:156`), call `emit_op_completed(...)`. Pass `agent_id` from `_get_context_identity()`. For non-read ops (write, list, delete), call the same helper with the appropriate `op` string.

Note: there will be at least one call site per supported op verb. List the verbs to wire from the existing dispatch (`read`, `write`, `list`, `delete`, etc.) and add a call for each one. Keep the additions minimal — emit a single OP event at op completion, success or error.

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/unit/core/test_dispatch_emits_op_event.py -v
```

Expected: PASS.

- [ ] **Step 5: Run the full activity test suite**

```bash
pytest tests/unit/services/activity/ tests/unit/core/test_dispatch_emits_op_event.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/core/nexus_fs_content.py tests/unit/core/test_dispatch_emits_op_event.py
git commit -m "feat(dispatch): emit OP events from op-completion post-hook"
```

(If discovery puts the helper elsewhere, update file paths in the commit accordingly.)

---

## Task 8: Emit EXEC events from sandbox

**Discovery sub-task:** locate the sandbox shell-execution call site:

```bash
grep -rn "subprocess\|asyncio.create_subprocess\|shell\|exec_shell\|exec_command" src/nexus/bricks/sandbox/ | head -30
```

Find the function that runs a shell command and returns `(stdout, exit_code, ms)`. That is the wrap point.

**Files:**
- Modify: sandbox exec entry (located in discovery)
- Test: `tests/integration/agent_log/test_exec_record.py`

- [ ] **Step 1: Write failing integration test**

Create `tests/integration/agent_log/test_exec_record.py`:

```python
"""End-to-end: a sandbox exec call produces an EXEC ActivityEvent."""
import json
import pytest

from nexus.services.activity import lifespan
from nexus.services.activity.sinks.jsonl import JsonlActivitySink


@pytest.mark.asyncio
async def test_sandbox_exec_emits_exec_record(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "1")
    await lifespan.setup_activity()
    try:
        # Drive a sandbox exec as agent "alice".
        # Replace this with the actual sandbox API surface located in
        # discovery — e.g. SandboxRunner.run_shell(cmd, agent_id=...).
        from nexus.bricks.sandbox import run_shell  # placeholder symbol

        await run_shell("echo hi", agent_id="alice")

        # Drain the worker so the sink gets the batch.
        worker = lifespan._STATE["worker"]
        await worker.flush()

        store = lifespan.get_agent_log_store()
        # Date is UTC — pick today.
        from datetime import datetime, timezone
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw = store.read_path(f"/.activity/{date}/alice.jsonl")
        assert raw, "no exec record written"
        recs = [json.loads(l) for l in raw.strip().split(b"\n")]
        exec_recs = [r for r in recs if r["kind"] == "exec"]
        assert len(exec_recs) >= 1
        r = exec_recs[-1]
        assert r["cmd"] == "echo hi"
        assert r["exit_code"] == 0
        assert isinstance(r["ms"], int) and r["ms"] >= 0
    finally:
        await lifespan.shutdown_activity()
```

If `worker.flush()` does not exist, add a small helper that drains the queue + sleeps until the sinks are quiescent.

- [ ] **Step 2: Run, verify failure**

```bash
pytest tests/integration/agent_log/test_exec_record.py -v
```

Expected: FAIL — sandbox exec is not wired to emit.

- [ ] **Step 3: Wire sandbox exec to emit**

At the sandbox exec call site, after the command completes, add:

```python
from nexus.contracts.protocols.activity import EventKind, Result, emit

emit(
    kind=EventKind.EXEC,
    result=Result.OK if exit_code == 0 else Result.BLOCKED,
    actor_agent=agent_id,
    latency_ms=ms,
    meta={"cmd": cmd, "exit_code": exit_code},
)
```

Use the same identity resolution the rest of the sandbox uses — pull `agent_id` from `OperationContext` if available (see `nexus.core.nexus_fs_internal._get_context_identity`).

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/integration/agent_log/test_exec_record.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/sandbox/<file_located_in_discovery>.py tests/integration/agent_log/test_exec_record.py
git commit -m "feat(sandbox): emit EXEC events for agent shell commands"
```

---

## Task 9: bricks/agent_log/ — mount registration + ReBAC grants

**Discovery sub-task:** find the brick startup hook idiom and the existing mount registration pattern. Read:

```bash
ls src/nexus/bricks/mount/
grep -n "add_mount_sync\|register_mount\|register_brick" src/nexus/bricks/mount/mount_service.py | head
ls src/nexus/bricks/identity/
grep -rn "agent_created\|on_agent_create\|register_agent" src/nexus/bricks/identity/ | head
```

Identify:
- How a brick adds a mount that points at an in-memory backend (vs. a connector). If the existing `add_mount_sync` only supports connector-typed backends, you may need a thin adapter that exposes `MemoryBackend` through the connector interface — keep this minimal.
- The agent-onboarding hook (search for `register_agent`, `on_agent_create`, or similar — it may be inside `key_service.py`).

**Files:**
- Create: `src/nexus/bricks/agent_log/__init__.py`
- Create: `src/nexus/bricks/agent_log/brick.py`
- Test: `tests/unit/bricks/agent_log/test_brick_startup.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/bricks/agent_log/test_brick_startup.py`:

```python
import pytest

from nexus.bricks.agent_log.brick import AgentLogBrick


@pytest.mark.asyncio
async def test_brick_registers_mount_at_dot_activity(monkeypatch, tmp_path):
    fake_mount_calls = []

    async def fake_add_mount(*, path, backend):
        fake_mount_calls.append((path, backend))

    brick = AgentLogBrick(add_mount=fake_add_mount, add_rebac_grant=lambda **_: None)
    await brick.startup(agent_ids=["alice", "bob"])

    assert len(fake_mount_calls) == 1
    assert fake_mount_calls[0][0] == "/.activity/"


@pytest.mark.asyncio
async def test_brick_grants_each_agent_read_on_their_own_log():
    grants = []

    def fake_grant(*, subject, relation, object):  # noqa: A002
        grants.append((subject, relation, object))

    brick = AgentLogBrick(
        add_mount=_noop_mount, add_rebac_grant=fake_grant,
    )
    await brick.startup(agent_ids=["alice", "bob"])

    assert ("agent:alice", "can-read", "path:/.activity/*/alice.jsonl") in grants
    assert ("agent:bob", "can-read", "path:/.activity/*/bob.jsonl") in grants


@pytest.mark.asyncio
async def test_brick_on_agent_created_adds_grant():
    grants = []

    def fake_grant(*, subject, relation, object):  # noqa: A002
        grants.append((subject, relation, object))

    brick = AgentLogBrick(add_mount=_noop_mount, add_rebac_grant=fake_grant)
    await brick.startup(agent_ids=[])
    brick.on_agent_created("carol")
    assert ("agent:carol", "can-read", "path:/.activity/*/carol.jsonl") in grants


async def _noop_mount(*, path, backend):  # noqa: ARG001
    return None
```

- [ ] **Step 2: Run, verify failure**

```bash
pytest tests/unit/bricks/agent_log/test_brick_startup.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement brick**

Create `src/nexus/bricks/agent_log/__init__.py`:

```python
"""Agent self-observability brick — mounts /.activity/ and owns ReBAC grants."""

from nexus.bricks.agent_log.brick import AgentLogBrick

__all__ = ["AgentLogBrick"]
```

Create `src/nexus/bricks/agent_log/brick.py`:

```python
"""Mounts /.activity/ at startup and ensures each agent has read access to
its own JSONL log.

Constructor takes function references rather than importing mount/ReBAC
services directly — keeps the brick testable and avoids cross-brick imports.
The owning lifespan wires the real implementations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol

from nexus.services.activity.agent_log_store import MemoryBackend


class _AddMount(Protocol):
    async def __call__(self, *, path: str, backend: MemoryBackend) -> None: ...


class _AddReBACGrant(Protocol):
    def __call__(self, *, subject: str, relation: str, object: str) -> None: ...


_MOUNT_PATH = "/.activity/"


def _grant_for(agent_id: str) -> tuple[str, str, str]:
    return (f"agent:{agent_id}", "can-read", f"path:/.activity/*/{agent_id}.jsonl")


class AgentLogBrick:
    def __init__(
        self,
        *,
        add_mount: _AddMount,
        add_rebac_grant: _AddReBACGrant,
        store: MemoryBackend | None = None,
    ) -> None:
        self._add_mount = add_mount
        self._add_grant = add_rebac_grant
        self._store = store

    async def startup(self, *, agent_ids: Iterable[str]) -> None:
        # The store is owned by the activity service. The brick may be
        # constructed with `store=None` if the activity service is disabled,
        # in which case mount registration is skipped.
        if self._store is not None:
            await self._add_mount(path=_MOUNT_PATH, backend=self._store)
        for agent_id in agent_ids:
            self._grant(agent_id)

    def on_agent_created(self, agent_id: str) -> None:
        self._grant(agent_id)

    def _grant(self, agent_id: str) -> None:
        subject, relation, obj = _grant_for(agent_id)
        self._add_grant(subject=subject, relation=relation, object=obj)
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/unit/bricks/agent_log/test_brick_startup.py -v
```

Expected: PASS, 3/3.

- [ ] **Step 5: Wire into runtime startup**

This is the integration step that depends on discovery. In whichever module composes bricks at process startup (look for `bricks/agent_log/brick_factory.py` style following `bricks/identity/brick_factory.py:1`), add:

```python
from nexus.bricks.agent_log import AgentLogBrick
from nexus.bricks.identity import register_agent_create_listener  # or equivalent
from nexus.bricks.mount.mount_service import add_mount_sync as _add_mount_sync
from nexus.bricks.rebac.namespace_manager import add_grant as _add_grant  # or equivalent
from nexus.services.activity.lifespan import get_agent_log_store


async def build_agent_log_brick() -> AgentLogBrick:
    async def add_mount(*, path, backend):
        # Wrap MemoryBackend in whatever connector adapter the mount service
        # expects. If add_mount_sync supports an in-memory backend kind
        # directly, pass it through.
        await _add_mount_sync(path=path, backend=backend)

    def add_grant(*, subject, relation, object):  # noqa: A002
        _add_grant(subject=subject, relation=relation, object=object)

    brick = AgentLogBrick(
        add_mount=add_mount,
        add_rebac_grant=add_grant,
        store=get_agent_log_store(),
    )
    register_agent_create_listener(brick.on_agent_created)
    return brick
```

The exact symbol names will be determined by discovery; substitute the closest match. If `add_mount_sync` does not accept an in-memory backend, write a small `MemoryBackendConnector` shim that exposes the expected backend interface and delegates `read_path`/`list_dir` to the store. Keep the shim < 60 lines.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/agent_log/ tests/unit/bricks/agent_log/test_brick_startup.py
git commit -m "feat(agent_log): brick that mounts /.activity/ and owns ReBAC grants"
```

If a connector shim was needed:

```bash
git add src/nexus/bricks/agent_log/connector.py
git commit -m "feat(agent_log): MemoryBackend connector shim for mount integration"
```

---

## Task 10: Retention sweep for memory store

**Files:**
- Modify: `src/nexus/services/activity/retention.py`
- Test: `tests/unit/services/activity/test_retention_agent_log.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/services/activity/test_retention_agent_log.py`:

```python
from datetime import datetime, timedelta, timezone

from nexus.services.activity.agent_log_store import MemoryBackend
from nexus.services.activity.retention import sweep_agent_log


def test_sweep_drops_dates_older_than_retention():
    store = MemoryBackend(cap_bytes=1024)
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=10)).isoformat()
    young = (today - timedelta(days=1)).isoformat()

    store.append_line("alice", old, b"old\n")
    store.append_line("alice", young, b"young\n")

    sweep_agent_log(store, retention_days=7, now=datetime.now(timezone.utc))

    assert store.read_path(f"/.activity/{old}/alice.jsonl") == b""
    assert store.read_path(f"/.activity/{young}/alice.jsonl") == b"young\n"
```

- [ ] **Step 2: Run, verify failure**

```bash
pytest tests/unit/services/activity/test_retention_agent_log.py -v
```

Expected: FAIL.

- [ ] **Step 3: Add the sweep helper**

In `src/nexus/services/activity/retention.py`, add a top-level helper:

```python
from datetime import datetime, timedelta, timezone

from nexus.services.activity.agent_log_store import MemoryBackend


def sweep_agent_log(
    store: MemoryBackend, *, retention_days: int, now: datetime | None = None
) -> int:
    """Drop (agent, date) buffers older than `retention_days`.

    Returns count of date keys dropped. Idempotent.
    """
    n = now or datetime.now(timezone.utc)
    cutoff = (n.date() - timedelta(days=retention_days)).isoformat()
    dates = list({k.date for k in store._buffers})  # noqa: SLF001
    dropped = 0
    for date in dates:
        if date < cutoff:
            store.drop_date(date)
            dropped += 1
    return dropped
```

(Touching `store._buffers` is a deliberate exception within the same package — `MemoryBackend` is a sibling module under `services/activity/`. If this feels too invasive, add `MemoryBackend.iter_dates()` returning `set[str]`.)

In the existing `RetentionTask` class within the same file, hook the sweep into the periodic loop:

```python
from nexus.services.activity.lifespan import get_agent_log_store

# inside the periodic tick:
store = get_agent_log_store()
if store is not None:
    sweep_agent_log(store, retention_days=self._agent_log_retention_days)
```

Pass `agent_log_retention_days` into `RetentionTask.__init__` from `setup_activity` (Task 5 file).

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/unit/services/activity/test_retention_agent_log.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/retention.py tests/unit/services/activity/test_retention_agent_log.py
git commit -m "feat(activity): retention sweep for agent_log MemoryBackend"
```

---

## Task 11: Integration — ReBAC isolation (agent A cannot read B's log)

**Files:**
- Test: `tests/integration/agent_log/test_rebac_isolation.py`

- [ ] **Step 1: Write the test**

Create `tests/integration/agent_log/test_rebac_isolation.py`:

```python
"""End-to-end: agent A reads its own /.activity/ file; cannot read B's."""
import json
import pytest
from datetime import datetime, timezone

from nexus.services.activity import lifespan


@pytest.mark.asyncio
async def test_isolation(monkeypatch, tmp_path, full_test_runtime):
    """`full_test_runtime` is the project-wide fixture that starts mounts +
    rebac + activity in-process. Replace with the actual fixture name once
    located (search tests/conftest.py for the rig that spins up bricks)."""
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "1")
    rt = full_test_runtime  # fixture provides ctx, cat_path, write_path, etc.

    # Drive an op as alice — produces a line in alice's JSONL.
    rt.cat_path("/local/some.txt", as_agent="alice")
    await rt.flush_activity()

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Alice reads her own file: ALLOW.
    raw = rt.cat_path(f"/.activity/{date}/alice.jsonl", as_agent="alice")
    assert raw and len([json.loads(l) for l in raw.strip().split(b"\n")]) >= 1

    # Bob reads alice's file: DENY.
    with pytest.raises(rt.PermissionDenied):
        rt.cat_path(f"/.activity/{date}/alice.jsonl", as_agent="bob")

    # Alice tries to write to her own file: DENY (read-only mount).
    with pytest.raises(rt.PermissionDenied):
        rt.write_path(f"/.activity/{date}/alice.jsonl", b"x", as_agent="alice")
```

If the test runtime fixture doesn't exist yet under the name above, search:

```bash
grep -rn "@pytest.fixture" tests/conftest.py tests/integration/conftest.py | head -20
```

and use the closest one. If none exist, write the smallest in-process fixture you need: instantiate the brick, register grants for `alice` and `bob`, and expose the FS verbs.

- [ ] **Step 2: Run, verify failure if any wiring missing**

```bash
pytest tests/integration/agent_log/test_rebac_isolation.py -v
```

Expected: FAIL until brick wiring (Task 9) is fully integrated.

- [ ] **Step 3: Iterate until pass**

Common issues:
- Mount not registered → check Task 9 wiring step.
- ReBAC grant missing → confirm `agent_ids` passed to `brick.startup` includes `alice` and `bob`.
- Path glob `/.activity/*/{agent}.jsonl` not matching → verify the wildcard syntax against the existing rebac matcher (peek at `bricks/rebac/namespace_manager.py:64`).

- [ ] **Step 4: Commit**

```bash
git add tests/integration/agent_log/test_rebac_isolation.py
git commit -m "test(agent_log): integration test for ReBAC isolation"
```

---

## Task 12: Integration — recursion smoke test

**Files:**
- Test: `tests/integration/agent_log/test_recursion_smoke.py`

- [ ] **Step 1: Write the test**

```python
"""Drive 1k mixed ops including writes attempted at /.activity/. Assert no
infinite loop, byte total stays under cap."""
import pytest
from datetime import datetime, timezone

from nexus.services.activity import lifespan


@pytest.mark.asyncio
async def test_no_recursion_under_load(monkeypatch, tmp_path, full_test_runtime):
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES", "65536")
    rt = full_test_runtime

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for i in range(1000):
        rt.cat_path(f"/local/file{i}.txt", as_agent="alice")
        # Half of these target the activity prefix — should be ReBAC-denied
        # without producing more records.
        if i % 2 == 0:
            try:
                rt.write_path(f"/.activity/{date}/alice.jsonl", b"x", as_agent="alice")
            except rt.PermissionDenied:
                pass

    await rt.flush_activity()

    store = lifespan.get_agent_log_store()
    raw = store.read_path(f"/.activity/{date}/alice.jsonl")
    # No infinite growth: each op is at most ~200 bytes; 1000 ops < 200KB
    # but cap is 64KB — ring eviction kicks in. Assert <= cap + 1 line slack.
    assert len(raw) <= 65536 + 1024
```

- [ ] **Step 2: Run, verify pass (or iterate)**

```bash
pytest tests/integration/agent_log/test_recursion_smoke.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/agent_log/test_recursion_smoke.py
git commit -m "test(agent_log): integration smoke test for recursion safety"
```

---

## Task 13: Documentation page

**Files:**
- Create: `docs/agents/self-observability.md`
- Modify: `mkdocs.yml` (add nav entry)

- [ ] **Step 1: Write doc**

Create `docs/agents/self-observability.md`:

```markdown
# Agent Self-Observability

Every Nexus agent has access to a JSONL log of its own activity at:

    /.activity/{utc_date}/{agent_id}.jsonl

Use the same `cat`, `grep`, and `jq` you already use to inspect any other
mount.

## Schema

```json
{"ts":"2026-05-09T23:42:11.043Z","kind":"op","op":"read","path":"/s3/bucket/foo.txt","bytes":12834,"ms":43}
{"ts":"2026-05-09T23:42:12.110Z","kind":"exec","cmd":"grep needle /gh/owner/repo/README.md","exit_code":0,"ms":215}
{"ts":"2026-05-09T23:42:14.221Z","kind":"op","op":"write","path":"/local/notes.md","bytes":412,"ms":8}
```

`ts` is ISO-8601 UTC. `cmd` is truncated to 4 KB; truncated records carry
`"cmd_truncated": true`.

## Examples

What did I read in the last hour?

    grep '"kind":"op"' /.activity/2026-05-09/me.jsonl | grep '"op":"read"' | tail

How much time did I spend on Slack today?

    jq 'select(.path | startswith("/slack/")) | .ms' /.activity/2026-05-09/me.jsonl \
      | awk '{s+=$1} END {print s}'

What was my last failed command?

    grep '"kind":"exec"' /.activity/2026-05-09/me.jsonl \
      | jq 'select(.exit_code != 0)' | tail -1

Replace `me.jsonl` with your own `agent_id`.

## Isolation

- Each agent can read only its own log file. ReBAC denies cross-agent reads.
- The mount is read-only for agents.
- Operators with `is_admin` can read any agent's log.

## Storage and retention

- Backed by RAM. Default cap **10 MB per agent per day**, configurable via
  `NEXUS_ACTIVITY_AGENT_LOG_CAP_BYTES`.
- When the cap is hit, oldest lines are evicted (ring buffer); the most
  recent activity is always available.
- Retention defaults to **7 days** in RAM, configurable via
  `NEXUS_ACTIVITY_AGENT_LOG_RETENTION_DAYS`. No disk archive in v1.

## Metrics

Operators see drop rates at:

- `nexus_activity_agent_log_lines_dropped_total{reason}` — `ring_evict`,
  `recursion`, `no_agent`.
- `nexus_activity_agent_log_bytes{agent_id}` — current per-agent buffer size.

## Limitations (v1)

- No streaming reads (`tail -f`) — re-read for new lines.
- System ops with no agent actor are not recorded.
- No on-disk archive.
```

- [ ] **Step 2: Add nav entry**

In `mkdocs.yml` under the appropriate `nav:` section (search for `Agents:` or `Architecture:`), add:

```yaml
      - Self-Observability: agents/self-observability.md
```

- [ ] **Step 3: Build docs locally to confirm rendering**

```bash
mkdocs build --strict
```

Expected: clean build.

- [ ] **Step 4: Commit**

```bash
git add docs/agents/self-observability.md mkdocs.yml
git commit -m "docs: agent self-observability page"
```

---

## Task 14: End-to-end validation pass

- [ ] **Step 1: Run the whole test surface for the feature**

```bash
pytest tests/unit/services/activity/ \
       tests/unit/bricks/agent_log/ \
       tests/unit/contracts/protocols/test_activity_eventkind.py \
       tests/unit/core/test_dispatch_emits_op_event.py \
       tests/integration/agent_log/ -v
```

Expected: all PASS.

- [ ] **Step 2: Run lint + type-check**

```bash
ruff check src/nexus/services/activity/ src/nexus/bricks/agent_log/
mypy src/nexus/services/activity/ src/nexus/bricks/agent_log/
```

Expected: clean.

- [ ] **Step 3: Run the broader activity-adjacent test suite for regression check**

```bash
pytest tests/unit/services/ tests/unit/contracts/ tests/integration/agent_log/ -q
```

Expected: PASS.

- [ ] **Step 4: Smoke check — start nexus, drive a few ops, cat the log**

```bash
NEXUS_ACTIVITY_AGENT_LOG_ENABLED=1 python -m nexus.server &
# Authenticate as a test agent.
nexus exec "cat /local/README.md"
nexus exec "cat /.activity/$(date -u +%Y-%m-%d)/<agent_id>.jsonl"
```

Expect to see at least one `{"kind":"op","op":"read",...}` line referencing `/local/README.md` and one referencing the post-hook self-observation of the second `cat`.

- [ ] **Step 5: Confirm acceptance criteria**

Walk through `docs/superpowers/specs/2026-05-09-issue-4081-agent-self-observability-design.md` "Acceptance Criteria":

- [ ] Every agent-actor op + exec recorded — Tasks 7, 8, 12.
- [ ] Agent A cannot read Agent B's log — Task 11.
- [ ] Recursion broken — Task 12.
- [ ] Capped storage; rotation policy configurable — Tasks 2, 4, 10.
- [ ] Doc page with example queries — Task 13.

- [ ] **Step 6: Final commit if anything trailed**

```bash
git status
# If docs/tests need touch-ups:
git add -A
git commit -m "chore(agent_log): final integration touch-ups"
```

---

## Notes for the Engineer

- Several tasks have a "discovery sub-task" because the exact call site (post-hook for ops, sandbox exec entry, brick composition root) varies by branch. Spend 10–15 minutes locating the right hook before writing code; record what you found in the commit message.
- The brick layer in this codebase keeps cross-brick coupling low. `AgentLogBrick` takes function refs in its constructor on purpose — do not import `mount_service` or `rebac.namespace_manager` from inside `bricks/agent_log/`.
- If `MountService.add_mount_sync` cannot accept an in-memory backend, write a thin connector shim under `bricks/agent_log/connector.py` and delegate `read_path` and `list_dir` to the store. Keep it under ~60 lines.
- The activity worker is the single writer to `MemoryBackend`. Reads from request threads use the per-key lock — do not refactor that without re-reading the recursion proof in the spec.
- `ts[:10]` is a deliberate UTC-date extraction. The emitter formats `ts` as ISO-8601 with `Z`. Do not change format without updating `_utc_date`.
