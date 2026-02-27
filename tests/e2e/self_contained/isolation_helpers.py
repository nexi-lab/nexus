"""Shared helper backends for isolation tests.

These classes are imported by IsolatedBackend child processes via their
module path string (e.g. ``tests.e2e.self_contained.isolation_helpers``).
They must be picklable and importable from a fresh interpreter.
"""

import hashlib
import json
import tempfile
from pathlib import Path


class MockBackend:
    """Filesystem-backed mock backend for isolation integration tests.

    Uses a shared directory so multiple worker processes (pool_size>1)
    see the same state.  Content is stored as files keyed by SHA-256 hash;
    directory tracking uses a JSON sidecar file.

    If ``storage_dir`` is omitted, each instance creates its own temp
    directory (state NOT shared across instances — useful for cross-brick
    isolation tests).

    Returns direct values (not HandlerResponse) following the ObjectStoreABC
    contract.  Errors are raised as exceptions.
    """

    def __init__(self, storage_dir: str | None = None):
        self._root = Path(storage_dir) if storage_dir else Path(tempfile.mkdtemp())
        self._root.mkdir(parents=True, exist_ok=True)
        self._content_dir = self._root / "content"
        self._content_dir.mkdir(exist_ok=True)
        self._dirs_file = self._root / "_dirs.json"

    @property
    def name(self) -> str:
        return "mock"

    # ── helpers ──

    def _load_dirs(self) -> set[str]:
        try:
            return set(json.loads(self._dirs_file.read_text()))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save_dirs(self, dirs: set[str]) -> None:
        self._dirs_file.write_text(json.dumps(sorted(dirs)))

    # ── lifecycle ──

    def connect(self, context=None):
        from nexus.backends.backend import HandlerStatusResponse

        return HandlerStatusResponse(success=True)

    def disconnect(self, context=None) -> None:
        pass

    def check_connection(self, context=None):
        from nexus.backends.backend import HandlerStatusResponse

        return HandlerStatusResponse(success=True)

    # ── content ops ──

    def write_content(self, content, context=None):
        from nexus.core.object_store import WriteResult

        h = hashlib.sha256(content).hexdigest()
        (self._content_dir / h).write_bytes(content)
        return WriteResult(content_hash=h, size=len(content))

    def read_content(self, h, context=None):
        from nexus.contracts.exceptions import NexusFileNotFoundError

        p = self._content_dir / h
        if not p.exists():
            raise NexusFileNotFoundError(f"Content not found: {h}")
        return p.read_bytes()

    def delete_content(self, h, context=None):
        p = self._content_dir / h
        p.unlink(missing_ok=True)

    def content_exists(self, h, context=None):
        return (self._content_dir / h).exists()

    def get_content_size(self, h, context=None):
        p = self._content_dir / h
        return p.stat().st_size if p.exists() else 0

    def get_ref_count(self, h, context=None):
        return 1 if (self._content_dir / h).exists() else 0

    # ── directory ops ──

    def mkdir(self, path, parents=False, exist_ok=False, context=None):
        dirs = self._load_dirs()
        dirs.add(path)
        self._save_dirs(dirs)

    def rmdir(self, path, recursive=False, context=None):
        dirs = self._load_dirs()
        dirs.discard(path)
        self._save_dirs(dirs)

    def is_directory(self, path, context=None):
        return path in self._load_dirs()

    def list_dir(self, path, context=None):
        return []
