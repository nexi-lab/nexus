"""Write-through RecordStore projection observer (#4738).

Receives FILE_WRITE / FILE_DELETE / FILE_RENAME / DIR_CREATE / DIR_DELETE
events from the audit interceptor (Rust kernel post-hooks relayed through
Python) and commits the *critical* projection rows — ``operation_log``,
``file_paths``, ``version_history`` — before the mutation returns to its
caller.  ``list_versions``, ``/api/v2/operations`` replay and the search
zone join on ``file_paths`` therefore never lag a write on this node.

History.  #809 moved this work off the hot path behind a 200 ms debounce
over an in-memory ``deque(maxlen=10_000)``: overflow dropped the oldest
events with only a counter, a crash lost everything in flight, and readers
had to call ``flush_write_observer``.  #4738 replaces the debounce with a
**synchronous group commit**::

    writer thread                        projection-commit thread
    ─────────────                        ────────────────────────
    on_write(...)                        wait for tickets
      ticket = _submit(events) ───────▶  drain ≤ _MAX_BATCH_DRAIN events
      ticket.done.wait(timeout)          ONE transaction: OperationLogger + VersionRecorder
      return ticket.seq          ◀────── ticket.seq = operation_log.sequence_number
                                         hand events to projection-deferred thread
                                                   │
                                                   ▼
                                         MCL rows + post-flush hooks (extraction, lineage)

* Concurrent writers share one transaction, so throughput under load matches
  the old batch path while every writer returns only after its own row is
  committed.  A single committer allocates ``sequence_number`` (MAX+1) — no
  unique-constraint retry storms.
* The sequence is the **projection sequence** a write returns as
  ``projection_seq``; ``GET /api/v2/operations/wait?seq=`` answers from the
  same column, so callers wait on it instead of calling
  ``flush_write_observer``.
* ``strict_mode=True`` (``AuditConfig`` default): a commit that fails after
  retries, or is not confirmed within ``write_through_timeout_s``, raises
  ``AuditLogError`` in the writer's thread — the typed error the write
  surfaces.  ``strict_mode=False``: the write succeeds with
  ``projection_seq=None``, the event stays queued for retry, and the gap is
  logged at CRITICAL.
* Nothing is dropped silently.  Both queues are bounded; the deferred queue
  backpressures the committer first; every drop is an ERROR log plus
  ``nexus_projection_events_dropped_total``.
* A crash between the kernel commit and the projection commit can still lose
  the rows in flight (the kernel is authoritative and already durable).
  ``storage/projection_reconcile.py`` — ``POST /api/v2/admin/reconcile-
  projections`` — repairs the projection from the kernel afterwards.
* ``write_through=False`` (``NEXUS_PROJECTION_MODE=async``) keeps everything
  above except the wait: the writer returns as soon as its ticket is queued
  and reports ``projection_seq: null``, and the committer coalesces tickets
  for ``debounce_seconds`` (0.2 s) before each group commit so a burst costs
  one transaction instead of one per write.  For tenants that keep their own
  version history and never fence on ``projection_seq`` this takes the
  projection transaction (round-trips + fsync + ORM CPU) off the write path;
  the crash window grows from "rows in flight" to "rows queued", still
  bounded and still repairable with reconcile.
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nexus.contracts.exceptions import AuditLogError
from nexus.lib import io_metrics

if TYPE_CHECKING:
    from nexus.core.file_events import FileEvent
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

_MAX_BATCH_DRAIN = 100  # Max events per commit transaction (group size)
_MAX_RETRIES = 3
_WRITE_THROUGH_TIMEOUT_S = 5.0  # How long a writer waits for its row to commit
_PENDING_MAX_EVENTS = 50_000  # Unconfirmed events kept for retry (non-strict mode)
_DEFERRED_MAX_EVENTS = 10_000  # Committed events awaiting MCL + post-flush hooks
_BACKPRESSURE_S = 2.0  # Committer blocks this long on a full deferred queue before dropping
_DEBOUNCE_S = 0.2  # Legacy constructor default (#809); no timer is driven by it any more

_RECONCILE_HINT = "run POST /api/v2/admin/reconcile-projections to repair the projection"
_REINDEX_HINT = "run POST /api/v2/admin/reindex to rebuild aspect/MCL state"


def _metadata_from_dict(d: dict[str, Any]) -> Any:
    """Reconstruct FileMetadata from to_dict() output."""
    from nexus.contracts.metadata import FileMetadata

    if d.get("created_at") and isinstance(d["created_at"], str):
        d["created_at"] = datetime.fromisoformat(d["created_at"])
    if d.get("modified_at") and isinstance(d["modified_at"], str):
        d["modified_at"] = datetime.fromisoformat(d["modified_at"])
    return FileMetadata(**d)


def _metadata_content_id(metadata: Any | None) -> str | None:
    if metadata is None:
        return None
    if isinstance(metadata, dict):
        value = metadata.get("content_id")
        return value if isinstance(value, str) else None
    value = getattr(metadata, "content_id", None)
    return value if isinstance(value, str) else None


def _metadata_snapshot(metadata: Any | None) -> dict[str, Any] | None:
    if metadata is None:
        return None
    if isinstance(metadata, dict):
        return metadata
    if hasattr(metadata, "to_dict"):
        snapshot = metadata.to_dict()
        return snapshot if isinstance(snapshot, dict) else None
    return None


class _Ticket:
    """One intake call's events plus the writer's completion signal."""

    __slots__ = ("done", "error", "events", "seq")

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.done = threading.Event()
        self.error: Exception | None = None
        self.seq: int | None = None

    def finish(self, error: Exception | None = None) -> None:
        seqs = [e["projection_seq"] for e in self.events if e.get("projection_seq") is not None]
        self.seq = max(seqs) if seqs else None
        self.error = error
        self.done.set()


class RecordStoreWriteObserver:
    """Write-through projection observer for RecordStore audit trail + versioning.

    Receives mutation events from ``SyncAuditWriteInterceptor`` and commits
    ``operation_log`` + ``file_paths`` + ``version_history`` in a group
    commit before returning; MCL rows and post-flush hooks run on a
    deferred thread.  See the module docstring for the full contract.

    Registration:
        Enlisted via factory orchestrator; events dispatched by the Rust
        kernel's post-hook path through ``SyncAuditWriteInterceptor``.
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        *,
        strict_mode: bool = True,
        event_signal: "Any | None" = None,
        debounce_seconds: float = _DEBOUNCE_S,
        write_through_timeout_s: float = _WRITE_THROUGH_TIMEOUT_S,
        write_through: bool = True,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._strict_mode = strict_mode
        self._event_signal = event_signal  # Issue #3193: wake delivery worker
        # Kept for constructor compatibility (#809 callers); the commit is
        # driven by writers waiting on their ticket, not by a timer.
        self._debounce = debounce_seconds
        self._timeout = write_through_timeout_s
        # ``write_through=False`` (NEXUS_PROJECTION_MODE=async): the writer
        # submits its ticket and returns without waiting for the commit, and
        # the committer coalesces tickets for ``debounce_seconds`` before each
        # transaction.  The group-commit thread, bounded queues, retry/salvage,
        # metrics and reconcile stay exactly the same; only read-your-projection
        # and the ``projection_seq`` fence are given up (writes report ``None``).
        self._write_through_enabled = write_through

        # Pending tickets (events accepted, not yet committed) — under _cond.
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pending: deque[_Ticket] = deque()
        self._pending_events = 0
        # Serialises "drain + commit" between the commit thread and flush_sync().
        self._commit_lock = threading.Lock()

        # Committed events awaiting MCL + hooks — under _deferred_cond.
        self._deferred_cond = threading.Condition()
        self._deferred: deque[list[dict[str, Any]]] = deque()
        self._deferred_events = 0

        self._stop = False
        self._commit_thread: threading.Thread | None = None
        self._deferred_thread: threading.Thread | None = None

        # Metrics
        self._total_flushed = 0
        self._total_failed = 0
        self._total_retries = 0
        self._total_dropped = 0
        self._total_timeouts = 0
        self._applied_seq: int | None = None

        # Post-flush hooks: called after successful commit (Issue #2978)
        # Used by CatalogService for async-on-write extraction
        self._post_flush_hooks: list[Any] = []

    def register_post_flush_hook(self, hook: Any) -> None:
        """Register a callback invoked after each successful commit.

        Hooks receive the list of committed events.  They run on the
        deferred thread AFTER the audit trail commit, so failures do not
        block the audit path or the writer.  Used by CatalogService for
        async-on-write extraction (Issue #2978).
        """
        self._post_flush_hooks.append(hook)

    # ------------------------------------------------------------------
    # Event intake — called by SyncAuditWriteInterceptor post-hooks
    # ------------------------------------------------------------------

    def on_write(
        self,
        metadata: Any,
        *,
        is_new: bool,
        path: str,
        old_metadata: Any | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> int | None:
        """Project a write; returns its projection sequence once committed."""
        if isinstance(old_metadata, dict):
            snapshot_hash = old_metadata.get("content_id")
            metadata_snapshot = old_metadata
        elif old_metadata is not None:
            snapshot_hash = getattr(old_metadata, "content_id", None)
            metadata_snapshot = (
                old_metadata.to_dict() if hasattr(old_metadata, "to_dict") else old_metadata
            )
        else:
            snapshot_hash = None
            metadata_snapshot = None

        event = {
            "op": "write",
            "path": path,
            "is_new": is_new,
            "zone_id": zone_id,
            "agent_id": agent_id,
            "snapshot_hash": snapshot_hash,
            "metadata_snapshot": metadata_snapshot,
            "metadata": metadata.to_dict() if hasattr(metadata, "to_dict") else metadata,
        }
        return self._write_through([event], operation="write", path=path)

    def on_write_batch(
        self,
        items: list[tuple[Any, bool]],
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        urgency: str | None = None,  # noqa: ARG002
    ) -> list[int | None]:
        """Project a batch write in one transaction; one sequence per item."""
        events = [
            {
                "op": "write",
                "path": metadata.path,
                "is_new": is_new,
                "zone_id": zone_id,
                "agent_id": agent_id,
                "snapshot_hash": None,
                "metadata_snapshot": None,
                "metadata": metadata.to_dict() if hasattr(metadata, "to_dict") else metadata,
            }
            for metadata, is_new in items
        ]
        if not events:
            return []
        confirmed = self._write_through(events, operation="write_batch", path=events[0]["path"])
        if confirmed is None:
            return [None] * len(events)
        return [e.get("projection_seq") for e in events]

    def on_delete(
        self,
        *,
        path: str,
        metadata: Any | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> int | None:
        """Project a delete; returns its projection sequence once committed."""
        event = {
            "op": "delete",
            "path": path,
            "zone_id": zone_id,
            "agent_id": agent_id,
            "snapshot_hash": _metadata_content_id(metadata),
            "metadata_snapshot": _metadata_snapshot(metadata),
        }
        return self._write_through([event], operation="delete", path=path)

    def on_rename(
        self,
        *,
        old_path: str,
        new_path: str,
        metadata: Any | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> int | None:
        """Project a rename; returns the sequence of its upsert row once committed."""
        event = {
            "op": "rename",
            "path": old_path,
            "new_path": new_path,
            "zone_id": zone_id,
            "agent_id": agent_id,
            "snapshot_hash": _metadata_content_id(metadata),
            "metadata_snapshot": _metadata_snapshot(metadata),
        }
        return self._write_through([event], operation="rename", path=old_path)

    def on_mkdir(
        self,
        *,
        path: str,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> int | None:
        """Project a mkdir; returns its projection sequence once committed."""
        event = {"op": "mkdir", "path": path, "zone_id": zone_id, "agent_id": agent_id}
        return self._write_through([event], operation="mkdir", path=path)

    def on_rmdir(
        self,
        *,
        path: str,
        zone_id: str | None = None,
        agent_id: str | None = None,
        recursive: bool = False,
    ) -> int | None:
        """Project an rmdir; returns its projection sequence once committed."""
        event = {
            "op": "rmdir",
            "path": path,
            "zone_id": zone_id,
            "agent_id": agent_id,
            "recursive": recursive,
        }
        return self._write_through([event], operation="rmdir", path=path)

    # ------------------------------------------------------------------
    # Write-through: submit, wait, apply the strict_mode policy
    # ------------------------------------------------------------------

    def _write_through(
        self, events: list[dict[str, Any]], *, operation: str, path: str
    ) -> int | None:
        """Queue ``events`` for the group commit and wait for the row(s) to land.

        Returns the ticket's projection sequence (highest
        ``operation_log.sequence_number`` among its events) or ``None`` when
        the commit is not confirmed and ``strict_mode`` is off.  Raises
        ``AuditLogError`` in strict mode when the commit failed or timed out.

        In async mode (``write_through=False``) the ticket is queued and the
        writer returns ``None`` at once; the commit thread still lands the
        row and a failure is still logged at ERROR + counted, it just cannot
        be reported to this caller.
        """
        ticket = self._submit(events)
        if not self._write_through_enabled:
            return None
        if not ticket.done.wait(self._timeout):
            self._total_timeouts += 1
            io_metrics.record_projection_write_through_timeout()
            detail = (
                f"projection of {operation} on '{path}' not confirmed within "
                f"{self._timeout:.1f}s (still queued for retry)"
            )
            if self._strict_mode:
                logger.error(
                    "AUDIT LOG FAILURE: %s on '%s' ABORTED. %s. "
                    "Set audit_strict_mode=False to allow writes without a confirmed audit row.",
                    operation,
                    path,
                    detail,
                )
                raise AuditLogError(f"Operation aborted: {detail}", path=path)
            logger.critical(
                "AUDIT LOG FAILURE: %s on '%s' SUCCEEDED but %s. "
                "Callers that fence on projection_seq see None for this write.",
                operation,
                path,
                detail,
            )
            return None
        if ticket.error is not None:
            if self._strict_mode:
                logger.error(
                    "AUDIT LOG FAILURE: %s on '%s' ABORTED. Error: %s. "
                    "Set audit_strict_mode=False to allow writes without audit logs.",
                    operation,
                    path,
                    ticket.error,
                )
                raise AuditLogError(
                    f"Operation aborted: audit logging failed for {operation}: {ticket.error}",
                    path=path,
                    original_error=ticket.error,
                ) from ticket.error
            # Non-strict: the salvage path already logged the lost row at ERROR.
            return None
        return ticket.seq

    def _submit(self, events: list[dict[str, Any]]) -> _Ticket:
        ticket = _Ticket(events)
        with self._cond:
            self._ensure_threads_locked()
            self._pending.append(ticket)
            self._pending_events += len(events)
            self._enforce_pending_bound_locked()
            pending = self._pending_events
            self._cond.notify()
        io_metrics.set_projection_pending("pending", pending)
        return ticket

    def _enqueue(self, event: dict[str, Any]) -> _Ticket:
        """Queue one event without waiting for it (shutdown / test helper)."""
        return self._submit([event])

    def _enforce_pending_bound_locked(self) -> None:
        """Drop the oldest unconfirmed tickets once the pending queue overflows.

        Only reachable in non-strict mode while the RecordStore has been
        unavailable long enough to accumulate ``_PENDING_MAX_EVENTS``
        (strict mode raises at the writer instead of orphaning tickets).
        Never silent: ERROR log + ``nexus_projection_events_dropped_total``.
        """
        while self._pending_events > _PENDING_MAX_EVENTS and len(self._pending) > 1:
            victim = self._pending.popleft()
            n = len(victim.events)
            self._pending_events -= n
            self._total_dropped += n
            io_metrics.record_projection_dropped("pending", n)
            first = victim.events[0]
            logger.error(
                "RecordStoreWriteObserver dropped %d unconfirmed event(s) (pending queue over "
                "%d): op=%s path=%s — their version/audit rows are LOST; %s",
                n,
                _PENDING_MAX_EVENTS,
                first.get("op"),
                first.get("path"),
                _RECONCILE_HINT,
            )
            victim.finish(RuntimeError("projection pending queue overflow"))

    def _ensure_threads_locked(self) -> None:
        """Start the commit + deferred threads lazily (caller holds ``_cond``)."""
        if self._commit_thread is not None and self._commit_thread.is_alive():
            return
        self._stop = False
        self._commit_thread = threading.Thread(
            target=self._commit_loop, name="projection-commit", daemon=True
        )
        self._commit_thread.start()
        if self._deferred_thread is None or not self._deferred_thread.is_alive():
            self._deferred_thread = threading.Thread(
                target=self._deferred_loop, name="projection-deferred", daemon=True
            )
            self._deferred_thread.start()

    # ------------------------------------------------------------------
    # Commit thread: group commit of the critical rows
    # ------------------------------------------------------------------

    def _drain_locked(self, max_events: int | None) -> list[_Ticket]:
        """Pop tickets up to ``max_events`` (at least one; caller holds ``_cond``)."""
        tickets: list[_Ticket] = []
        total = 0
        while self._pending:
            nxt = self._pending[0]
            if tickets and max_events is not None and total + len(nxt.events) > max_events:
                break
            self._pending.popleft()
            tickets.append(nxt)
            total += len(nxt.events)
        self._pending_events -= total
        return tickets

    def _commit_loop(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._stop:
                    self._cond.wait()
                if self._stop and not self._pending:
                    return
                if not self._write_through_enabled and self._debounce > 0 and not self._stop:
                    # Async mode: nobody is waiting on these tickets, so
                    # coalesce the ones that arrive within the debounce window
                    # into ONE transaction (the pre-#4738 batching).  Per-write
                    # commits would otherwise cost a session + ORM flush + fsync
                    # each, and that CPU competes with request threads for
                    # the GIL right after every write.
                    self._cond.wait_for(lambda: self._stop, timeout=self._debounce)
            with self._commit_lock:
                with self._cond:
                    tickets = self._drain_locked(_MAX_BATCH_DRAIN)
                    pending = self._pending_events
                io_metrics.set_projection_pending("pending", pending)
                if tickets:
                    self._commit_tickets(tickets)

    def _commit_tickets(self, tickets: list[_Ticket]) -> int:
        """Commit the tickets' events in one transaction; returns rows committed.

        Retries the whole batch with backoff, then salvages per event so a
        single poison row (#4645) cannot take the batch's audit trail down.
        Caller holds ``_commit_lock``.
        """
        events = [e for t in tickets for e in t.events]
        attempt = 0
        while True:
            try:
                self._flush_batch_sync(events)
                break
            except Exception as exc:
                if attempt < _MAX_RETRIES:
                    self._total_retries += 1
                    wait = 0.1 * (2**attempt)  # 100ms, 200ms, 400ms
                    logger.warning(
                        "RecordStoreWriteObserver commit failed (attempt %d/%d, retry in %.1fs): %s",
                        attempt + 1,
                        _MAX_RETRIES,
                        wait,
                        exc,
                    )
                    attempt += 1
                    time.sleep(wait)
                    continue
                logger.error(
                    "RecordStoreWriteObserver commit FAILED after %d retries, "
                    "salvaging %d events individually: %s",
                    _MAX_RETRIES,
                    len(events),
                    exc,
                )
                return self._salvage_tickets(tickets)

        for ticket in tickets:
            ticket.finish()
        self._after_commit(events)
        return len(events)

    def _salvage_tickets(self, tickets: list[_Ticket]) -> int:
        """Last-resort per-event commit after a batch repeatedly failed (#4645).

        Each event gets its own transaction; only the rows that individually
        fail are lost, every loss is logged at ERROR with op + path, and the
        ticket carries the first failure so a strict-mode writer raises.
        """
        salvaged: list[dict[str, Any]] = []
        for ticket in tickets:
            first_error: Exception | None = None
            for event in ticket.events:
                try:
                    self._flush_batch_sync([event])
                    salvaged.append(event)
                except Exception as event_err:
                    self._total_failed += 1
                    io_metrics.record_projection_failed(1)
                    first_error = first_error or event_err
                    logger.error(
                        "RecordStoreWriteObserver dropping audit event: op=%s path=%s error=%s — %s",
                        event.get("op"),
                        event.get("path"),
                        event_err,
                        _RECONCILE_HINT,
                    )
            ticket.finish(first_error)
        if salvaged:
            self._after_commit(salvaged)
        return len(salvaged)

    def _after_commit(self, events: list[dict[str, Any]]) -> None:
        """Bookkeeping after critical rows committed: metrics, signal, deferred work."""
        self._total_flushed += len(events)
        seqs = [e["projection_seq"] for e in events if e.get("projection_seq") is not None]
        if seqs:
            top = max(seqs)
            if self._applied_seq is None or top > self._applied_seq:
                self._applied_seq = top
        # Issue #3193: signal delivery worker immediately after commit
        if self._event_signal is not None:
            self._event_signal.set()
        self._enqueue_deferred(events)

    # ------------------------------------------------------------------
    # Deferred thread: MCL rows + post-flush hooks (off the writer's path)
    # ------------------------------------------------------------------

    def _enqueue_deferred(self, events: list[dict[str, Any]]) -> None:
        """Hand committed events to the deferred thread, with backpressure.

        A full queue first blocks the committer (and therefore the writers)
        for ``_BACKPRESSURE_S``; only then are the oldest batches dropped,
        loudly.  Dropping here loses MCL rows + hooks, never audit/version
        rows — those are already committed.
        """
        with self._deferred_cond:
            deadline = time.monotonic() + _BACKPRESSURE_S
            while (
                self._deferred_events + len(events) > _DEFERRED_MAX_EVENTS
                and self._deferred
                and not self._stop
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._deferred_cond.wait(remaining)
            while self._deferred_events + len(events) > _DEFERRED_MAX_EVENTS and self._deferred:
                dropped = self._deferred.popleft()
                n = len(dropped)
                self._deferred_events -= n
                self._total_dropped += n
                io_metrics.record_projection_dropped("deferred", n)
                logger.error(
                    "RecordStoreWriteObserver dropped %d committed event(s) from the deferred "
                    "queue (over %d after %.1fs of backpressure): first op=%s path=%s — their "
                    "MCL rows and post-flush hooks will not run; %s",
                    n,
                    _DEFERRED_MAX_EVENTS,
                    _BACKPRESSURE_S,
                    dropped[0].get("op"),
                    dropped[0].get("path"),
                    _REINDEX_HINT,
                )
            self._deferred.append(events)
            self._deferred_events += len(events)
            depth = self._deferred_events
            self._deferred_cond.notify_all()
        io_metrics.set_projection_pending("deferred", depth)

    def _deferred_loop(self) -> None:
        while True:
            with self._deferred_cond:
                while not self._deferred and not self._stop:
                    self._deferred_cond.wait()
                if not self._deferred:
                    return
                batch = self._deferred.popleft()
                self._deferred_events -= len(batch)
                depth = self._deferred_events
                self._deferred_cond.notify_all()
            io_metrics.set_projection_pending("deferred", depth)
            self._run_deferred(batch)

    def _run_deferred(self, events: list[dict[str, Any]]) -> None:
        """Phase 2 for committed events: MCL rows, then post-flush hooks."""
        self._record_mcl_batch(events)
        for hook in self._post_flush_hooks:
            try:
                hook(events)
            except Exception as hook_err:
                logger.debug(
                    "Post-flush hook %s failed (non-critical): %s",
                    getattr(hook, "__name__", hook),
                    hook_err,
                )

    def _drain_deferred_inline(self) -> None:
        """Run all queued deferred work on the calling thread (shutdown)."""
        while True:
            with self._deferred_cond:
                if not self._deferred:
                    return
                batch = self._deferred.popleft()
                self._deferred_events -= len(batch)
                self._deferred_cond.notify_all()
            self._run_deferred(batch)

    # ------------------------------------------------------------------
    # Flush (CLI shutdown / close callbacks / flush_write_observer RPC)
    # ------------------------------------------------------------------

    def flush_sync(self) -> int:
        """Commit every pending event and run all deferred work, inline.

        Returns the number of events committed by this call.  With
        write-through the pending queue is normally empty (each writer has
        already waited for its own row); this drains whatever a non-strict
        timeout or a shutdown left behind.
        """
        committed = 0
        with self._commit_lock:
            with self._cond:
                tickets = self._drain_locked(None)
            io_metrics.set_projection_pending("pending", 0)
            if tickets:
                committed = self._commit_tickets(tickets)
        self._drain_deferred_inline()
        io_metrics.set_projection_pending("deferred", 0)
        if committed:
            logger.debug("flush_sync: committed %d events", committed)
        return committed

    async def flush(self, timeout: float = 5.0) -> int:  # noqa: ARG002
        """Flush pending events. Async signature for protocol compat.

        Delegates to flush_sync() since no pipe draining is needed.
        """
        return self.flush_sync()

    # ------------------------------------------------------------------
    # Event conversion: FileEvent -> audit dict
    # ------------------------------------------------------------------

    @staticmethod
    def _file_event_to_dict(event: "FileEvent") -> dict[str, Any] | None:
        """Convert a kernel FileEvent to the dict format used by _process_events_in_session."""
        from nexus.core.file_events import FileEventType

        etype = event.type if isinstance(event.type, str) else event.type.value

        if etype == FileEventType.FILE_WRITE:
            return {
                "op": "write",
                "path": event.path,
                "is_new": event.is_new,
                "zone_id": event.zone_id,
                "agent_id": event.agent_id,
                "snapshot_hash": event.old_content_id,
                "metadata_snapshot": None,
                "metadata": event.to_dict(),
            }
        elif etype == FileEventType.FILE_DELETE:
            return {
                "op": "delete",
                "path": event.path,
                "zone_id": event.zone_id,
                "agent_id": event.agent_id,
                "snapshot_hash": event.content_id,
                "metadata_snapshot": None,
            }
        elif etype == FileEventType.FILE_RENAME:
            return {
                "op": "rename",
                "path": event.old_path or event.path,
                "new_path": event.new_path or event.path,
                "zone_id": event.zone_id,
                "agent_id": event.agent_id,
                "snapshot_hash": event.content_id,
                "metadata_snapshot": None,
            }
        elif etype == FileEventType.DIR_CREATE:
            return {
                "op": "mkdir",
                "path": event.path,
                "zone_id": event.zone_id,
                "agent_id": event.agent_id,
            }
        elif etype == FileEventType.DIR_DELETE:
            return {
                "op": "rmdir",
                "path": event.path,
                "zone_id": event.zone_id,
                "agent_id": event.agent_id,
                "recursive": False,
            }
        else:
            return None  # Unsupported event type — ignore

    # ------------------------------------------------------------------
    # DB logic
    # ------------------------------------------------------------------

    @staticmethod
    def _build_urn(path: str, zone_id: str | None) -> str:
        """Build a locator URN for a file from its virtual path.

        Delegates to NexusURN.for_file() -- single source of truth for
        URN construction (Issue #2978, Issue #2929 Key Decision #3).
        """
        from nexus.contracts.urn import NexusURN

        return str(NexusURN.for_file(zone_id or "default", path))

    def _record_mcl_for_event(
        self,
        session: Any,
        event: dict[str, Any],
    ) -> None:
        """Record MCL entry for a single event. Non-critical, uses savepoint.

        MCL failures must NEVER corrupt the outer session transaction.
        """
        try:
            from nexus.storage.mcl_recorder import MCLRecorder

            op = event["op"]
            zone_id = event.get("zone_id")
            agent_id = event.get("agent_id")
            path = event["path"]
            changed_by = agent_id or "system"

            with session.begin_nested():
                recorder = MCLRecorder(session)
                if op == "write":
                    urn = self._build_urn(path, zone_id)
                    recorder.record_file_write(
                        entity_urn=urn,
                        metadata_dict=event.get("metadata"),
                        zone_id=zone_id,
                        changed_by=changed_by,
                        previous_metadata=event.get("metadata_snapshot"),
                    )
                elif op == "delete":
                    from nexus.storage.aspect_service import AspectService

                    urn = self._build_urn(path, zone_id)
                    recorder.record_file_delete(
                        entity_urn=urn,
                        zone_id=zone_id,
                        changed_by=changed_by,
                        previous_metadata=event.get("metadata_snapshot"),
                    )
                    AspectService(session).soft_delete_entity_aspects(urn)
                elif op == "rename":
                    old_urn = self._build_urn(path, zone_id)
                    new_path = event.get("new_path", "")
                    new_urn = self._build_urn(new_path, zone_id)
                    recorder.record_file_delete(
                        entity_urn=old_urn,
                        zone_id=zone_id,
                        changed_by=changed_by,
                        previous_metadata=event.get("metadata_snapshot"),
                    )
                    recorder.record_file_write(
                        entity_urn=new_urn,
                        metadata_dict=event.get("metadata_snapshot"),
                        zone_id=zone_id,
                        changed_by=changed_by,
                    )
        except Exception:
            logger.debug(
                "MCL recording failed for %s:%s (non-critical)", event.get("op"), event.get("path")
            )

    def _record_mcl_batch(self, events: list[dict[str, Any]]) -> None:
        """Record MCL rows for committed events in their own session (non-critical)."""
        mcl_session = None
        try:
            mcl_session = self._session_factory()
            for event in events:
                if event.get("op") in ("write", "delete", "rename"):
                    self._record_mcl_for_event(mcl_session, event)
            mcl_session.commit()
        except Exception as mcl_err:
            if mcl_session is not None:
                mcl_session.rollback()
            logger.debug("MCL batch recording failed (non-critical): %s", mcl_err)
        finally:
            if mcl_session is not None:
                mcl_session.close()

    def _process_events_in_session(self, session: Any, events: list[dict[str, Any]]) -> None:
        """Dispatch events to OperationLogger + VersionRecorder within a session.

        Stamps every event with ``projection_seq`` — the
        ``operation_log.sequence_number`` of its (last) audit row.  Caller
        is responsible for ``session.commit()``.
        """
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        op_logger = OperationLogger(session)
        recorder = VersionRecorder(session)

        for event in events:
            op = event["op"]
            zone_id = event.get("zone_id")
            agent_id = event.get("agent_id")

            if op == "write":
                urn = self._build_urn(event["path"], zone_id)
                op_logger.log_operation(
                    operation_type="write",
                    path=event["path"],
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=event.get("snapshot_hash"),
                    metadata_snapshot=event.get("metadata"),
                    status="success",
                    entity_urn=urn,
                    aspect_name="file_metadata",
                    change_type="upsert",
                )
                md = _metadata_from_dict(event["metadata"])
                recorder.record_write(md, is_new=event["is_new"])

            elif op == "delete":
                urn = self._build_urn(event["path"], zone_id)
                op_logger.log_operation(
                    operation_type="delete",
                    path=event["path"],
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=event.get("snapshot_hash"),
                    metadata_snapshot=event.get("metadata_snapshot"),
                    status="success",
                    entity_urn=urn,
                    aspect_name="file_metadata",
                    change_type="delete",
                )
                recorder.record_delete(event["path"])
                from nexus.storage.aspect_service import AspectService

                AspectService(session).soft_delete_entity_aspects(urn)

            elif op == "rename":
                old_urn = self._build_urn(event["path"], zone_id)
                new_path = event.get("new_path", "")
                new_urn = self._build_urn(new_path, zone_id)
                op_logger.log_operation(
                    operation_type="rename",
                    path=event["path"],
                    new_path=new_path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=event.get("snapshot_hash"),
                    metadata_snapshot=event.get("metadata_snapshot"),
                    status="success",
                    entity_urn=old_urn,
                    aspect_name="file_metadata",
                    change_type="delete",
                )
                op_logger.log_operation(
                    operation_type="rename",
                    path=new_path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    metadata_snapshot=event.get("metadata_snapshot"),
                    status="success",
                    entity_urn=new_urn,
                    aspect_name="file_metadata",
                    change_type="upsert",
                )
                if new_path:
                    recorder.record_rename(event["path"], new_path, zone_id=zone_id)

            elif op == "mkdir":
                op_logger.log_operation(
                    operation_type="mkdir",
                    path=event["path"],
                    zone_id=zone_id,
                    agent_id=agent_id,
                    status="success",
                )

            elif op == "rmdir":
                op_type = "rmdir_recursive" if event.get("recursive") else "rmdir"
                op_logger.log_operation(
                    operation_type=op_type,
                    path=event["path"],
                    zone_id=zone_id,
                    agent_id=agent_id,
                    status="success",
                )

            event["projection_seq"] = op_logger.last_sequence_number

    def _flush_batch_sync(self, events: list[dict[str, Any]]) -> None:
        """Commit the critical rows for ``events`` in ONE transaction.

        operation_log + file_paths + version_history only.  MCL rows and
        post-flush hooks are deferred work (``_run_deferred``) so a slow
        extraction never sits on a writer's critical path.
        """
        session = self._session_factory()
        try:
            self._process_events_in_session(session, events)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Stop the commit + deferred threads (for clean shutdown).

        Pending events are committed before the commit thread exits; call
        ``flush_sync()`` first when deferred work (MCL, hooks) must also
        complete synchronously.
        """
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        with self._deferred_cond:
            self._deferred_cond.notify_all()
        for thread in (self._commit_thread, self._deferred_thread):
            if (
                thread is not None
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def metrics(self) -> dict[str, int | None]:
        """Return observer metrics."""
        with self._cond:
            pending = self._pending_events
        with self._deferred_cond:
            deferred = self._deferred_events
        return {
            "total_flushed": self._total_flushed,
            "total_failed": self._total_failed,
            "total_retries": self._total_retries,
            "total_dropped": self._total_dropped,
            "total_timeouts": self._total_timeouts,
            "pending_events": pending,
            "deferred_events": deferred,
            "applied_seq": self._applied_seq,
        }


# Backward-compat alias so existing imports don't break during migration
PipedRecordStoreWriteObserver = RecordStoreWriteObserver
