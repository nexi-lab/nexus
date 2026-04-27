"""Kernel-tier file event data types.

FileEvent is the single kernel-defined I/O event type, analogous to Linux
``fsnotify_event``. One event struct, multiple consumer paths:

- KernelDispatch OBSERVE phase (local, fire-and-forget)
- EventBus (distributed delivery via Dragonfly/NATS)

Per NEXUS-LEGO-ARCHITECTURE, data types can be defined in lower tiers and used
by higher tiers. FileEvent is a kernel data type consumed by all layers.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class FileEventType(StrEnum):
    """Types of file system events."""

    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    FILE_RENAME = "file_rename"
    FILE_COPY = "file_copy"
    METADATA_CHANGE = "metadata_change"  # chmod, chown, truncate (Issue #1115)
    DIR_CREATE = "dir_create"
    DIR_DELETE = "dir_delete"
    CONFLICT_DETECTED = "conflict_detected"
    # Mount/unmount lifecycle events (Step C: unified into OBSERVE phase)
    MOUNT = "mount"
    UNMOUNT = "unmount"


# ── Bitmask positions for Rust ObserverRegistry filtering (Issue #1748) ──

FILE_EVENT_BIT: dict[FileEventType, int] = {
    FileEventType.FILE_WRITE: 1 << 0,
    FileEventType.FILE_DELETE: 1 << 1,
    FileEventType.FILE_RENAME: 1 << 2,
    FileEventType.METADATA_CHANGE: 1 << 3,
    FileEventType.DIR_CREATE: 1 << 4,
    FileEventType.DIR_DELETE: 1 << 5,
    FileEventType.CONFLICT_DETECTED: 1 << 6,
    FileEventType.FILE_COPY: 1 << 7,
    FileEventType.MOUNT: 1 << 8,
    FileEventType.UNMOUNT: 1 << 9,
}
ALL_FILE_EVENTS: int = (1 << 10) - 1  # 0x3FF — matches all event types


@dataclass(frozen=True)
class FileEvent:
    """Frozen kernel I/O event — immutable once created.

    Analogous to Linux ``fsnotify_event``. Carries all context that consumers
    might need; each consumer extracts what it requires and ignores the rest.

    Used by KernelDispatch OBSERVE phase and EventBus distributed delivery.
    """

    type: FileEventType | str
    path: str
    zone_id: str | None = None  # Kernel namespace partition (None for Layer 1 local events)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    old_path: str | None = None
    size: int | None = None
    content_id: str | None = None
    agent_id: str | None = None
    vector_clock: str | None = None
    sequence_number: int | None = None  # Monotonic ordering within a zone (#2755)

    # Identity & write-specific context
    user_id: str | None = None
    version: int | None = None  # write-specific: file version counter
    is_new: bool = False  # write-specific: True if file was created (not overwritten)
    new_path: str | None = None  # rename-specific: destination path
    old_content_id: str | None = (
        None  # write-specific: previous content hash (for overwrite detection)
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result: dict[str, Any] = {
            "type": self.type.value if isinstance(self.type, FileEventType) else self.type,
            "path": self.path,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
        }
        # Optional fields - only include if set
        if self.zone_id is not None:
            result["zone_id"] = self.zone_id
        if self.old_path is not None:
            result["old_path"] = self.old_path
        if self.size is not None:
            result["size"] = self.size
        if self.content_id is not None:
            result["content_id"] = self.content_id
        if self.agent_id is not None:
            result["agent_id"] = self.agent_id
        if self.vector_clock is not None:
            result["vector_clock"] = self.vector_clock
        if self.sequence_number is not None:
            result["sequence_number"] = self.sequence_number
        if self.user_id is not None:
            result["user_id"] = self.user_id
        if self.version is not None:
            result["version"] = self.version
        if self.is_new:
            result["is_new"] = self.is_new
        if self.new_path is not None:
            result["new_path"] = self.new_path
        if self.old_content_id is not None:
            result["old_content_id"] = self.old_content_id
        return result

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileEvent":
        """Create FileEvent from dictionary."""
        return cls(
            type=data["type"],
            path=data["path"],
            zone_id=data.get("zone_id"),  # Optional for Layer 1
            timestamp=data.get("timestamp", datetime.now(UTC).isoformat()),
            event_id=data.get("event_id", str(uuid.uuid4())),
            old_path=data.get("old_path"),
            size=data.get("size"),
            content_id=data.get("content_id"),
            agent_id=data.get("agent_id"),
            vector_clock=data.get("vector_clock"),
            sequence_number=data.get("sequence_number"),
            user_id=data.get("user_id"),
            version=data.get("version"),
            is_new=data.get("is_new", False),
            new_path=data.get("new_path"),
            old_content_id=data.get("old_content_id"),
        )

    @classmethod
    def from_json(cls, json_str: str | bytes) -> "FileEvent":
        """Deserialize from JSON string."""
        if isinstance(json_str, bytes):
            json_str = json_str.decode("utf-8")
        return cls.from_dict(json.loads(json_str))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FileEvent):
            return NotImplemented
        return self.event_id == other.event_id

    def __hash__(self) -> int:
        return hash(self.event_id)

    def matches_path_pattern(self, pattern: str) -> bool:
        """Check if this event matches a path pattern.

        Supports:
        - Exact match: "/inbox/file.txt"
        - Directory match: "/inbox/" (matches all files in /inbox/)
        - Glob patterns: "/inbox/*.txt", "/inbox/**"

        Args:
            pattern: Path pattern to match against

        Returns:
            True if event path matches the pattern
        """
        # Exact match
        if self.path == pattern:
            return True

        # Directory match - pattern ends with / OR pattern is a directory path
        # Handle both "/inbox/" and "/inbox" as directory patterns
        if pattern.endswith("/"):
            if self.path.startswith(pattern):
                return True
            if self.path == pattern.rstrip("/"):
                return True
        else:
            # Pattern without trailing slash - treat as directory if path is under it
            # e.g., pattern "/inbox" should match path "/inbox/test.txt"
            if self.path.startswith(pattern + "/"):
                return True

        # For rename events, also check old_path
        if self.old_path:
            if self.old_path == pattern:
                return True
            if pattern.endswith("/") and self.old_path.startswith(pattern):
                return True
            if not pattern.endswith("/") and self.old_path.startswith(pattern + "/"):
                return True

        # Glob pattern match — delegate to path_utils for consistency
        if "*" in pattern or "?" in pattern:
            from nexus.core.path_utils import path_matches_pattern

            if path_matches_pattern(self.path, pattern):
                return True
            if self.old_path and path_matches_pattern(self.old_path, pattern):
                return True

        return False
