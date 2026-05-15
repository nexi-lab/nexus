"""SharedRingBuffer — Cross-process mmap ring buffer.

Provides zero-copy IPC via memory-mapped files for:
- Zone graph tuple update broadcasting (Zone A -> Zone B)
- Revision sequence broadcasting across processes

Uses SPSC (Single-Producer Single-Consumer) lock-free protocol
with atomic sequence numbers for synchronization.

Related: Issue #3192
"""

import logging
import mmap
import os
import struct
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

# Ring buffer header layout (32 bytes):
# [0:8]   write_seq (uint64)  — monotonically increasing write sequence
# [8:16]  read_seq (uint64)   — last read sequence (per consumer)
# [16:20] entry_size (uint32) — size of each entry in bytes
# [20:24] capacity (uint32)   — max number of entries
# [24:28] flags (uint32)      — 0x1 = producer alive
# [28:32] reserved
HEADER_SIZE = 32
HEADER_FMT = "<QQIIIxxxx"  # Note: xxxx for 4 bytes padding to align to 32


class SharedRingBuffer:
    """Cross-process ring buffer backed by mmap.

    Designed for broadcasting small updates (tuples, revision numbers)
    between processes without network round-trips.

    Thread-safe for single-producer, single-consumer per buffer.
    For multiple consumers, create separate buffers or use read-only views.
    """

    def __init__(
        self,
        name: str,
        entry_size: int = 1024,
        capacity: int = 4096,
        base_dir: str | None = None,
    ):
        """Initialize or attach to a shared ring buffer.

        Args:
            name: Unique name for this buffer (used as filename)
            entry_size: Size of each entry in bytes
            capacity: Maximum number of entries
            base_dir: Directory for mmap files (default: temp dir)
        """
        self._name = name
        self._entry_size = entry_size
        self._capacity = capacity

        self._base_dir = base_dir or os.path.join(tempfile.gettempdir(), "nexus-shm")
        os.makedirs(self._base_dir, exist_ok=True)

        self._file_path = os.path.join(self._base_dir, f"{name}.shm")
        self._total_size = HEADER_SIZE + (entry_size * capacity)

        self._fd: int | None = None
        self._mm: mmap.mmap | None = None
        self._is_producer = False

        # Metrics
        self._writes = 0
        self._reads = 0
        self._overflows = 0

    def open_producer(self) -> "SharedRingBuffer":
        """Open as producer (creates file if needed).

        Returns:
            self for chaining
        """
        fd = os.open(self._file_path, os.O_RDWR | os.O_CREAT)

        # Ensure file is correct size
        current_size = os.fstat(fd).st_size
        if current_size < self._total_size:
            os.ftruncate(fd, self._total_size)

        self._fd = fd
        self._mm = mmap.mmap(fd, self._total_size)
        self._is_producer = True

        # Write header
        write_seq, read_seq, _, _, flags = self._read_header()
        self._write_header(write_seq, read_seq, self._entry_size, self._capacity, 0x1)

        logger.info(
            "[SHM] Producer opened: %s (entry_size=%d, capacity=%d)",
            self._name,
            self._entry_size,
            self._capacity,
        )
        return self

    def open_consumer(self) -> "SharedRingBuffer":
        """Open as consumer (read-only).

        Returns:
            self for chaining

        Raises:
            FileNotFoundError: If buffer doesn't exist yet
        """
        if not os.path.exists(self._file_path):
            raise FileNotFoundError(f"Shared ring buffer not found: {self._file_path}")

        fd = os.open(self._file_path, os.O_RDONLY)
        self._fd = fd
        self._mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
        self._is_producer = False

        logger.info("[SHM] Consumer opened: %s", self._name)
        return self

    def write(self, data: bytes) -> int:
        """Write an entry to the ring buffer.

        Args:
            data: Entry data (must be <= entry_size)

        Returns:
            Sequence number of written entry

        Raises:
            ValueError: If data exceeds entry_size
            RuntimeError: If not opened as producer
        """
        if not self._is_producer or self._mm is None:
            raise RuntimeError("Not opened as producer")
        if len(data) > self._entry_size:
            raise ValueError(f"Data size {len(data)} exceeds entry_size {self._entry_size}")

        write_seq, read_seq, entry_size, capacity, flags = self._read_header()

        # Calculate offset in ring
        slot = write_seq % capacity
        offset = HEADER_SIZE + (slot * entry_size)

        # Write entry: [4 bytes length][data][padding]
        entry = struct.pack("<I", len(data)) + data
        padded = entry.ljust(entry_size, b"\x00")
        self._mm[offset : offset + entry_size] = padded

        # Update write sequence (atomic for readers)
        new_seq = write_seq + 1
        self._write_header(new_seq, read_seq, entry_size, capacity, flags)

        self._writes += 1
        return new_seq

    def read(self, from_seq: int = 0) -> list[tuple[int, bytes]]:
        """Read entries from a sequence number.

        Args:
            from_seq: Sequence to start reading from (exclusive)

        Returns:
            List of (sequence, data) tuples for new entries
        """
        if self._mm is None:
            return []

        write_seq, _, entry_size, capacity, flags = self._read_header()

        if from_seq >= write_seq:
            return []  # No new entries

        # Calculate how many entries to read (capped by capacity)
        available = write_seq - from_seq
        if available > capacity:
            # Buffer has wrapped — we missed some entries
            from_seq = write_seq - capacity
            self._overflows += 1

        entries: list[tuple[int, bytes]] = []
        for seq in range(from_seq + 1, write_seq + 1):
            slot = (seq - 1) % capacity
            offset = HEADER_SIZE + (slot * entry_size)

            raw = self._mm[offset : offset + entry_size]
            data_len = struct.unpack("<I", raw[:4])[0]
            data = raw[4 : 4 + data_len]
            entries.append((seq, data))
            self._reads += 1

        return entries

    def close(self) -> None:
        """Close the ring buffer."""
        if self._mm is not None:
            if self._is_producer:
                # Clear producer flag
                try:
                    write_seq, read_seq, entry_size, capacity, _ = self._read_header()
                    self._write_header(write_seq, read_seq, entry_size, capacity, 0x0)
                except Exception:
                    pass
            self._mm.close()
            self._mm = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def cleanup(self) -> None:
        """Close and delete the backing file."""
        self.close()
        import contextlib

        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._file_path)

    def get_stats(self) -> dict[str, Any]:
        """Get ring buffer statistics."""
        write_seq = 0
        if self._mm is not None:
            write_seq, _, _, _, _ = self._read_header()
        return {
            "name": self._name,
            "file_path": self._file_path,
            "entry_size": self._entry_size,
            "capacity": self._capacity,
            "write_sequence": write_seq,
            "writes": self._writes,
            "reads": self._reads,
            "overflows": self._overflows,
            "is_producer": self._is_producer,
        }

    def _read_header(self) -> tuple[int, int, int, int, int]:
        """Read ring buffer header."""
        if self._mm is None:
            return (0, 0, self._entry_size, self._capacity, 0)
        raw = self._mm[:HEADER_SIZE]
        write_seq, read_seq, entry_size, capacity, flags = struct.unpack("<QQIII", raw[:28])
        return (write_seq, read_seq, entry_size, capacity, flags)

    def _write_header(
        self, write_seq: int, read_seq: int, entry_size: int, capacity: int, flags: int
    ) -> None:
        """Write ring buffer header."""
        if self._mm is None:
            return
        header = struct.pack("<QQIII", write_seq, read_seq, entry_size, capacity, flags)
        self._mm[:28] = header

    def __enter__(self) -> "SharedRingBuffer":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
