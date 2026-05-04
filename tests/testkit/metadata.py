"""Metadata store test helpers."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC

__all__ = [
    "DictMetastore",
    "FailingMetastore",
    "InMemoryNexusFS",
    "MetastoreError",
]


def DictMetastore(  # noqa: N802
    storage_path: Any = None,
    *_args: Any,
    **_kwargs: Any,
) -> MetastoreABC:
    """Return a fresh production-compatible metastore for tests."""
    from nexus.storage.dict_metastore import DictMetastore as _factory

    return _factory(storage_path)


class MetastoreError(RuntimeError):
    """Injected metastore failure for testing."""

    def __init__(self, method: str, call_count: int) -> None:
        self.method = method
        self.call_count = call_count
        super().__init__(f"Injected metastore failure (method={method}, call #{call_count})")


class FailingMetastore(MetastoreABC):
    """MetastoreABC wrapper that injects failures for testing."""

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

    def rename_path(self, old_path: str, new_path: str) -> None:
        self._maybe_fail("rename_path")
        rename_path = getattr(self._inner, "rename_path", None)
        if callable(rename_path):
            rename_path(old_path, new_path)

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        self._maybe_fail("set_file_metadata")
        set_file_metadata = getattr(self._inner, "set_file_metadata", None)
        if callable(set_file_metadata):
            set_file_metadata(path, key, value)

    def get_file_metadata(self, path: str, key: str) -> Any:
        self._maybe_fail("get_file_metadata")
        get_file_metadata = getattr(self._inner, "get_file_metadata", None)
        if callable(get_file_metadata):
            return get_file_metadata(path, key)
        return None

    def is_implicit_directory(self, path: str) -> bool:
        self._maybe_fail("is_implicit_directory")
        is_implicit_directory = getattr(self._inner, "is_implicit_directory", None)
        if callable(is_implicit_directory):
            return is_implicit_directory(path)
        return False


class InMemoryNexusFS:
    """Minimal NexusFS double for tests using VFS-backed stores."""

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def sys_write(self, path: str, buf: bytes | str, **kwargs: Any) -> dict[str, Any]:
        if isinstance(buf, str):
            buf = buf.encode("utf-8")
        self._files[path] = bytes(buf)
        return {"path": path, "bytes_written": len(buf)}

    def sys_read(self, path: str, **kwargs: Any) -> dict[str, Any]:
        if path not in self._files:
            raise FileNotFoundError(path)
        return {"hit": True, "content": self._files[path]}

    def sys_readdir(self, path: str = "/", recursive: bool = True, **kwargs: Any) -> list[str]:
        prefix = path if path.endswith("/") else path + "/"
        names: set[str] = set()
        for full in self._files:
            if full.startswith(prefix):
                rest = full[len(prefix) :]
                if not rest:
                    continue
                if recursive or "/" not in rest:
                    names.add(rest.split("/", 1)[0])
        return sorted(names)

    def sys_unlink(self, path: str, **kwargs: Any) -> dict[str, Any]:
        if path not in self._files:
            raise FileNotFoundError(path)
        del self._files[path]
        return {"path": path}
