"""In-memory MockBackend for testing CachingBackendWrapper.

Purpose-built dict-based Backend implementation. No disk I/O, no external
dependencies. Supports call tracking for verifying cache behavior.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from nexus.backends.backend import Backend
from nexus.core.response import HandlerResponse

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.core.permissions_enhanced import EnhancedOperationContext


class MockBackend(Backend):
    """In-memory Backend for unit testing.

    Stores content in dicts. Tracks method call counts so tests can verify
    whether the cache prevented calls to the inner backend.
    """

    def __init__(self) -> None:
        # content_hash -> bytes
        self._content: dict[str, bytes] = {}
        # content_hash -> ref_count
        self._ref_counts: dict[str, int] = {}
        # path -> is_directory
        self._dirs: set[str] = set()
        # Call counters for cache verification
        self.call_counts: dict[str, int] = {
            "read_content": 0,
            "write_content": 0,
            "delete_content": 0,
            "content_exists": 0,
            "get_content_size": 0,
            "get_ref_count": 0,
            "batch_read_content": 0,
            "mkdir": 0,
            "rmdir": 0,
            "is_directory": 0,
            "list_dir": 0,
        }

    @property
    def name(self) -> str:
        return "mock"

    def _hash(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    # === Content Operations ===

    def write_content(
        self, content: bytes, context: OperationContext | None = None
    ) -> HandlerResponse[str]:
        self.call_counts["write_content"] += 1
        content_hash = self._hash(content)
        if content_hash in self._content:
            self._ref_counts[content_hash] += 1
        else:
            self._content[content_hash] = content
            self._ref_counts[content_hash] = 1
        return HandlerResponse.ok(data=content_hash, backend_name="mock")

    def read_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bytes]:
        self.call_counts["read_content"] += 1
        if content_hash not in self._content:
            return HandlerResponse.not_found(
                path=content_hash, message="Content not found", backend_name="mock"
            )
        return HandlerResponse.ok(data=self._content[content_hash], backend_name="mock")

    def batch_read_content(
        self, content_hashes: list[str], context: OperationContext | None = None
    ) -> dict[str, bytes | None]:
        self.call_counts["batch_read_content"] += 1
        result: dict[str, bytes | None] = {}
        for h in content_hashes:
            result[h] = self._content.get(h)
        return result

    def delete_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[None]:
        self.call_counts["delete_content"] += 1
        if content_hash not in self._content:
            return HandlerResponse.not_found(
                path=content_hash, message="Content not found", backend_name="mock"
            )
        self._ref_counts[content_hash] -= 1
        if self._ref_counts[content_hash] <= 0:
            del self._content[content_hash]
            del self._ref_counts[content_hash]
        return HandlerResponse.ok(data=None, backend_name="mock")

    def content_exists(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]:
        self.call_counts["content_exists"] += 1
        return HandlerResponse.ok(data=content_hash in self._content, backend_name="mock")

    def get_content_size(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]:
        self.call_counts["get_content_size"] += 1
        if content_hash not in self._content:
            return HandlerResponse.not_found(
                path=content_hash, message="Content not found", backend_name="mock"
            )
        return HandlerResponse.ok(data=len(self._content[content_hash]), backend_name="mock")

    def get_ref_count(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]:
        self.call_counts["get_ref_count"] += 1
        return HandlerResponse.ok(data=self._ref_counts.get(content_hash, 0), backend_name="mock")

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> HandlerResponse[None]:
        self.call_counts["mkdir"] += 1
        if path in self._dirs and not exist_ok:
            return HandlerResponse.error("Directory exists", code=409, backend_name="mock")
        self._dirs.add(path)
        if parents:
            parts = path.strip("/").split("/")
            for i in range(1, len(parts)):
                self._dirs.add("/" + "/".join(parts[:i]))
        return HandlerResponse.ok(data=None, backend_name="mock")

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> HandlerResponse[None]:
        self.call_counts["rmdir"] += 1
        if path not in self._dirs:
            return HandlerResponse.not_found(
                path=path, message="Directory not found", backend_name="mock"
            )
        self._dirs.discard(path)
        return HandlerResponse.ok(data=None, backend_name="mock")

    def is_directory(
        self, path: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]:
        self.call_counts["is_directory"] += 1
        return HandlerResponse.ok(data=path in self._dirs, backend_name="mock")

    def list_dir(self, path: str, context: OperationContext | None = None) -> list[str]:
        self.call_counts["list_dir"] += 1
        if path not in self._dirs:
            raise FileNotFoundError(f"Directory not found: {path}")
        return [
            d + "/"
            for d in self._dirs
            if d.startswith(path + "/") and "/" not in d[len(path) + 1 :]
        ]
