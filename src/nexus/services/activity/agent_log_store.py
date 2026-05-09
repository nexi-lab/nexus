"""In-memory ring-buffered store for the agent activity log mount.

Backs the /.activity/{date}/{agent_id}.jsonl files. One bounded deque per
(agent_id, date). Sink writes here directly; FS reads route through the
mount registered by bricks/agent_log/.

Concurrency: ActivityWorker is the single writer; reads can race with
writes from request threads. We hold a per-key lock for both append and
read so a snapshot returned to a reader is internally consistent.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass

_MOUNT_PREFIX = "/.activity/"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
        self._counter_lock = threading.Lock()
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
                with self._counter_lock:
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

    def list_dir(self, path: str) -> list[str]:
        with self._global_lock:
            if path == _MOUNT_PREFIX or path == _MOUNT_PREFIX.rstrip("/"):
                return sorted({k.date for k in self._buffers})
            date = _parse_date_dir(path)
            if date is None:
                return []
            return sorted(f"{k.agent_id}.jsonl" for k in self._buffers if k.date == date)

    def drop_date(self, date: str) -> None:
        with self._global_lock:
            keys = [k for k in self._buffers if k.date == date]
            # Safety: keys we iterate are already registered in _buffers, so they
            # are also in _locks (fast path in _lock_for). _lock_for therefore
            # never tries to acquire _global_lock for these keys while we hold
            # it. Concurrent _lock_for slow-path calls for unrelated keys queue
            # behind us — that's latency, not deadlock.
            for k in keys:
                # Acquire the per-key lock before discarding it, so any in-flight
                # append/read finishes first.
                lock = self._locks.get(k)
                if lock is not None:
                    with lock:
                        self._buffers.pop(k, None)
                        self._sizes.pop(k, None)
                else:
                    self._buffers.pop(k, None)
                    self._sizes.pop(k, None)
                self._locks.pop(k, None)


def _parse_file_path(path: str) -> tuple[str, str] | None:
    if not path.startswith(_MOUNT_PREFIX):
        return None
    rest = path[len(_MOUNT_PREFIX) :]
    if "/" not in rest:
        return None
    date, _, fname = rest.partition("/")
    if not fname.endswith(".jsonl"):
        return None
    agent_id = fname[: -len(".jsonl")]
    if not agent_id or not date:
        return None
    # Reject paths with extra slashes (e.g., /.activity/2026-05-09/extra/alice.jsonl)
    if "/" in agent_id:
        return None
    if not _DATE_RE.match(date):
        return None
    return agent_id, date


def _parse_date_dir(path: str) -> str | None:
    if not path.startswith(_MOUNT_PREFIX):
        return None
    rest = path[len(_MOUNT_PREFIX) :].rstrip("/")
    if "/" in rest or not rest:
        return None
    if not _DATE_RE.match(rest):
        return None
    return rest
