"""Error-injection wrapper for any MetastoreABC implementation.

Used in tests to simulate metadata store failures on specific operations,
enabling verification of error handling when the inode layer fails.

Usage:
    store = FailingMetastore(DictMetastore(), fail_on=["put"])
    store.get("/file.txt")  # OK
    store.put(metadata)     # raises RuntimeError

    # Or fail on nth call to any method:
    store = FailingMetastore(DictMetastore(), fail_on_nth=2)
    store.get("/a.txt")     # OK (call 1)
    store.get("/b.txt")     # raises RuntimeError (call 2)
"""

from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC


class MetastoreError(RuntimeError):
    """Injected metastore failure for testing."""

    def __init__(self, method: str, call_count: int) -> None:
        self.method = method
        self.call_count = call_count
        super().__init__(f"Injected metastore failure (method={method}, call #{call_count})")


class FailingMetastore(MetastoreABC):
    """MetastoreABC wrapper that injects failures for testing.

    Args:
        inner: The real metastore to delegate to.
        fail_on: List of method names that should always fail.
        fail_on_nth: Fail on the Nth call (1-indexed). 0 means never fail by count.
        fail_permanently: If True, fail on every call at or after fail_on_nth.
    """

    def __init__(
        self,
        inner: MetastoreABC,
        *,
        fail_on: list[str] | None = None,
        fail_on_nth: int = 0,
        fail_permanently: bool = False,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._fail_on = set(fail_on) if fail_on else set()
        self._fail_on_nth = fail_on_nth
        self._fail_permanently = fail_permanently
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def reset(self) -> None:
        """Reset the call counter."""
        self._call_count = 0

    def _maybe_fail(self, method_name: str) -> None:
        """Check whether this call should fail."""
        self._call_count += 1

        if method_name in self._fail_on:
            raise MetastoreError(method_name, self._call_count)

        if self._fail_on_nth <= 0:
            return

        should_fail = (
            self._call_count >= self._fail_on_nth
            if self._fail_permanently
            else self._call_count == self._fail_on_nth
        )

        if should_fail:
            raise MetastoreError(method_name, self._call_count)

    # === Abstract method implementations ===

    def _get_raw(self, path: str) -> FileMetadata | None:
        self._maybe_fail("get")
        return self._inner.get(path)

    def _put_raw(self, metadata: FileMetadata) -> int | None:
        self._maybe_fail("put")
        return self._inner.put(metadata)

    def _delete_raw(self, path: str) -> dict[str, Any] | None:
        self._maybe_fail("delete")
        return self._inner.delete(path)

    def _exists_raw(self, path: str) -> bool:
        self._maybe_fail("exists")
        return self._inner.exists(path)

    def _list_raw(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> list[FileMetadata]:
        self._maybe_fail("list")
        return self._inner.list(prefix, recursive, **kwargs)

    def close(self) -> None:
        self._inner.close()

    # === Concrete method overrides ===

    def is_committed(self, token: int) -> str | None:
        self._maybe_fail("is_committed")
        return self._inner.is_committed(token)

    def _list_iter_raw(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> Iterator[FileMetadata]:
        self._maybe_fail("list_iter")
        return self._inner.list_iter(prefix, recursive, **kwargs)

    def _get_batch_raw(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        self._maybe_fail("get_batch")
        return self._inner.get_batch(paths)

    def _delete_batch_raw(self, paths: Sequence[str]) -> None:
        self._maybe_fail("delete_batch")
        self._inner.delete_batch(paths)

    def _put_batch_raw(
        self,
        metadata_list: Sequence[FileMetadata],
        *,
        skip_snapshot: bool = False,
    ) -> None:
        self._maybe_fail("put_batch")
        self._inner.put_batch(metadata_list, skip_snapshot=skip_snapshot)

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        self._maybe_fail("batch_get_content_ids")
        return self._inner.batch_get_content_ids(paths)

    # === Duck-typing methods used by NexusFS kernel ===

    def rename_path(self, old_path: str, new_path: str) -> None:
        self._maybe_fail("rename_path")
        if hasattr(self._inner, "rename_path"):
            self._inner.rename_path(old_path, new_path)  # type: ignore[attr-defined]

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        self._maybe_fail("set_file_metadata")
        if hasattr(self._inner, "set_file_metadata"):
            self._inner.set_file_metadata(path, key, value)  # type: ignore[attr-defined]

    def get_file_metadata(self, path: str, key: str) -> Any:
        self._maybe_fail("get_file_metadata")
        if hasattr(self._inner, "get_file_metadata"):
            return self._inner.get_file_metadata(path, key)  # type: ignore[attr-defined]
        return None

    def is_implicit_directory(self, path: str) -> bool:
        self._maybe_fail("is_implicit_directory")
        if hasattr(self._inner, "is_implicit_directory"):
            return self._inner.is_implicit_directory(path)  # type: ignore[attr-defined]
        return False
