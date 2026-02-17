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
        from nexus.core.response import HandlerResponse

        h = hashlib.sha256(content).hexdigest()
        (self._content_dir / h).write_bytes(content)
        return HandlerResponse.ok(data=h, backend_name=self.name)

    def read_content(self, h, context=None):
        from nexus.core.response import HandlerResponse

        p = self._content_dir / h
        data = p.read_bytes() if p.exists() else b""
        return HandlerResponse.ok(data=data, backend_name=self.name)

    def delete_content(self, h, context=None):
        from nexus.core.response import HandlerResponse

        p = self._content_dir / h
        p.unlink(missing_ok=True)
        return HandlerResponse.ok(data=None, backend_name=self.name)

    def content_exists(self, h, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=(self._content_dir / h).exists(), backend_name=self.name)

    def get_content_size(self, h, context=None):
        from nexus.core.response import HandlerResponse

        p = self._content_dir / h
        size = p.stat().st_size if p.exists() else 0
        return HandlerResponse.ok(data=size, backend_name=self.name)

    def get_ref_count(self, h, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(
            data=1 if (self._content_dir / h).exists() else 0, backend_name=self.name
        )

    # ── directory ops ──

    def mkdir(self, path, parents=False, exist_ok=False, context=None):
        from nexus.core.response import HandlerResponse

        dirs = self._load_dirs()
        dirs.add(path)
        self._save_dirs(dirs)
        return HandlerResponse.ok(data=None, backend_name=self.name)

    def rmdir(self, path, recursive=False, context=None):
        from nexus.core.response import HandlerResponse

        dirs = self._load_dirs()
        dirs.discard(path)
        self._save_dirs(dirs)
        return HandlerResponse.ok(data=None, backend_name=self.name)

    def is_directory(self, path, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=(path in self._load_dirs()), backend_name=self.name)

    def list_dir(self, path, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=[], backend_name=self.name)
