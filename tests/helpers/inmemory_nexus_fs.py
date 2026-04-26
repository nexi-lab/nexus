"""In-memory NexusFS stub for unit tests.

Implements just the four sys_* calls used by VFS-backed brick stores
(``MetastoreMountStore``, ``MetastoreNamespaceStore``,
``MetastoreVersionStore``). Avoids the cost of constructing the full
real NexusFS for tests that only exercise small file-keyed stores.

Mirrors the kernel's VFS contract closely enough to reach all branches
in the stores: missing-file → ``FileNotFoundError``; ``sys_read`` returns
``{"hit": True, "content": bytes}``; ``sys_readdir`` returns basenames
under the queried directory.
"""

from __future__ import annotations

from typing import Any


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
