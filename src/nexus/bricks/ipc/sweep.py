"""TTL expiry sweeper for filesystem-as-IPC.

Background task that moves expired messages to dead_letter/.

Issue #3197: Supports two sweep modes:
  1. Event-driven via CacheStore pub/sub (low-latency, targeted per-agent)
  2. Fallback periodic poll (safety net, full scan)

Events are debounced: rapid TTL schedule events are coalesced into a
single sweep after a short delay (default 2s).  The poll fallback runs
at a longer interval (default 300s) and scans ALL agent inboxes.

Retention (poll-only, runs every sweep cycle):
  - inbox stale drain: dead-letters no-TTL inbox messages older than
    ``inbox_stale_hours`` (dead consumer relief valve).
  - processed/ and outbox/ TTL delete: files older than the configured
    retention window are deleted outright (already captured by event log).
  - dead_letter/ compaction: per-day JSONL archive segments written with
    a two-phase commit (.tmp → final → delete originals) so the archive
    is always crash-safe.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nexus.bricks.ipc.conventions import (
    AGENTS_ROOT,
    OUTBOX_DIR,
    PROCESSED_DIR,
    dead_letter_archive_path,
    dead_letter_archive_segment,
    dead_letter_archive_tmp,
    dead_letter_path,
    inbox_path,
)
from nexus.bricks.ipc.envelope import MessageEnvelope
from nexus.bricks.ipc.exceptions import DLQReason
from nexus.bricks.ipc.lifecycle import dead_letter_message
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.contracts.cache_store import CacheStoreABC

logger = logging.getLogger(__name__)

# Default sweep interval (fallback poll) in seconds.
DEFAULT_SWEEP_INTERVAL = 60

# Default debounce delay for event-driven sweeps (seconds).
DEFAULT_DEBOUNCE_SECONDS = 2.0

# Default retention windows.
DEFAULT_INBOX_STALE_HOURS = 24
DEFAULT_PROCESSED_RETENTION_DAYS = 7
DEFAULT_OUTBOX_RETENTION_DAYS = 7
DEFAULT_DEAD_LETTER_COMPACT_MIN_FILES = 50
DEFAULT_DEAD_LETTER_ARCHIVE_RETENTION_DAYS = 30
DEFAULT_DEAD_LETTER_MAX_FILES_PER_SEGMENT = 200
# Conservative: DLQ files must be at least this old before being compacted.
# Decoupled from inbox_stale_hours so aggressive inbox draining doesn't
# accidentally hide fresh DLQ evidence before operators can inspect it.
DEFAULT_DEAD_LETTER_COMPACT_MIN_AGE_HOURS = 72  # 3 days

# Minutes before an orphaned .arch_* claimed file is considered stale and recovered.
# Must be comfortably longer than the worst-case compaction time (build archive +
# two VFS writes for a full segment). Set conservatively at 30 minutes — in practice
# archiving 200 files takes seconds. A future lease/heartbeat mechanism would
# remove this assumption for very slow deployments.
_CLAIMED_STALE_MINUTES = 30


class TTLSweeper:
    """Background sweeper that moves expired messages to dead_letter/.

    Operates in two modes (can be combined):

    **Event-driven** (when ``cache_store`` is provided):
      MessageSender publishes ``ipc:ttl:schedule:{zone_id}`` events when
      sending messages with TTLs.  The sweeper subscribes, debounces rapid
      events, and sweeps only the targeted agent's inbox.

    **Fallback poll** (always active):
      Periodic full scan of all agent inboxes.  Acts as a safety net for
      missed pub/sub events (subscriber disconnect, restart, etc.).

    Retention sweep (runs during each fallback poll cycle):
      - ``_drain_stale_inbox``: dead-letters no-TTL inbox messages older
        than ``inbox_stale_hours``.
      - ``_prune_dir``: deletes processed/ and outbox/ files older than
        their configured retention window.
      - ``_compact_dead_letter``: packs aged dead_letter/ files into
        per-day JSONL archives with two-phase crash-safe commit.

    Args:
        vfs: NexusFS instance for IPC listing, reading, and renaming.
        zone_id: Zone ID for multi-tenant isolation.
        interval: Seconds between fallback poll cycles.
        cache_store: CacheStoreABC for event-driven TTL pub/sub. Optional.
        debounce_seconds: Delay before sweeping after a pub/sub event.
        inbox_stale_hours: Dead-letter no-TTL inbox messages older than
            this many hours. None (default) disables the drain — opt-in
            explicitly via startup config. Suggested: DEFAULT_INBOX_STALE_HOURS.
        processed_retention_days: Delete processed/ files older than this.
            None (default) disables. Suggested: DEFAULT_PROCESSED_RETENTION_DAYS.
        outbox_retention_days: Delete outbox/ files older than this.
            None (default) disables. Suggested: DEFAULT_OUTBOX_RETENTION_DAYS.
        dead_letter_compact_min_files: Minimum files in a day-bucket before
            compaction triggers. None (default) disables. Suggested:
            DEFAULT_DEAD_LETTER_COMPACT_MIN_FILES.
        dead_letter_compact_min_age_hours: A DLQ file must be at least this
            many hours old before being eligible for compaction. Decoupled
            from ``inbox_stale_hours`` so that aggressive inbox draining does
            not hide fresh DLQ evidence. Defaults to
            ``DEFAULT_DEAD_LETTER_COMPACT_MIN_AGE_HOURS`` (72h / 3 days).
        dead_letter_compact_delete_originals: When ``False`` (default), compaction
            writes archive segments but preserves the original ``.json`` and
            ``.reason.json`` files. Set to ``True`` only after verifying that
            archive inspection/replay tooling works for your deployment —
            deletion is irreversible and makes raw DLQ data inaccessible.
        dead_letter_archive_retention_days: Delete archive segments older
            than this. None (default) disables. Suggested:
            DEFAULT_DEAD_LETTER_ARCHIVE_RETENTION_DAYS.
        dead_letter_max_files_per_segment: Safety cap on how many files are
            packed into one archive segment per sweep cycle. Prevents
            unbounded memory usage when a day bucket is very large.
            Remaining files are picked up in subsequent cycles.

    .. warning::
        Dead-letter compaction (``dead_letter_compact_min_files``) converts
        raw ``.json`` + ``.reason.json`` files into ``.jsonl`` archive
        segments under ``dead_letter/_archive/``. No automated reader,
        replay, or CLI tooling exists for the archive format yet. Enabling
        compaction makes older DLQ data accessible only via direct file
        reads of the ``.jsonl`` segments. Only enable once you have
        evaluated this operational trade-off.
    """

    def __init__(
        self,
        vfs: Any,
        zone_id: str = ROOT_ZONE_ID,
        interval: float = DEFAULT_SWEEP_INTERVAL,
        cache_store: "CacheStoreABC | None" = None,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
        inbox_stale_hours: int | None = None,
        processed_retention_days: int | None = None,
        outbox_retention_days: int | None = None,
        dead_letter_compact_min_files: int | None = None,
        dead_letter_compact_min_age_hours: int = DEFAULT_DEAD_LETTER_COMPACT_MIN_AGE_HOURS,
        dead_letter_compact_delete_originals: bool = False,
        dead_letter_archive_retention_days: int | None = None,
        dead_letter_max_files_per_segment: int = DEFAULT_DEAD_LETTER_MAX_FILES_PER_SEGMENT,
    ) -> None:
        self._vfs = vfs
        self._zone_id = zone_id
        self._interval = interval
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._cache_store = cache_store
        self._debounce_seconds = debounce_seconds
        self._sub_task: asyncio.Task[None] | None = None
        self._pending_agents: set[str] = set()
        self._sweep_event = asyncio.Event()
        self._next_expiry: float | None = None
        self._expiry_task: asyncio.Task[None] | None = None

        # Retention config
        self._inbox_stale_hours = inbox_stale_hours
        self._processed_retention_days = processed_retention_days
        self._outbox_retention_days = outbox_retention_days
        self._dead_letter_compact_min_files = dead_letter_compact_min_files
        self._dead_letter_compact_min_age_hours = dead_letter_compact_min_age_hours
        self._dead_letter_compact_delete_originals = dead_letter_compact_delete_originals
        self._dead_letter_archive_retention_days = dead_letter_archive_retention_days
        self._dead_letter_max_files_per_segment = dead_letter_max_files_per_segment

        # Lazy import to avoid circular deps at module level
        from nexus.contracts.cache_store import NullCacheStore

        self._null_cache_type = NullCacheStore

    def _ctx(self) -> Any:
        from nexus.contracts.types import OperationContext

        return OperationContext(user_id="system", groups=[], zone_id=self._zone_id, is_system=True)

    async def _file_mtime(self, path: str) -> "datetime | None":
        """Check metastore modified_at for a file."""
        try:
            meta = self._vfs.metadata.get(path)
            if meta is not None:
                return getattr(meta, "modified_at", None)
        except Exception:
            pass
        return None

    async def start(self) -> None:
        """Start the background sweep loop and optional pub/sub listener."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._sweep_loop())

        event_driven = self._cache_store is not None and not isinstance(
            self._cache_store, self._null_cache_type
        )
        if event_driven:
            self._sub_task = asyncio.create_task(self._subscribe_loop())

        retention_enabled = any(
            [
                self._inbox_stale_hours is not None,
                self._processed_retention_days is not None,
                self._outbox_retention_days is not None,
                self._dead_letter_compact_min_files is not None,
            ]
        )
        logger.info(
            "TTL sweeper started (poll_interval: %.0fs, event_driven: %s, debounce: %.1fs, retention: %s)",
            self._interval,
            event_driven,
            self._debounce_seconds,
            retention_enabled,
        )
        if retention_enabled:
            logger.warning(
                "[IPC] Retention features are enabled. These require server-observed file "
                "mtime via file_mtime(). When mtime is unavailable (e.g. non-local storage "
                "backends or metastore misses), retention silently safe-fails and no files "
                "are deleted or archived. Verify that file_mtime() returns non-None for your "
                "storage backend before relying on retention to control inbox/DLQ growth."
            )

    async def stop(self) -> None:
        """Stop the background sweep loop and pub/sub listener."""
        self._running = False

        if self._expiry_task is not None:
            self._expiry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._expiry_task
            self._expiry_task = None

        if self._sub_task is not None:
            self._sub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sub_task
            self._sub_task = None

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        logger.info("TTL sweeper stopped")

    async def sweep_once(self) -> int:
        """Run a single sweep cycle across all agent inboxes.

        Also runs retention maintenance: stale inbox drain, processed/outbox
        TTL delete, and dead_letter compaction.

        Returns:
            Number of TTL-expired messages moved to dead_letter.
        """
        expired_count = 0
        try:
            agent_ids = self._vfs.sys_readdir(AGENTS_ROOT, recursive=False, context=self._ctx())
        except Exception:
            logger.debug("Cannot list %s for sweep", AGENTS_ROOT)
            return 0

        for agent_id in agent_ids:
            expired_count += await self._sweep_agent(agent_id)
            await self._drain_stale_inbox(agent_id)
            await self._prune_dir(agent_id, PROCESSED_DIR, self._processed_retention_days)
            await self._prune_dir(agent_id, OUTBOX_DIR, self._outbox_retention_days)
            await self._compact_dead_letter(agent_id)

        if expired_count > 0:
            logger.info(
                "TTL sweep: moved %d expired messages to dead_letter",
                expired_count,
            )
        return expired_count

    # ------------------------------------------------------------------
    # Internal loops
    # ------------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        """Main sweep loop — combines event-driven wakeup with periodic fallback."""
        while self._running:
            try:
                try:
                    await asyncio.wait_for(
                        self._sweep_event.wait(),
                        timeout=self._interval,
                    )
                    self._sweep_event.clear()
                    agents_to_sweep = self._pending_agents.copy()
                    self._pending_agents.clear()
                    expired = 0
                    for agent_id in agents_to_sweep:
                        expired += await self._sweep_agent(agent_id, skip_recent=False)
                    if expired > 0:
                        logger.info(
                            "TTL event sweep: moved %d expired messages for %d agents",
                            expired,
                            len(agents_to_sweep),
                        )
                except TimeoutError:
                    await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("TTL sweep cycle failed", exc_info=True)

    async def _subscribe_loop(self) -> None:
        """Subscribe to CacheStore pub/sub for TTL schedule events."""
        if self._cache_store is None:
            return
        channel = f"ipc:ttl:schedule:{self._zone_id}"
        max_retries = 5
        consecutive_failures = 0
        while self._running:
            try:
                async with self._cache_store.subscribe(channel) as messages:
                    consecutive_failures = 0
                    async for msg in messages:
                        if not self._running:
                            return
                        try:
                            data = json.loads(msg)
                            agent_id = data.get("agent_id")
                            expires_at = data.get("expires_at")
                            if agent_id:
                                self._pending_agents.add(agent_id)
                                self._schedule_expiry_sweep(expires_at)
                        except Exception:
                            logger.debug("Invalid TTL schedule event", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= max_retries:
                    logger.error(
                        "TTL sweeper pub/sub listener failed %d times for zone %s, stopping",
                        max_retries,
                        self._zone_id,
                    )
                    return
                delay = min(2**consecutive_failures, 30)
                logger.warning(
                    "TTL sweeper pub/sub listener error for zone %s (attempt %d/%d), retrying in %ds",
                    self._zone_id,
                    consecutive_failures,
                    max_retries,
                    delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)

    def _schedule_expiry_sweep(self, expires_at: float | None) -> None:
        """Schedule a sweep at the message's expiry time."""
        import time

        now = time.time()

        if expires_at is None:
            expires_at = now + self._debounce_seconds

        if self._next_expiry is not None and expires_at >= self._next_expiry:
            return

        self._next_expiry = expires_at

        if self._expiry_task is not None:
            self._expiry_task.cancel()
        self._expiry_task = asyncio.create_task(self._wait_and_sweep(expires_at))

    async def _wait_and_sweep(self, expires_at: float) -> None:
        """Sleep until expiry time, then signal the sweep loop."""
        import time

        try:
            delay = max(0, expires_at - time.time() + 0.1)
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_expiry = None
            self._sweep_event.set()
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Per-agent TTL sweep (existing)
    # ------------------------------------------------------------------

    async def _sweep_agent(self, agent_id: str, *, skip_recent: bool = True) -> int:
        """Sweep a single agent's inbox for TTL-expired messages."""
        agent_inbox = inbox_path(agent_id)
        expired = 0

        try:
            filenames = self._vfs.sys_readdir(agent_inbox, recursive=False, context=self._ctx())
        except Exception:
            return 0

        now = datetime.now(UTC)
        for filename in filenames:
            if not filename.endswith(".json"):
                continue

            if skip_recent and self._is_recent_by_filename(filename, now):
                continue

            msg_path = f"{agent_inbox}/{filename}"
            try:
                data = self._vfs.sys_read(msg_path, context=self._ctx())
                envelope = MessageEnvelope.from_bytes(data)
                if envelope.is_expired():
                    await dead_letter_message(
                        self._vfs,
                        msg_path,
                        agent_id,
                        self._zone_id,
                        DLQReason.TTL_EXPIRED,
                        msg_id=envelope.id,
                        timestamp=envelope.timestamp,
                        detail=f"TTL {envelope.ttl_seconds}s expired (sweeper)",
                    )
                    expired += 1
            except Exception:
                logger.debug(
                    "Skipping unreadable file during sweep: %s",
                    msg_path,
                )

        return expired

    def _is_recent_by_filename(self, filename: str, now: datetime) -> bool:
        """Check if a message is too recent to be expired based on filename timestamp."""
        try:
            ts_str = filename.split("_", 1)[0]
            file_ts = datetime.strptime(ts_str, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            age_seconds = (now - file_ts).total_seconds()
            return age_seconds < self._interval
        except (ValueError, IndexError):
            return False

    # ------------------------------------------------------------------
    # Retention: stale inbox drain
    # ------------------------------------------------------------------

    async def _drain_stale_inbox(self, agent_id: str) -> int:
        """Dead-letter no-TTL inbox messages older than inbox_stale_hours.

        Provides a relief valve for dead consumers: a full inbox self-heals
        after the configured window without requiring the consumer to be alive.
        Only targets messages with no TTL — TTL messages are handled by
        _sweep_agent().

        **Race safety:** Each candidate file is atomically renamed to a
        ``{fn}.drain_{claim_ts}_{run_id}`` name before reading. If
        ``MessageProcessor`` already read the file and is executing its
        handler, the rename fails silently — the drain skips the message.
        If the drain claims the file first, the processor's ``sys_read``
        gets ``FileNotFoundError`` and skips gracefully (delivery.py:536).

        **Crash recovery:** orphaned ``.drain_*`` files (sweeper crashed
        after claiming but before dead-lettering) are restored to their
        original names at the start of each drain cycle so they can be
        re-evaluated on the next sweep.
        """
        if self._inbox_stale_hours is None:
            return 0

        agent_inbox = inbox_path(agent_id)
        cutoff = datetime.now(UTC) - timedelta(hours=self._inbox_stale_hours)
        run_id = uuid.uuid4().hex[:8]
        drained = 0

        try:
            filenames = self._vfs.sys_readdir(agent_inbox, recursive=False, context=self._ctx())
        except Exception:
            return 0

        # Crash recovery: restore orphaned .drain_* files from previous cycles.
        # These are stale when their embedded claim_ts is older than _CLAIMED_STALE_MINUTES.
        await self._recover_drain_claims(agent_inbox, filenames)

        for filename in filenames:
            if not filename.endswith(".json"):
                continue
            msg_path = f"{agent_inbox}/{filename}"
            if not await self._file_is_older_than(msg_path, filename, cutoff):
                continue

            # Atomic claim: rename before reading so the processor can't race us.
            claim_ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            claimed_path = f"{agent_inbox}/{filename}.drain_{claim_ts}_{run_id}"
            try:
                self._vfs.sys_rename(msg_path, claimed_path, context=self._ctx())
            except Exception:
                continue  # processor or concurrent sweeper already moved it — skip

            try:
                data = self._vfs.sys_read(claimed_path, context=self._ctx())
                envelope = MessageEnvelope.from_bytes(data)
                if envelope.ttl_seconds is not None:
                    # Has TTL — restore and let _sweep_agent handle it
                    await self._maybe_rename_to_orig(claimed_path, msg_path)
                    continue
                # dead_letter_message renames claimed_path to its canonical DLQ path
                await dead_letter_message(
                    self._vfs,
                    claimed_path,
                    agent_id,
                    self._zone_id,
                    DLQReason.STALE_INBOX,
                    msg_id=envelope.id,
                    timestamp=envelope.timestamp,
                    detail=f"No consumer for >{self._inbox_stale_hours}h",
                )
                drained += 1
            except Exception:
                await self._maybe_rename_to_orig(claimed_path, msg_path)
                logger.debug("Skipping unreadable file during stale drain: %s", filename)

        if drained > 0:
            logger.info(
                "Stale inbox drain: moved %d messages for agent %s",
                drained,
                agent_id,
            )
        return drained

    async def _recover_drain_claims(self, inbox_dir: str, filenames: list[str]) -> None:
        """Restore orphaned ``.drain_{claim_ts}_{run_id}`` files from crashed drain cycles.

        Uses the server-written ``claim_ts`` in the filename to determine staleness —
        no mtime needed, backend-agnostic.
        """
        stale_cutoff = datetime.now(UTC) - timedelta(minutes=_CLAIMED_STALE_MINUTES)
        for fn in filenames:
            if ".drain_" not in fn:
                continue
            try:
                drain_suffix = fn.split(".drain_", 1)[1]  # "{claim_ts}_{run_id}"
                claim_ts_str = drain_suffix.split("_", 1)[0]
                claim_dt = datetime.strptime(claim_ts_str, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            except (ValueError, IndexError):
                logger.debug("Cannot parse drain claim time from %s — skipping", fn)
                continue
            if claim_dt >= stale_cutoff:
                continue  # recently claimed — active drain cycle
            orig_fn = fn.split(".drain_")[0]
            await self._maybe_rename_to_orig(f"{inbox_dir}/{fn}", f"{inbox_dir}/{orig_fn}")
            logger.info("Restored orphaned drain claim: %s → %s", fn, orig_fn)

    # ------------------------------------------------------------------
    # Retention: processed/ and outbox/ TTL delete
    # ------------------------------------------------------------------

    async def _prune_dir(self, agent_id: str, dir_name: str, retention_days: int | None) -> int:
        """Delete files older than retention_days from a directory.

        Used for processed/ and outbox/ cleanup. Skips the _archive
        subdirectory and non-.json entries.
        """
        if retention_days is None:
            return 0

        from nexus.bricks.ipc.conventions import agent_dir

        dir_path = f"{agent_dir(agent_id)}/{dir_name}"
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        deleted = 0

        try:
            filenames = self._vfs.sys_readdir(dir_path, recursive=False, context=self._ctx())
        except Exception:
            return 0

        for filename in filenames:
            if not filename.endswith(".json"):
                continue  # skip _archive dirs, .jsonl, and other non-message files
            file_path = f"{dir_path}/{filename}"
            if not await self._file_is_older_than(file_path, filename, cutoff):
                continue
            try:
                self._vfs.sys_unlink(file_path, context=self._ctx())
                deleted += 1
            except Exception:
                logger.debug("Failed to delete aged file %s", file_path)

        return deleted

    # ------------------------------------------------------------------
    # Retention: dead_letter/ two-phase JSONL compaction
    # ------------------------------------------------------------------

    async def _compact_dead_letter(self, agent_id: str) -> int:
        """Archive aged dead_letter files into per-day JSONL segments.

        Two-phase commit for crash safety:
          1. Collect aged files, build JSONL in memory, write ``.jsonl.tmp``
          2. Write committed ``.jsonl`` (archive is durable after this)
          3. Delete original files — ONLY after step 2 succeeds
          4. Delete ``.tmp`` (best-effort cleanup)

        On the next sweep after a crash, orphaned ``.tmp`` files are cleaned up
        by ``_recover_archive_tmp()`` before any new compaction runs.

        Only compacts day-buckets with >= dead_letter_compact_min_files aged files,
        keeping partial/recent days untouched.
        """
        if self._dead_letter_compact_min_files is None:
            return 0

        dl_path = dead_letter_path(agent_id)
        archive_dir = dead_letter_archive_path(agent_id)

        # Crash recovery: clean orphaned archive temps and restore claimed files
        await self._recover_archive_tmp(archive_dir)
        await self._recover_claimed_files(dl_path, archive_dir)

        # Prune old archive segments
        await self._prune_archives(archive_dir, self._dead_letter_archive_retention_days)

        cutoff = datetime.now(UTC) - timedelta(hours=self._dead_letter_compact_min_age_hours)

        try:
            filenames = self._vfs.sys_readdir(dl_path, recursive=False, context=self._ctx())
        except Exception:
            return 0

        # Collect message files: .json but not .reason.json, not _archive, not
        # .arch_* (actively claimed), not .archived (already compacted in
        # preserve-originals mode — idempotency marker).
        msg_files = []
        for fn in filenames:
            if not fn.endswith(".json"):
                continue
            if fn.endswith(".reason.json"):
                continue
            if ".arch_" in fn:
                continue
            if self._vfs.access(f"{dl_path}/{fn}.archived", context=self._ctx()):
                continue  # already archived in a previous preserve-originals sweep
            msg_files.append(fn)
        msg_files.sort()

        if len(msg_files) < self._dead_letter_compact_min_files:
            return 0

        # Group by day prefix (first 8 chars: YYYYMMDD)
        by_day: dict[str, list[str]] = {}
        for fn in msg_files:
            day = fn[:8]
            if len(day) == 8 and day.isdigit():
                by_day.setdefault(day, []).append(fn)

        total_archived = 0
        for day, day_files in by_day.items():
            # Only compact files old enough by server-observed write time
            aged: list[str] = []
            for fn in day_files:
                if await self._file_is_older_than(f"{dl_path}/{fn}", fn, cutoff):
                    aged.append(fn)
            if len(aged) < self._dead_letter_compact_min_files:
                continue
            # Cap per-segment file count to bound memory usage.
            # Remaining files are picked up in the next sweep cycle.
            if len(aged) > self._dead_letter_max_files_per_segment:
                aged = aged[: self._dead_letter_max_files_per_segment]
            total_archived += await self._write_archive_segment(
                agent_id,
                dl_path,
                archive_dir,
                day,
                aged,
                delete_originals=self._dead_letter_compact_delete_originals,
            )

        return total_archived

    async def _write_archive_segment(
        self,
        agent_id: str,
        dl_path: str,
        archive_dir: str,
        day: str,
        filenames: list[str],
        *,
        delete_originals: bool = False,
    ) -> int:
        """Write one JSONL archive segment using atomic per-file claiming.

        Claim protocol: each source file is renamed to ``{fn}.arch_{run_id}``
        before reading. Only the sweeper that successfully renames a file reads
        and archives it. A concurrent sweeper claiming the same file gets
        FileNotFoundError on the rename and skips it — no duplicate entries.

        Crash safety:
          - If the sweeper crashes after claiming but before committing, orphaned
            ``.arch_*`` files are restored by ``_recover_claimed_files()`` on the
            next sweep cycle (after ``_CLAIMED_STALE_MINUTES`` have elapsed).
          - If the sweeper crashes after committing but before deleting claimed
            files, the next sweep will see no candidates (they're claimed) and
            recovery renames them back, then the following cycle re-compacts them.

        .. warning::
            Archived data in ``_archive/*.jsonl`` has no automated reader or
            replay path. Enable compaction only after evaluating this trade-off.

        Returns number of messages archived.
        """
        now_ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        run_id = uuid.uuid4().hex[:8]
        archive_tmp = dead_letter_archive_tmp(agent_id, day, f"{now_ts}_{run_id}")
        archive_final = dead_letter_archive_segment(agent_id, day, f"{now_ts}_{run_id}")

        with contextlib.suppress(Exception):
            self._vfs.mkdir(archive_dir, parents=True, exist_ok=True, context=self._ctx())

        if delete_originals:
            # Phase 0: atomically claim each candidate via rename so concurrent
            # sweepers cannot double-archive. Claim name: "{fn}.arch_{claim_ts}_{run_id}".
            claim_ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            claims: list[tuple[str, str, str]] = []
            for fn in filenames:
                orig = f"{dl_path}/{fn}"
                claimed = f"{dl_path}/{fn}.arch_{claim_ts}_{run_id}"
                try:
                    self._vfs.sys_rename(orig, claimed, context=self._ctx())
                    claims.append((fn, orig, claimed))
                except Exception:
                    pass  # concurrent sweeper or already deleted
            if not claims:
                return 0
        else:
            # Preserve-originals mode: read directly without claiming.
            # Concurrent sweepers may produce duplicate archives (acceptable trade-off;
            # originals are not touched so no data is lost).
            claims = [(fn, f"{dl_path}/{fn}", f"{dl_path}/{fn}") for fn in filenames]

        # Build JSONL buffer. When deleting, read from claimed path; when preserving,
        # read_path == orig_path.
        lines: list[bytes] = []
        good_claims: list[tuple[str, str, str]] = []
        bad_claims: list[tuple[str, str, str]] = []

        for fn, orig, read_path in claims:
            reason_path = f"{dl_path}/{fn}.reason.json"
            try:
                data = self._vfs.sys_read(read_path, context=self._ctx())
                reason_raw = b"{}"
                if self._vfs.access(reason_path, context=self._ctx()):
                    reason_raw = self._vfs.sys_read(reason_path, context=self._ctx())
                record = json.dumps(
                    {
                        "file": fn,
                        "envelope": json.loads(data),
                        "reason": json.loads(reason_raw),
                    },
                    separators=(",", ":"),
                )
                lines.append(record.encode() + b"\n")
                good_claims.append((fn, orig, read_path))
            except Exception:
                logger.debug("Skipping unreadable file during compaction: %s", fn)
                bad_claims.append((fn, orig, read_path))

        if delete_originals:
            for _fn, orig, claimed in bad_claims:
                await self._maybe_rename_to_orig(claimed, orig)

        if not good_claims:
            return 0

        archive_bytes = b"".join(lines)

        # Phase 1: write .tmp
        try:
            self._vfs.write(archive_tmp, archive_bytes, context=self._ctx())
        except Exception:
            logger.warning("Failed to write archive tmp %s", archive_tmp)
            if delete_originals:
                for _fn, orig, claimed in good_claims:
                    await self._maybe_rename_to_orig(claimed, orig)
            return 0

        # Phase 2: write final (archive is now durable)
        try:
            self._vfs.write(archive_final, archive_bytes, context=self._ctx())
        except Exception:
            logger.warning("Failed to commit archive %s", archive_final)
            if delete_originals:
                for _fn, orig, claimed in good_claims:
                    await self._maybe_rename_to_orig(claimed, orig)
            await self._maybe_unlink(archive_tmp)
            return 0

        # Phase 3: delete claimed files and reason sidecars (only when deleting originals),
        # OR write .archived marker per source file (preserve-originals idempotency).
        if delete_originals:
            for fn, _orig, claimed in good_claims:
                await self._maybe_unlink(claimed)
                await self._maybe_unlink(f"{dl_path}/{fn}.reason.json")
        else:
            # Mark each source file so subsequent sweeps skip it — prevents
            # re-archiving the same messages on every poll cycle.
            for fn, orig, _ in good_claims:
                try:
                    self._vfs.write(f"{orig}.archived", b"", context=self._ctx())
                except Exception:
                    logger.debug("Failed to write .archived marker for %s", fn)

        # Phase 4: delete .tmp (best-effort cleanup)
        await self._maybe_unlink(archive_tmp)

        logger.info(
            "Compacted %d dead_letter messages for agent %s (day=%s → %s, delete_originals=%s)",
            len(good_claims),
            agent_id,
            day,
            archive_final,
            delete_originals,
        )
        return len(good_claims)

    async def _recover_claimed_files(self, dl_path: str, archive_dir: str) -> None:
        """Recover stale ``.arch_{claim_ts}_{run_id}`` claimed files after crashed compaction.

        Staleness is determined by parsing ``claim_ts`` from the filename itself —
        a server-generated timestamp written at claim time. This is backend-agnostic
        and does not depend on ``file_mtime()``, which may be unavailable on
        non-local backends.

        Decision logic per stale claim:
          - If a committed archive ending in ``_{run_id}.jsonl`` exists →
            the archive is durable; **delete** the claimed file (don't re-archive).
          - If no such archive exists → the commit never happened; **restore**
            the claimed file so the next sweep can re-archive it.
        """
        stale_cutoff = datetime.now(UTC) - timedelta(minutes=_CLAIMED_STALE_MINUTES)
        try:
            filenames = self._vfs.sys_readdir(dl_path, recursive=False, context=self._ctx())
        except Exception:
            return

        # Pre-load archive listing for run_id lookup (best-effort)
        archive_files: set[str] = set()
        with contextlib.suppress(Exception):
            archive_files = set(
                self._vfs.sys_readdir(archive_dir, recursive=False, context=self._ctx())
            )

        for fn in filenames:
            if ".arch_" not in fn:
                continue
            # Parse server-written claim_ts from "{orig}.arch_{claim_ts}_{run_id}"
            try:
                arch_suffix = fn.split(".arch_", 1)[1]  # "{claim_ts}_{run_id}"
                claim_ts_str, run_id = arch_suffix.split("_", 1)
                claim_dt = datetime.strptime(claim_ts_str, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            except (ValueError, IndexError):
                logger.debug("Cannot parse claim time from %s — skipping recovery", fn)
                continue

            if claim_dt >= stale_cutoff:
                continue  # recently claimed — active sweeper, leave it alone

            claimed_path = f"{dl_path}/{fn}"
            archive_committed = any(af.endswith(f"_{run_id}.jsonl") for af in archive_files)

            if archive_committed:
                # Archive is durable — delete the stale claim (already captured)
                await self._maybe_unlink(claimed_path)
                logger.info("Deleted post-commit stale claim (already in archive): %s", fn)
            else:
                # No committed archive — restore so next sweep can compact it
                orig_fn = fn.split(".arch_")[0]
                orig_path = f"{dl_path}/{orig_fn}"
                await self._maybe_rename_to_orig(claimed_path, orig_path)
                logger.info("Restored orphaned claimed file: %s → %s", fn, orig_fn)

    async def _recover_archive_tmp(self, archive_dir: str) -> None:
        """Clean up orphaned .jsonl.tmp files from previously failed compactions.

        Cases:
          - ``.tmp`` exists + matching ``.jsonl`` exists → crash in phase 4
            (originals already deleted, archive committed). Delete ``.tmp``.
          - ``.tmp`` exists + no matching ``.jsonl`` → crash in phase 1/2
            (originals still intact). Delete ``.tmp``; next sweep re-compacts.
        """
        try:
            filenames = self._vfs.sys_readdir(archive_dir, recursive=False, context=self._ctx())
        except Exception:
            return

        for fn in filenames:
            if not fn.endswith(".jsonl.tmp"):
                continue
            tmp_path = f"{archive_dir}/{fn}"
            await self._maybe_unlink(tmp_path)

    async def _prune_archives(self, archive_dir: str, retention_days: int | None) -> None:
        """Delete archive segment files older than retention_days.

        Uses the file's server-observed mtime (via ``_archive_file_is_older_than``)
        so that a freshly created archive for an old DLQ bucket is not immediately
        pruned because the source-message day is old.
        """
        if retention_days is None:
            return

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)

        try:
            filenames = self._vfs.sys_readdir(archive_dir, recursive=False, context=self._ctx())
        except Exception:
            return

        for fn in filenames:
            if not fn.endswith(".jsonl"):
                continue
            file_path = f"{archive_dir}/{fn}"
            if await self._archive_file_is_older_than(file_path, fn, cutoff):
                await self._maybe_unlink(file_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _archive_file_is_older_than(self, path: str, fn: str, cutoff: datetime) -> bool:
        """Check archive segment age using mtime (preferred) or creation timestamp.

        Archive filename format: ``{day}_{created_ts}_{run_id}.jsonl``

        Mtime is the server-observed write time (set when the archive was committed).
        Fallback parses the creation timestamp from the second ``_``-delimited field
        rather than the day prefix, which encodes source-message date not archive age.
        """
        mtime = await self._file_mtime(path)
        if mtime is not None:
            return mtime < cutoff
        # Fallback: parse creation timestamp from second _-delimited field
        # e.g. "20200101_20260404T123456_abc12345.jsonl" → "20260404T123456"
        try:
            parts = fn.split("_")
            if len(parts) >= 2:
                file_dt = datetime.strptime(parts[1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
                return file_dt < cutoff
        except (ValueError, IndexError):
            pass
        return False  # safe default: do not prune when age is unknown

    async def _file_is_older_than(self, path: str, _filename: str, cutoff: datetime) -> bool:
        """Return True if the file is older than cutoff, based on server-observed mtime.

        **Never falls back to filename timestamps for retention decisions.**
        Filename timestamps are sender-controlled (derived from ``envelope.timestamp``)
        and cannot be trusted for destructive operations. When ``_file_mtime()``
        returns ``None`` (e.g. metastore miss on ``/agents`` mounts), this method
        returns ``False`` — safe fail: no retention action is taken.

        This means retention is silently disabled for a file when its mtime cannot
        be resolved. Operators should check VFS metadata configuration if retention
        stops working unexpectedly.
        """
        mtime = await self._file_mtime(path)
        if mtime is None:
            logger.debug(
                "file_mtime unavailable for %s — skipping retention check (safe fail)",
                path,
            )
            return False
        return mtime < cutoff

    def _is_older_than(self, filename: str, cutoff: datetime) -> bool:
        """Return True if the filename's timestamp prefix is before cutoff.

        Only used as a fallback when file_mtime() returns None. Prefer
        _file_is_older_than() for retention decisions.
        """
        try:
            ts_str = filename.split("_", 1)[0]
            file_ts = datetime.strptime(ts_str, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            return file_ts < cutoff
        except (ValueError, IndexError):
            return False

    async def _maybe_rename_to_orig(self, claimed_path: str, orig_path: str) -> None:
        """Best-effort: rename a claimed file back to its original path.

        Only renames if the original doesn't already exist, to avoid
        overwriting a concurrently restored copy.
        """
        try:
            if not self._vfs.access(orig_path, context=self._ctx()):
                self._vfs.sys_rename(claimed_path, orig_path, context=self._ctx())
        except Exception:
            pass

    async def _maybe_unlink(self, path: str) -> None:
        """Delete a file, silently ignoring errors."""
        with contextlib.suppress(Exception):
            self._vfs.sys_unlink(path, context=self._ctx())
