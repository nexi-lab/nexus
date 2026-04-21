"""Durable cross-zone invalidation stream via Redis Streams.

Replaces fire-and-forget Pub/Sub with guaranteed delivery using
Redis/Dragonfly Streams (XADD/XREADGROUP/XACK).  Provides:

- Persistent offsets via consumer groups (catch-up on zone restart)
- Acknowledgment per message (XACK)
- Bounded stream size (MAXLEN ~ 100000)
- Pipelined XADD for invalidation storms (batch up to 100)
- Concurrent consumer with asyncio.Semaphore

Architecture:
    Publisher side (sync):
        invalidate_for_write() → queue.put_nowait() → background drain → XADD pipeline
    Consumer side (async):
        XREADGROUP BLOCK → process batch → XACK → advance read fence watermark

The sync publish preserves the synchronous invalidate_for_write() contract.
The small window between queue-append and XADD is covered by the read fence
(cached results are compared against the watermark, not the stream directly).

Related: Issue #3396 (decisions 1A, 6A, 14A, 15A, 16A)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from typing import TYPE_CHECKING, Any

from nexus.bricks.rebac.cache.channel_codec import encode_channel
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.bricks.rebac.cache.read_fence import ReadFence

logger = logging.getLogger(__name__)

# Defaults
_STREAM_PREFIX = "rebac:durable"
_MAX_STREAM_LEN = 100_000  # MAXLEN ~ for XADD
_DRAIN_BATCH_SIZE = 100  # Pipeline up to N events per drain cycle
_CONSUMER_BATCH_SIZE = 10  # XREADGROUP COUNT per call
_CONSUMER_BLOCK_MS = 2000  # XREADGROUP BLOCK timeout
_CONSUMER_CONCURRENCY = 10  # Max concurrent event processing
_QUEUE_MAXSIZE = 10_000  # Bounded in-process publish queue
_DRAIN_INTERVAL_S = 0.05  # 50ms drain loop interval (idle)
_MAX_DELIVERY_ATTEMPTS = 5  # Max retries before DLQ
_CLAIM_MIN_IDLE_MS = 30_000  # XCLAIM: reclaim messages idle > 30s
_DLQ_SUFFIX = ":dlq"  # Dead-letter queue stream suffix


class DurableInvalidationStream:
    """Cross-zone durable invalidation via Redis Streams.

    Publisher API is synchronous (queue.put_nowait) to preserve the
    sync invalidate_for_write() contract.  A background asyncio task
    drains the queue and calls XADD with pipeline batching.

    Consumer API is async — reads from a consumer group with XREADGROUP
    BLOCK, processes events concurrently, and XACKs on completion.

    Example::

        stream = DurableInvalidationStream(
            redis_client=async_redis,
            zone_id="us-east-1",
            read_fence=fence,
        )
        await stream.start()

        # Publish (sync, from invalidate_for_write):
        stream.publish("zone-b", {"subject_type": "user", ...})

        # Consumer runs in background, updating read_fence watermarks.
        await stream.stop()
    """

    def __init__(
        self,
        redis_client: Any = None,
        *,
        zone_id: str = ROOT_ZONE_ID,
        read_fence: ReadFence | None = None,
        stream_prefix: str = _STREAM_PREFIX,
        max_stream_len: int = _MAX_STREAM_LEN,
        drain_batch_size: int = _DRAIN_BATCH_SIZE,
        consumer_batch_size: int = _CONSUMER_BATCH_SIZE,
        consumer_block_ms: int = _CONSUMER_BLOCK_MS,
        consumer_concurrency: int = _CONSUMER_CONCURRENCY,
        queue_maxsize: int = _QUEUE_MAXSIZE,
    ) -> None:
        self._client = redis_client
        self._zone_id = zone_id
        self._read_fence = read_fence
        self._stream_prefix = stream_prefix
        self._max_stream_len = max_stream_len
        self._drain_batch_size = drain_batch_size
        self._consumer_batch_size = consumer_batch_size
        self._consumer_block_ms = consumer_block_ms
        self._consumer_concurrency = consumer_concurrency

        self._enabled = redis_client is not None

        # Sync publish queue — bounded to prevent OOM if drain stalls
        self._queue: deque[tuple[str, dict[str, Any]]] = deque(maxlen=queue_maxsize)

        # Background tasks
        self._drain_task: asyncio.Task[None] | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._closed = False

        # Consumer group name = zone_id, consumer name = zone_id:consumer
        self._group_name = f"zone:{zone_id}"
        self._consumer_name = f"{zone_id}:consumer"

        # Event handlers: list of async callables
        self._handlers: list[tuple[str, Any]] = []

        # Metrics
        self._published = 0
        self._drained = 0
        self._drain_errors = 0
        self._consumed = 0
        self._consume_errors = 0
        self._queue_drops = 0

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Start background drain and consumer tasks."""
        if not self._enabled:
            return

        # Ensure consumer group exists for all subscribed streams
        await self._ensure_consumer_groups()

        self._drain_task = asyncio.create_task(self._drain_loop(), name="durable-stream-drain")
        self._consumer_task = asyncio.create_task(
            self._consume_loop(), name="durable-stream-consumer"
        )
        logger.info(
            "[DurableStream] Started for zone %s (group=%s)",
            self._zone_id,
            self._group_name,
        )

    async def stop(self) -> None:
        """Stop background tasks. Idempotent."""
        self._closed = True
        for task in (self._drain_task, self._consumer_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        # Final drain of remaining queued events
        if self._enabled:
            await self._drain_batch()
        logger.info("[DurableStream] Stopped for zone %s", self._zone_id)

    # -- publish (sync) -------------------------------------------------------

    def publish(self, target_zone_id: str, payload: dict[str, Any]) -> bool:
        """Enqueue an invalidation event for durable delivery.

        Synchronous — appends to an in-process deque. The background
        drain task sends to Redis Streams asynchronously.

        Args:
            target_zone_id: Zone the invalidation targets.
            payload: Invalidation details (subject, relation, object, etc.)

        Returns:
            True if enqueued, False if queue is full (event dropped).
        """
        if not self._enabled:
            return False

        if len(self._queue) >= (self._queue.maxlen or _QUEUE_MAXSIZE):
            self._queue_drops += 1
            logger.warning(
                "[DurableStream] Queue full, dropping event for zone %s",
                target_zone_id,
            )
            return False

        self._queue.append((target_zone_id, payload))
        self._published += 1
        return True

    # -- handler registration -------------------------------------------------

    def register_handler(
        self,
        handler_id: str,
        handler: Any,  # async callable(zone_id: str, payload: dict) -> None
    ) -> None:
        """Register an async handler for incoming durable stream events."""
        for hid, _ in self._handlers:
            if hid == handler_id:
                return
        self._handlers.append((handler_id, handler))

    def unregister_handler(self, handler_id: str) -> bool:
        """Unregister a handler."""
        for i, (hid, _) in enumerate(self._handlers):
            if hid == handler_id:
                self._handlers.pop(i)
                return True
        return False

    # -- drain loop (async background) ----------------------------------------

    async def _drain_loop(self) -> None:
        """Background task: drain the publish queue to Redis Streams."""
        while not self._closed:
            try:
                if self._queue:
                    await self._drain_batch()
                else:
                    await asyncio.sleep(_DRAIN_INTERVAL_S)
            except asyncio.CancelledError:
                return
            except Exception:
                self._drain_errors += 1
                logger.warning("[DurableStream] Drain error", exc_info=True)
                await asyncio.sleep(_DRAIN_INTERVAL_S)

    async def _drain_batch(self) -> None:
        """Drain up to drain_batch_size events using a Redis pipeline."""
        if not self._queue:
            return

        batch: list[tuple[str, dict[str, Any]]] = []
        while self._queue and len(batch) < self._drain_batch_size:
            batch.append(self._queue.popleft())

        if not batch:
            return

        try:
            pipe = self._client.pipeline(transaction=False)
            for target_zone_id, payload in batch:
                stream_key = encode_channel(self._stream_prefix, target_zone_id, "all")
                pipe.xadd(
                    stream_key,
                    {"data": json.dumps(payload)},
                    maxlen=self._max_stream_len,
                    approximate=True,
                )
            await pipe.execute()
            self._drained += len(batch)
        except Exception:
            # Put failed events back at the front of the queue
            for item in reversed(batch):
                self._queue.appendleft(item)
            self._drain_errors += 1
            raise

    # -- consumer loop (async background) -------------------------------------

    async def _consume_loop(self) -> None:
        """Background task: read from Redis Streams and process events.

        Periodically reclaims failed messages from the PEL (every
        _RECLAIM_INTERVAL iterations) so transient handler failures
        are retried instead of stranded forever.
        """
        sem = asyncio.Semaphore(self._consumer_concurrency)
        iterations_since_reclaim = 0
        _RECLAIM_EVERY = 30  # Reclaim PEL every ~30 read cycles (~60s at 2s block)

        while not self._closed:
            try:
                # Periodically reclaim stranded messages from PEL
                iterations_since_reclaim += 1
                if iterations_since_reclaim >= _RECLAIM_EVERY:
                    iterations_since_reclaim = 0
                    try:
                        await self.reclaim_pending()
                    except Exception:
                        logger.debug("[DurableStream] Reclaim cycle failed", exc_info=True)

                stream_key = encode_channel(self._stream_prefix, self._zone_id, "all")
                # XREADGROUP GROUP <group> <consumer> COUNT <n> BLOCK <ms> STREAMS <key> >
                results = await self._client.xreadgroup(
                    groupname=self._group_name,
                    consumername=self._consumer_name,
                    streams={stream_key: ">"},
                    count=self._consumer_batch_size,
                    block=self._consumer_block_ms,
                )

                if not results:
                    continue

                # results: list of [stream_key, [(msg_id, fields), ...]]
                tasks = []
                for _stream, messages in results:
                    for msg_id, fields in messages:
                        tasks.append(self._process_message(sem, stream_key, msg_id, fields))

                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            except asyncio.CancelledError:
                return
            except Exception:
                self._consume_errors += 1
                logger.warning("[DurableStream] Consumer error", exc_info=True)
                await asyncio.sleep(1.0)

    async def _process_message(
        self,
        sem: asyncio.Semaphore,
        stream_key: str,
        msg_id: bytes | str,
        fields: dict[bytes | str, bytes | str],
    ) -> None:
        """Process a single message with concurrency control."""
        async with sem:
            try:
                raw = fields.get(b"data") or fields.get("data")
                if raw is None:
                    logger.warning("[DurableStream] Message %s has no data field", msg_id)
                    await self._client.xack(stream_key, self._group_name, msg_id)
                    return

                payload = json.loads(raw if isinstance(raw, str) else raw.decode())

                # Extract source zone from payload or stream key
                source_zone = payload.get("source_zone", self._zone_id)

                # Invoke handlers — all must succeed before ACK.
                # If any handler fails, the message stays in the PEL and will
                # be redelivered by reclaim_pending() (DLQ after max retries).
                handler_failed = False
                for handler_id, handler in self._handlers:
                    try:
                        await handler(source_zone, payload)
                    except Exception:
                        self._consume_errors += 1
                        handler_failed = True
                        logger.warning(
                            "[DurableStream] Handler %s failed for %s — not ACKing",
                            handler_id,
                            msg_id,
                            exc_info=True,
                        )
                        break  # Don't run remaining handlers on partial failure

                if handler_failed:
                    # Leave in PEL for redelivery via reclaim_pending()
                    return

                # All handlers succeeded — ACK and advance fence
                await self._client.xack(stream_key, self._group_name, msg_id)
                self._consumed += 1

                # Advance read fence generation — signals to the L1 cache that
                # any entries stamped before this generation are stale.
                if self._read_fence:
                    self._read_fence.advance(source_zone)

            except Exception:
                self._consume_errors += 1
                logger.warning("[DurableStream] Failed to process %s", msg_id, exc_info=True)

    # -- helpers --------------------------------------------------------------

    async def _ensure_consumer_groups(self) -> None:
        """Create consumer groups if they don't exist."""
        stream_key = encode_channel(self._stream_prefix, self._zone_id, "all")
        try:
            # XGROUP CREATE <key> <group> <id> MKSTREAM
            await self._client.xgroup_create(
                stream_key,
                self._group_name,
                id="0",
                mkstream=True,
            )
        except Exception as e:
            # BUSYGROUP = group already exists (expected on restart)
            if "BUSYGROUP" in str(e):
                logger.debug(
                    "[DurableStream] Consumer group %s already exists",
                    self._group_name,
                )
            else:
                raise

    # -- dead-letter queue (Issue #3396) --------------------------------------

    async def reclaim_pending(self) -> int:
        """Reclaim and retry messages stuck in the Pending Entries List.

        Uses XAUTOCLAIM to reclaim messages idle longer than CLAIM_MIN_IDLE_MS,
        then reprocesses them. Messages that exceed MAX_DELIVERY_ATTEMPTS are
        moved to the dead-letter queue stream.

        Returns:
            Number of messages reclaimed and reprocessed.
        """
        if not self._enabled:
            return 0

        stream_key = encode_channel(self._stream_prefix, self._zone_id, "all")
        reclaimed = 0

        try:
            # XAUTOCLAIM: reclaim idle messages from the consumer group
            # Returns: [new_start_id, [(msg_id, fields), ...], [deleted_ids]]
            result = await self._client.xautoclaim(
                stream_key,
                self._group_name,
                self._consumer_name,
                min_idle_time=_CLAIM_MIN_IDLE_MS,
                start_id="0-0",
                count=50,
            )

            if not result or len(result) < 2:
                return 0

            messages = result[1]
            sem = asyncio.Semaphore(self._consumer_concurrency)

            for msg_id, fields in messages:
                # Check delivery count via XPENDING (approximation: check if
                # the message has been delivered too many times)
                try:
                    pending_info = await self._client.xpending_range(
                        stream_key,
                        self._group_name,
                        min=msg_id,
                        max=msg_id,
                        count=1,
                    )
                    delivery_count = 1
                    if pending_info:
                        entry = pending_info[0]
                        # entry format varies by client, but typically includes delivery count
                        if isinstance(entry, dict):
                            delivery_count = int(entry.get("times_delivered", 1))
                        elif isinstance(entry, (list, tuple)) and len(entry) >= 4:
                            delivery_count = int(entry[3])
                except Exception:
                    delivery_count = 1

                if delivery_count >= _MAX_DELIVERY_ATTEMPTS:
                    # Move to DLQ
                    await self._move_to_dlq(stream_key, msg_id, fields)
                    await self._client.xack(stream_key, self._group_name, msg_id)
                    logger.warning(
                        "[DurableStream] Message %s moved to DLQ after %d attempts",
                        msg_id,
                        delivery_count,
                    )
                else:
                    # Retry processing
                    await self._process_message(sem, stream_key, msg_id, fields)
                    reclaimed += 1

        except Exception:
            logger.warning("[DurableStream] Reclaim failed", exc_info=True)

        return reclaimed

    async def _move_to_dlq(
        self,
        stream_key: str,
        msg_id: bytes | str,
        fields: dict[bytes | str, bytes | str],
    ) -> None:
        """Move a failed message to the dead-letter queue stream."""
        dlq_key = stream_key + _DLQ_SUFFIX
        try:
            # Preserve original message ID and add metadata
            dlq_fields = dict(fields)
            id_str = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
            dlq_fields[b"original_id" if isinstance(msg_id, bytes) else "original_id"] = id_str
            await self._client.xadd(
                dlq_key,
                dlq_fields,
                maxlen=1000,  # Keep last 1000 DLQ entries
                approximate=True,
            )
        except Exception:
            logger.warning("[DurableStream] Failed to write to DLQ %s", dlq_key, exc_info=True)

    # -- diagnostics ----------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return operational metrics."""
        return {
            "enabled": self._enabled,
            "zone_id": self._zone_id,
            "published": self._published,
            "drained": self._drained,
            "drain_errors": self._drain_errors,
            "consumed": self._consumed,
            "consume_errors": self._consume_errors,
            "queue_drops": self._queue_drops,
            "queue_size": len(self._queue),
            "handler_count": len(self._handlers),
        }

    async def health_check(self) -> dict[str, Any]:
        """Health check including PEL monitoring (Issue #3396 decision 15A).

        Queries XINFO GROUPS to detect stuck consumers with growing
        Pending Entries List (PEL). A PEL > 1000 indicates the consumer
        is falling behind.

        Returns:
            Health status dict with PEL size and consumer lag.
        """
        if not self._enabled:
            return {"status": "disabled"}

        try:
            stream_key = encode_channel(self._stream_prefix, self._zone_id, "all")
            groups = await self._client.xinfo_groups(stream_key)

            pel_size = 0
            for group in groups:
                # group is a dict or list depending on Redis client version
                if isinstance(group, dict):
                    name = (
                        group.get("name", b"").decode()
                        if isinstance(group.get("name"), bytes)
                        else group.get("name", "")
                    )
                    if name == self._group_name:
                        pel_size = int(group.get("pending", 0))
                        break

            status = "healthy"
            if pel_size > 1000:
                status = "degraded"
                logger.warning(
                    "[DurableStream] PEL size %d for group %s — consumer falling behind",
                    pel_size,
                    self._group_name,
                )

            return {
                "status": status,
                "pel_size": pel_size,
                "group": self._group_name,
                "consumed": self._consumed,
                "consume_errors": self._consume_errors,
                "drain_errors": self._drain_errors,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}
