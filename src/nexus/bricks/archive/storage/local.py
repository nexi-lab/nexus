"""Local-filesystem archive storage backend."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from nexus.bricks.archive.storage.base import StorageEntry


class LocalArchiveStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _abs(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, source_path: Path) -> None:
        target = self._abs(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_path.resolve() != target.resolve():
            shutil.copy2(source_path, target)

    def get(self, key: str, target_path: Path) -> None:
        shutil.copy2(self._abs(key), target_path)

    def delete(self, key: str) -> None:
        self._abs(key).unlink()

    def list(self, prefix: str) -> list[StorageEntry]:
        out: list[StorageEntry] = []
        if not self.root.exists():
            return out
        search_root = self.root
        for p in search_root.rglob("*"):
            if p.is_file():
                key = str(p.relative_to(self.root))
                if key.startswith(prefix):
                    stat = p.stat()
                    out.append(
                        StorageEntry(
                            key=key,
                            size_bytes=stat.st_size,
                            last_modified=datetime.fromtimestamp(stat.st_mtime, UTC),
                        )
                    )
        return out
