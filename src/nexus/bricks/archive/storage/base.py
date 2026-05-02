"""Storage backend protocol for archive destinations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass
class StorageEntry:
    key: str
    size_bytes: int
    last_modified: datetime


class ArchiveStorage(Protocol):
    def put(self, key: str, source_path: Path) -> None: ...
    def get(self, key: str, target_path: Path) -> None: ...
    def delete(self, key: str) -> None: ...
    def list(self, prefix: str) -> list[StorageEntry]: ...
