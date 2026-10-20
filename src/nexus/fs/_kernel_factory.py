"""Share one native process per metadata path through owned client handles."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class _SharedKernel:
    client: Any
    owners: int = 0


_KERNEL_CACHE: dict[str, _SharedKernel] = {}
_KERNEL_CACHE_LOCK = threading.Lock()


class _KernelHandle:
    def __init__(self, path: str, shared: _SharedKernel) -> None:
        self._path = path
        self._shared = shared
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        if self._closed:
            raise RuntimeError("Native filesystem handle is closed")
        return getattr(self._shared.client, name)

    def close(self) -> None:
        with _KERNEL_CACHE_LOCK:
            if self._closed:
                return
            self._closed = True
            self._shared.owners -= 1
            if self._shared.owners == 0:
                try:
                    self._shared.client.close()
                finally:
                    del _KERNEL_CACHE[self._path]


def create_kernel(db_path: str | Path) -> Any:
    """Acquire a native client; the caller must close the returned handle."""
    from nexus.remote.kernel_client import KernelClient

    path = Path(db_path).with_suffix(".redb").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(str(path))
    with _KERNEL_CACHE_LOCK:
        shared = _KERNEL_CACHE.get(key)
        if shared is None:
            client = KernelClient(metadata_path=str(path))
            client.open()
            shared = _SharedKernel(client)
            _KERNEL_CACHE[key] = shared
        shared.owners += 1
        return _KernelHandle(key, shared)
