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
            try:
                line = self._build_line(e, meta)
                date = _utc_date(e.ts)
                self._store.append_line(agent, date, line)
            except Exception:  # never break the worker on a single bad event
                logger.warning("agent_log per-event failure", exc_info=True)

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
