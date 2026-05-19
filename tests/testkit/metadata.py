"""Metadata store test helpers.

Post-W3 the Python ``MetastoreABC`` / ``RustMetastoreProxy`` /
``DictMetastore`` classes are gone. The kernel exposes
``sys_stat`` / ``sys_setattr`` / ``sys_unlink`` / ``access`` /
``stat_batch`` PyO3 bindings; tests that previously instantiated a
Python metastore subclass now get a bare ``PyKernel`` from
``DictMetastore()``. ``FailingMetastore`` becomes a fault-injection
wrapper that delegates to kernel syscalls.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from nexus.contracts.metadata import FileMetadata

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
) -> Any:
    """Return a fresh kernel-backed metastore for tests.

    A new ``PyKernel`` is constructed per call with its redb store opened
    against either ``storage_path`` (when supplied) or a tempfile.
    """
    from nexus.remote.kernel_client import KernelClient as PyKernel

    if storage_path is None:
        redb_path = str(Path(tempfile.mkdtemp()) / "dict.redb")
    else:
        redb_path = str(storage_path)
    kernel = PyKernel()
    kernel.set_metastore_path(redb_path)
    return kernel


class MetastoreError(RuntimeError):
    """Injected metastore failure for testing."""

    def __init__(self, method: str, call_count: int) -> None:
        self.method = method
        self.call_count = call_count
        super().__init__(f"Injected metastore failure (method={method}, call #{call_count})")


class FailingMetastore:
    """Kernel-handle wrapper that injects failures for testing.

    Wraps a real ``PyKernel`` and routes every syscall through
    ``_maybe_fail`` before delegating. The wrapper exposes the
    same surface tests use (``get`` / ``put`` / ``delete`` /
    ``sys_stat`` / ``sys_setattr`` / etc.) so existing call
    sites work unchanged.
    """

    def __init__(
        self,
        inner: Any,
        *,
        fail_on: list[str] | None = None,
        fail_on_nth: int = 0,
        fail_permanently: bool = False,
    ) -> None:
        # ``inner`` may be a bare kernel or a kernel wrapper. We extract
        # the kernel handle via the same dual-shape guard used elsewhere.
        self._inner = (
            inner._rust_kernel if inner is not None and hasattr(inner, "_rust_kernel") else inner
        )
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

    # ── proxy-shape compatibility (legacy callers expect ``.get`` etc.) ──

    def get(self, path: str) -> FileMetadata | None:
        self._maybe_fail("get")
        return self._inner.sys_stat(path, "root")

    def put(self, metadata: FileMetadata) -> Any:
        self._maybe_fail("put")
        return self._inner.sys_setattr(
            metadata.path,
            entry_type=metadata.entry_type if hasattr(metadata, "entry_type") else 1,
        )

    def delete(self, path: str) -> Any:
        self._maybe_fail("delete")
        return self._inner.sys_unlink(path, None, False)

    def exists(self, path: str) -> bool:
        self._maybe_fail("exists")
        return self._inner.access(path, "root")

    def list(self, prefix: str = "", recursive: bool = True, **_kwargs: Any) -> list[FileMetadata]:
        self._maybe_fail("list")
        page = self._inner.metastore_list_paginated(prefix, recursive, 100000, None)
        return page["items"]

    def is_implicit_directory(self, path: str) -> bool:
        self._maybe_fail("is_implicit_directory")
        stat = self._inner.sys_stat(path, "root")
        return stat is not None and stat.get("is_directory", False)

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        self._maybe_fail("set_file_metadata")
        if value is None:
            return
        if not isinstance(value, str):
            import json

            value = json.dumps(value)
        self._inner.set_xattr(path, key, value)

    def get_file_metadata(self, path: str, key: str) -> Any:
        self._maybe_fail("get_file_metadata")
        return self._inner.get_xattr(path, key)

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        self._maybe_fail("get_batch")
        plist = list(paths)
        return dict(zip(plist, self._inner.stat_batch(plist, "root"), strict=True))

    def delete_batch(self, paths: Sequence[str]) -> None:
        self._maybe_fail("delete_batch")
        for p in paths:
            self._inner.sys_unlink(p, None, False)

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        self._maybe_fail("batch_get_content_ids")
        result: dict[str, str | None] = {}
        for path in paths:
            stat = self._inner.sys_stat(path, "root")
            result[path] = stat.get("content_id") if stat else None
        return result

    def close(self) -> None:
        """No-op — kernel manages redb lifecycle."""

    # ── ``_rust_kernel`` shim so legacy callers that read it still work ──
    @property
    def _rust_kernel(self) -> Any:
        return self._inner


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

    def sys_setattr(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return {"path": path, "created": path not in self._files}

    def sys_unlink(self, path: str, **kwargs: Any) -> dict[str, Any]:
        if path not in self._files:
            raise FileNotFoundError(path)
        del self._files[path]
        return {"path": path}
