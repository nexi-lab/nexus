"""Shared helper backends for isolation tests.

These classes are imported by IsolatedBackend child processes via their
module path string (e.g. ``tests.e2e.self_contained.isolation_helpers``).
They must be picklable and importable from a fresh interpreter.
"""

from __future__ import annotations

import hashlib


class MockBackend:
    """In-memory backend for isolation integration tests.

    Stores content in a dict keyed by SHA-256 hash.  Supports directory
    tracking for mkdir/rmdir/is_directory round-trips.
    """

    def __init__(self):
        self._store: dict[str, bytes] = {}
        self._dirs: set[str] = set()

    @property
    def name(self) -> str:
        return "mock"

    def connect(self, context=None):
        from nexus.backends.backend import HandlerStatusResponse

        return HandlerStatusResponse(success=True)

    def disconnect(self, context=None) -> None:
        pass

    def check_connection(self, context=None):
        from nexus.backends.backend import HandlerStatusResponse

        return HandlerStatusResponse(success=True)

    def write_content(self, content, context=None):
        from nexus.core.response import HandlerResponse

        h = hashlib.sha256(content).hexdigest()
        self._store[h] = content
        return HandlerResponse.ok(data=h, backend_name=self.name)

    def read_content(self, h, context=None):
        from nexus.core.response import HandlerResponse

        data = self._store.get(h, b"")
        return HandlerResponse.ok(data=data, backend_name=self.name)

    def delete_content(self, h, context=None):
        from nexus.core.response import HandlerResponse

        self._store.pop(h, None)
        return HandlerResponse.ok(data=None, backend_name=self.name)

    def content_exists(self, h, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=(h in self._store), backend_name=self.name)

    def get_content_size(self, h, context=None):
        from nexus.core.response import HandlerResponse

        size = len(self._store.get(h, b""))
        return HandlerResponse.ok(data=size, backend_name=self.name)

    def get_ref_count(self, h, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=1 if h in self._store else 0, backend_name=self.name)

    def mkdir(self, path, parents=False, exist_ok=False, context=None):
        from nexus.core.response import HandlerResponse

        self._dirs.add(path)
        return HandlerResponse.ok(data=None, backend_name=self.name)

    def rmdir(self, path, recursive=False, context=None):
        from nexus.core.response import HandlerResponse

        self._dirs.discard(path)
        return HandlerResponse.ok(data=None, backend_name=self.name)

    def is_directory(self, path, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=(path in self._dirs), backend_name=self.name)

    def list_dir(self, path, context=None):
        from nexus.core.response import HandlerResponse

        # Return empty list; isolation tests just need a valid response
        return HandlerResponse.ok(data=[], backend_name=self.name)
