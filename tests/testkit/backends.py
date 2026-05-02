"""Backend fakes and compatibility re-exports for Nexus tests."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from nexus.backends.base.backend import Backend
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.core.object_store import WriteResult
from tests.helpers.dict_metastore import DictMetastore
from tests.helpers.failing_backend import FailingBackend
from tests.helpers.in_memory_record_store import InMemoryRecordStore
from tests.helpers.inmemory_nexus_fs import InMemoryNexusFS

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

__all__ = [
    "DictMetastore",
    "FactoryStubBackend",
    "FailingBackend",
    "InMemoryBackend",
    "InMemoryNexusFS",
    "InMemoryRecordStore",
]


def _normalize_dir(path: str) -> str:
    """Normalize a directory path to an absolute path without trailing slash."""

    if not path or path == "/":
        return "/"
    return "/" + path.strip("/")


class InMemoryBackend(Backend):
    """Minimal in-memory backend for unit tests."""

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}
        self._dirs: set[str] = {"/"}

    @property
    def name(self) -> str:
        return "memory"

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        if offset:
            existing = self.read_content(content_id, context=context)
            content = existing[:offset] + content
        key = content_id or hashlib.sha256(content).hexdigest()
        self._content[key] = bytes(content)
        return WriteResult(content_id=key, version=key, size=len(content))

    def read_content(
        self,
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> bytes:
        try:
            return self._content[content_id]
        except KeyError as exc:
            raise NexusFileNotFoundError(content_id, "Content not found") from exc

    def delete_content(
        self,
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> None:
        if content_id not in self._content:
            raise NexusFileNotFoundError(content_id, "Content not found")
        del self._content[content_id]

    def content_exists(
        self,
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        return content_id in self._content

    def get_content_size(
        self,
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> int:
        return len(self.read_content(content_id, context=context))

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        directory = _normalize_dir(path)
        if directory in self._dirs:
            if exist_ok:
                return
            return

        if parents:
            current = ""
            for part in directory.strip("/").split("/"):
                current = f"{current}/{part}"
                self._dirs.add(current)
            return

        parent = _normalize_dir(directory.rsplit("/", 1)[0] or "/")
        if parent not in self._dirs:
            raise NexusFileNotFoundError(parent, "Directory not found")
        self._dirs.add(directory)

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        directory = _normalize_dir(path)
        if directory not in self._dirs:
            raise NexusFileNotFoundError(directory, "Directory not found")
        if directory == "/":
            return
        if recursive:
            prefix = f"{directory}/"
            self._dirs = {
                item for item in self._dirs if item != directory and not item.startswith(prefix)
            }
            return
        self._dirs.remove(directory)

    def is_directory(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        return _normalize_dir(path) in self._dirs

    def list_dir(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> list[str]:
        directory = _normalize_dir(path)
        if directory not in self._dirs:
            raise NexusFileNotFoundError(directory, "Directory not found")
        prefix = "/" if directory == "/" else f"{directory}/"
        names = {
            candidate[len(prefix) :].split("/", 1)[0]
            for candidate in self._dirs
            if candidate != directory and candidate.startswith(prefix)
        }
        return sorted(names)


class FactoryStubBackend(InMemoryBackend):
    """Backend test double that records arbitrary factory kwargs."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.kwargs = kwargs

    @property
    def name(self) -> str:
        return "stub"
