"""WALEventLog — Rust-backed EventLogProtocol implementation.

Delegates to the `_nexus_wal.PyWAL` Rust extension for sub-5μs
durable event writes. Python handles serialization (orjson) while
Rust handles frame I/O, CRC32, segment rotation, and crash recovery.

Tracked by: #1397
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self

import orjson

if TYPE_CHECKING:
    from nexus.core.event_bus import FileEvent
    from nexus.services.event_log.protocol import EventLogConfig

logger = logging.getLogger(__name__)

# Maximum serialized event payload size (10 MB).
_MAX_PAYLOAD_BYTES = 10 * 1024 * 1024

# Rust extension availability
_HAS_NEXUS_WAL = False

try:
    from _nexus_wal import PyWAL

    _HAS_NEXUS_WAL = True
except ImportError:
    try:
        from nexus._nexus_wal import PyWAL  # noqa: F811

        _HAS_NEXUS_WAL = True
    except ImportError:
        PyWAL = None


def is_available() -> bool:
    """Return True if the Rust WAL extension is importable."""
    return _HAS_NEXUS_WAL


class WALEventLog:
    """EventLogProtocol implementation backed by Rust WAL.

    Thread-safe: the underlying Rust engine uses a parking_lot::Mutex.
    All async methods are thin wrappers — the Rust calls are synchronous
    but fast enough (<5μs) that they don't need to_thread offloading.
    """

    def __init__(self, config: EventLogConfig) -> None:
        if PyWAL is None:
            raise ImportError(
                "Rust WAL extension (_nexus_wal) not available. "
                "Build with: cd rust/nexus_wal && maturin develop"
            )

        wal_dir = Path(config.wal_dir)
        wal_dir.mkdir(parents=True, exist_ok=True)

        self._wal = PyWAL(
            str(wal_dir),
            config.segment_size_bytes,
            config.sync_mode,
        )
        self._closed = False
        logger.info("WALEventLog opened at %s (sync_mode=%s)", wal_dir, config.sync_mode)

    # -- EventLogProtocol ---------------------------------------------------

    async def append(self, event: FileEvent) -> int:
        payload = orjson.dumps(event.to_dict())
        if len(payload) > _MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"Event payload too large: {len(payload)} bytes (max {_MAX_PAYLOAD_BYTES})"
            )
        zone_id = (event.zone_id or "").encode()
        result: int = self._wal.append(zone_id, payload)
        return result

    async def append_batch(self, events: list[FileEvent]) -> list[int]:
        batch: list[tuple[bytes, bytes]] = []
        for e in events:
            payload = orjson.dumps(e.to_dict())
            if len(payload) > _MAX_PAYLOAD_BYTES:
                raise ValueError(
                    f"Event payload too large: {len(payload)} bytes (max {_MAX_PAYLOAD_BYTES})"
                )
            batch.append(((e.zone_id or "").encode(), payload))
        results: list[int] = self._wal.append_batch(batch)
        return results

    async def read_from(
        self,
        seq: int,
        limit: int = 1000,
        *,
        zone_id: str | None = None,
    ) -> list[FileEvent]:
        from nexus.core.event_bus import FileEvent

        zone_filter = zone_id.encode() if zone_id else None
        records = self._wal.read_from(seq, limit, zone_filter)
        return [FileEvent.from_dict(orjson.loads(payload)) for _seq, _zid, payload in records]

    async def truncate(self, before_seq: int) -> int:
        count: int = self._wal.truncate(before_seq)
        return count

    async def sync(self) -> None:
        self._wal.sync_wal()

    async def close(self) -> None:
        if not self._closed:
            self._wal.close()
            self._closed = True
            logger.info("WALEventLog closed")

    def current_sequence(self) -> int:
        seq: int = self._wal.current_sequence()
        return seq

    async def health_check(self) -> bool:
        ok: bool = self._wal.health_check()
        return ok

    # -- Context manager ----------------------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.close()
