"""``AuditNode`` — audit collect/gather service.

Run-time placement: opt-in service started by an operator on a node
joining the federation as an audit-only role.  Production nodes do
NOT run this service; they generate traces via the ``AuditHook``
installed by ``services::audit::install``.

Two responsibilities:

1. **Bootstrap**: create the audit-node's own zone (centralised
   storage), join every production zone as a raft learner, and
   register the ``/audit/traces/`` DT_STREAM locally on each joined
   zone (without an ``AuditHook`` — the audit-node is a consumer,
   not a producer).

2. **Collect/gather loop**: an async task that polls every joined
   zone's ``/audit/traces/`` stream from the persisted offset,
   appends new entries to the audit-node's local zone, and persists
   the new offset.  Layout in the audit-node's local zone:

   ```
   /{audit_zone_id}/collect/{source_zone}/traces/   ← appended copies
   /{audit_zone_id}/collect/{source_zone}/offset    ← last-read position
   ```

   The ``/{audit_zone_id}/collect/{source_zone}/traces/`` path is a
   regular DT_STREAM on the audit-node — long-lived storage,
   independent of raft replication.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class AuditCheckpoint:
    """Per-source-zone offset tracker.

    The collect loop writes the next-to-read offset back to
    ``/{audit_zone_id}/collect/{source_zone}/offset`` after every
    successful batch flush, so a restart resumes where the previous
    run left off.
    """

    source_zone: str
    offset: int


class AuditNode:
    """Audit-node service: bootstrap + collect/gather loop.

    Construct with the kernel handle and the audit-node's local zone
    id (e.g. ``"audit"``).  Call :meth:`bootstrap` once at startup,
    then :meth:`run` to start the collect loop (returns the running
    asyncio.Task).
    """

    def __init__(
        self,
        kernel: Any,
        *,
        audit_zone_id: str,
        stream_path: str = "/audit/traces/",
        batch_size: int = 256,
        poll_interval_secs: float = 1.0,
    ) -> None:
        self._kernel = kernel
        self._audit_zone_id = audit_zone_id
        self._stream_path = stream_path
        self._batch_size = batch_size
        self._poll_interval = poll_interval_secs
        # source_zone -> AuditCheckpoint
        self._checkpoints: dict[str, AuditCheckpoint] = {}
        self._stop_event = asyncio.Event()

    # ── Bootstrap ────────────────────────────────────────────────────

    def bootstrap(self, production_zones: list[str]) -> None:
        """Create the audit zone and join each production zone as learner.

        Idempotent: ``federation_create_zone`` rejects duplicates with
        a clear error that we treat as success when the zone already
        exists; ``federation_join_zone`` is similarly idempotent.

        ``prepare_audit_stream_only`` must run for every joined zone
        so ``stream_read_batch`` knows about the path on the
        audit-node side (the WAL stream backend itself is
        raft-replicated and doesn't need explicit registration on
        learners — but the kernel's ``stream_manager`` does).
        """
        import nexus_runtime  # local import — keeps test envs working

        # 1. Create the audit-node's own central zone.
        try:
            nexus_runtime.federation_create_zone(self._kernel, self._audit_zone_id)
            logger.info("[audit-node] created audit zone %r", self._audit_zone_id)
        except Exception as exc:  # pragma: no cover — race with operator
            # Idempotent: existing-zone errors are expected on restart.
            logger.debug(
                "[audit-node] audit zone %r already present (%s)",
                self._audit_zone_id,
                exc,
            )

        # 2. Join every production zone as learner + register the
        #    audit stream locally.
        for zone in production_zones:
            try:
                nexus_runtime.federation_join_zone(self._kernel, zone, as_learner=True)
                logger.info("[audit-node] joined zone %r as learner", zone)
            except Exception as exc:
                logger.warning(
                    "[audit-node] join_zone(%r, as_learner=True) failed: %s",
                    zone,
                    exc,
                )
                continue
            try:
                nexus_runtime.prepare_audit_stream_only(self._kernel, zone, self._stream_path)
                logger.info("[audit-node] registered audit stream for zone %r", zone)
            except Exception as exc:
                logger.warning(
                    "[audit-node] prepare_audit_stream_only(%r) failed: %s",
                    zone,
                    exc,
                )
                continue
            # Restore offset from persisted state if it exists.
            offset = self._read_offset(zone)
            self._checkpoints[zone] = AuditCheckpoint(zone, offset)

    # ── Collect loop ─────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the collect loop until :meth:`stop` is called.

        Per iteration: poll every checkpointed zone, drain its
        stream up to ``batch_size`` records, append to the local
        per-source-zone trace stream, persist the new offset.  Sleep
        ``poll_interval_secs`` between iterations.
        """
        logger.info(
            "[audit-node] collect loop starting (zones=%d, batch=%d, interval=%.2fs)",
            len(self._checkpoints),
            self._batch_size,
            self._poll_interval,
        )
        while not self._stop_event.is_set():
            self._poll_once()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval,
                )
            except TimeoutError:
                continue
        logger.info("[audit-node] collect loop stopped")

    def stop(self) -> None:
        """Signal :meth:`run` to exit on its next iteration."""
        self._stop_event.set()

    # ── Internals ────────────────────────────────────────────────────

    def _poll_once(self) -> int:
        """Drain a single batch from every checkpointed zone.

        Returns the total number of records collected across zones in
        this iteration (mainly for tests + observability).
        """
        total = 0
        for zone, checkpoint in list(self._checkpoints.items()):
            try:
                count = self._drain_zone(zone, checkpoint)
            except Exception as exc:
                logger.warning(
                    "[audit-node] drain zone=%r offset=%d failed: %s",
                    zone,
                    checkpoint.offset,
                    exc,
                )
                continue
            total += count
        return total

    def _drain_zone(self, zone: str, checkpoint: AuditCheckpoint) -> int:
        """Read up to ``batch_size`` entries from ``zone``'s audit stream.

        Returns the number of records collected.  Updates
        ``checkpoint.offset`` and persists the new offset on success.
        """
        source_path = f"/{zone}{self._stream_path}".rstrip("/")
        # ``stream_read_batch`` returns ``(entries, new_offset)``;
        # record bytes are JSON-encoded ``AuditRecord`` blobs the
        # producer wrote via ``AuditHook``.
        entries, new_offset = self._kernel.stream_read_batch(
            source_path,
            checkpoint.offset,
            self._batch_size,
        )
        if not entries:
            return 0

        target_path = self._collect_traces_path(zone)
        for raw in entries:
            # ``stream_write_nowait`` is the append-only writer for
            # DT_STREAM paths; returns the appended offset.  We don't
            # care about the local offset on the audit-node — only
            # the source-zone offset that we persist as a checkpoint.
            self._kernel.stream_write_nowait(target_path, raw)

        checkpoint.offset = new_offset
        self._write_offset(zone, new_offset)
        logger.debug(
            "[audit-node] zone=%r drained=%d new_offset=%d",
            zone,
            len(entries),
            new_offset,
        )
        return len(entries)

    def _collect_traces_path(self, source_zone: str) -> str:
        return f"/{self._audit_zone_id}/collect/{source_zone}/traces"

    def _offset_path(self, source_zone: str) -> str:
        return f"/{self._audit_zone_id}/collect/{source_zone}/offset"

    def _read_offset(self, source_zone: str) -> int:
        """Read the persisted offset for a source zone, default 0."""
        path = self._offset_path(source_zone)
        try:
            data = self._kernel.sys_read(path)
        except Exception:
            return 0
        if data is None:
            return 0
        try:
            return int(json.loads(data).get("offset", 0))
        except (ValueError, json.JSONDecodeError):
            return 0

    def _write_offset(self, source_zone: str, offset: int) -> None:
        path = self._offset_path(source_zone)
        payload = json.dumps({"offset": offset}).encode("utf-8")
        try:
            self._kernel.sys_write(path, payload)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "[audit-node] failed to persist offset for zone=%r: %s",
                source_zone,
                exc,
            )
