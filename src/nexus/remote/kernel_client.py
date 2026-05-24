# ruff: noqa: ARG002
"""gRPC kernel client — replaces in-process PyKernel (nexus_runtime.so).

All profiles (embedded, full, cloud) now use this client to communicate
with the Rust kernel running as a separate `nexus-cluster` process.
The REMOTE profile already used gRPC via RPCTransport; this module
generalizes that pattern for local deployments where the kernel process
is spawned as a subprocess.

The KernelClient exposes the same method surface that Python code
previously called on PyKernel. Under the hood, typed RPCs are used for
content-heavy operations (Read/Write/Delete/BatchRead) and the generic
Call RPC for metadata/service operations.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.remote.rpc_transport import RPCTransport

logger = logging.getLogger(__name__)

# Default port for local kernel subprocess.
_DEFAULT_LOCAL_PORT = 2126
_KERNEL_BINARY_ENV = "NEXUS_KERNEL_BINARY"
_KERNEL_BINARY_CANDIDATES = ("nexus-cluster", "nexusd-cluster")
_KERNEL_DATA_DIR_FILE_FALLBACK_SUFFIX = ".kernel"


def _resolve_kernel_binary() -> str:
    """Return the Rust kernel binary to spawn.

    CI and packaged installs often provide the compatibility name
    ``nexus-cluster``. Local Cargo builds produce the actual bin target
    ``nexusd-cluster``. Accept both so real local E2E runs do not require a
    manual symlink.
    """
    configured = os.environ.get(_KERNEL_BINARY_ENV)
    if configured:
        return configured

    for binary_name in _KERNEL_BINARY_CANDIDATES:
        resolved = shutil.which(binary_name)
        if resolved:
            return resolved

    return _KERNEL_BINARY_CANDIDATES[0]


def _resolve_kernel_data_dir(metadata_path: str | None) -> str | None:
    """Return a directory path suitable for the Rust kernel data dir.

    Older Python-only runs used ``metadata_path`` as a database file. The Rust
    kernel reads ``NEXUS_DATA_DIR`` as a directory, so passing that legacy file
    path makes the subprocess panic before it can report a useful startup
    error. Keep normal directory/nonexistent paths unchanged, but route an
    existing file to a deterministic sidecar directory.
    """
    if not metadata_path:
        return None

    path = Path(metadata_path).expanduser()
    try:
        if path.exists() and path.is_file():
            return str(path.with_name(f"{path.name}{_KERNEL_DATA_DIR_FILE_FALLBACK_SUFFIX}"))
    except OSError:
        return metadata_path

    return metadata_path


class KernelClient:
    """gRPC-based kernel client — drop-in replacement for PyKernel.

    For local profiles (embedded/full/cloud), spawns ``nexus-cluster``
    as a subprocess and connects via loopback gRPC. For remote profiles,
    connects to an existing server.

    Provides the same method surface as the old PyKernel so existing
    Python callers continue to work unchanged.
    """

    def __init__(
        self,
        *,
        server_address: str | None = None,
        auth_token: str | None = None,
        metadata_path: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        self._metadata_path = metadata_path
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_file: IO[bytes] | None = None
        self._stderr_path: str | None = None
        self._transport: RPCTransport | None = None
        self._timeout = timeout
        self._auth_token = auth_token or ""
        self.requires_python_hooks = True
        self._hooks: dict[str, list[Any]] = {}

        if server_address:
            # Remote mode — connect to existing server.
            self._server_address = server_address
        else:
            # Local mode — will spawn subprocess on open().
            port = _find_free_port()
            self._server_address = f"127.0.0.1:{port}"

    # ── Lifecycle ──────────────────────────────────────────────────────

    def open(self) -> None:
        """Start kernel subprocess (if local) and establish gRPC channel."""
        if self._process is None and not self._is_remote():
            self._spawn_kernel()
        self._transport = RPCTransport(
            server_address=self._server_address,
            auth_token=self._auth_token,
            timeout=self._timeout,
        )
        # Wait for kernel to be ready.
        self._wait_ready()

    def close(self) -> None:
        """Shutdown kernel subprocess and close gRPC channel."""
        if self._transport:
            import contextlib

            with contextlib.suppress(Exception):
                self._transport.close()
            self._transport = None
        if self._process:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        if self._stderr_file is not None:
            import contextlib

            with contextlib.suppress(OSError):
                self._stderr_file.close()
            stderr_path = getattr(self, "_stderr_path", None)
            if stderr_path:
                with contextlib.suppress(OSError):
                    os.unlink(stderr_path)
            self._stderr_file = None

    def _is_remote(self) -> bool:
        return self._process is None and self._transport is not None

    def _spawn_kernel(self) -> None:
        """Spawn nexus-cluster as a subprocess."""
        kernel_binary = _resolve_kernel_binary()
        cmd = [kernel_binary]
        env = os.environ.copy()
        # Pass data directory if provided (Rust binary reads NEXUS_DATA_DIR).
        kernel_data_dir = _resolve_kernel_data_dir(self._metadata_path)
        if kernel_data_dir:
            env["NEXUS_DATA_DIR"] = kernel_data_dir
            if self._metadata_path and kernel_data_dir != self._metadata_path:
                logger.warning(
                    "Kernel metadata path %s is a file; using %s as NEXUS_DATA_DIR",
                    self._metadata_path,
                    kernel_data_dir,
                )
        env["NEXUS_BIND_ADDR"] = self._server_address
        env["NEXUS_NO_TLS"] = "true"  # Loopback, no TLS needed.
        env.setdefault("NEXUS_BOOTSTRAP_MODE", "dynamic")

        # Redirect stdout/stderr to temp files to avoid pipe buffer deadlock.
        # The OS pipe buffer (~65KB) fills up when the Rust binary emits
        # tracing output and nobody reads the pipe, blocking the process.
        import tempfile

        fd, stderr_path = tempfile.mkstemp(prefix="nexus-kernel-", suffix=".log")
        self._stderr_file = os.fdopen(fd, "wb")
        self._stderr_path = stderr_path
        self._process = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_file,
        )
        logger.info(
            "Spawned %s (pid=%d) at %s, log=%s",
            kernel_binary,
            self._process.pid,
            self._server_address,
            self._stderr_path,
        )

    def _wait_ready(self, timeout: float = 30.0) -> None:
        """Poll kernel health until ready."""
        assert self._transport is not None
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            # Check if subprocess crashed before wasting time on gRPC.
            if self._process is not None:
                rc = self._process.poll()
                if rc is not None:
                    stderr_tail = self._read_stderr_tail()
                    raise RuntimeError(
                        f"Kernel subprocess exited with code {rc} "
                        f"before becoming ready.\n{stderr_tail}"
                    )
            try:
                self._transport.ping()
                return
            except Exception as e:
                last_err = e
                time.sleep(0.1)
        stderr_tail = self._read_stderr_tail()
        raise TimeoutError(
            f"Kernel not ready after {timeout}s at {self._server_address}\n{stderr_tail}"
        ) from last_err

    def _read_stderr_tail(self, lines: int = 30) -> str:
        """Read the last N lines from the kernel stderr log file."""
        stderr_path = getattr(self, "_stderr_path", None)
        if stderr_path is None:
            return ""
        try:
            with open(stderr_path) as f:
                all_lines = f.readlines()
                tail = all_lines[-lines:]
                return "".join(tail)
        except OSError:
            return ""

    # ── Syscall interface ──────────────────────────────────────────────

    def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Generic Call RPC dispatch."""
        assert self._transport is not None
        return self._transport.call_rpc(method, params or {}, auth_token=self._auth_token)

    def sys_read(
        self,
        path: str,
        context: Any = None,
        timeout_ms: int = 0,
        offset: int = 0,
    ) -> Any:
        """Read file content via typed Read RPC."""
        assert self._transport is not None
        if timeout_ms != 5000 or offset:
            result = self._call(
                "sys_read",
                {
                    "path": path,
                    "timeout_ms": int(timeout_ms),
                    "offset": int(offset),
                },
            )
            if isinstance(result, dict):
                data = result.get("data")
                if data is None:
                    data = b""
                elif isinstance(data, str):
                    data = data.encode("utf-8")
                return _SysReadResult(
                    data=data,
                    content_id=result.get("content_id"),
                    gen=int(result.get("gen") or 0),
                    entry_type=int(result.get("entry_type") or 1),
                    stream_next_offset=result.get("stream_next_offset"),
                    post_hook_needed=bool(result.get("post_hook_needed"))
                    or self.hook_count("read") > 0,
                )
        content = self._transport.read_file(path, content_id="", read_timeout=self._timeout)
        return _SysReadResult(data=content, post_hook_needed=self.hook_count("read") > 0)

    def sys_read_raw(self, path: str, zone_id: str = ROOT_ZONE_ID) -> bytes:  # noqa: ARG002
        """Read raw file bytes for compatibility with versioning/parsers services."""
        assert self._transport is not None
        return self._transport.read_file(path, content_id="", read_timeout=self._timeout)

    def sys_write(
        self,
        path: str,
        context: Any = None,
        data: bytes = b"",
        offset: int = 0,
    ) -> Any:
        """Write file content via typed Write RPC."""
        assert self._transport is not None
        result = self._transport.write_file(path, data, content_id=None, read_timeout=self._timeout)
        return _SysWriteResult(
            content_id=result.get("content_id"),
            size=result.get("size", 0),
            gen=result.get("gen", 0),
        )

    def sys_stat(self, path: str, zone_id: str = ROOT_ZONE_ID) -> Any:
        """Stat a path — returns metadata dict or None on not-found.

        Enriches the Rust JSON response with ISO-8601 string fields:
        - modified_at / created_at: ISO strings from epoch-ms fields
        """
        try:
            result = self._call("sys_stat", {"path": path, "zone_id": zone_id})
        except Exception:
            # FileNotFound is raised as an RPC error — translate to None.
            return None
        if result is None:
            return None
        # Enrich with ISO-string timestamps that Python callers expect.
        # Callers that need datetime objects should parse via fromisoformat().
        if isinstance(result, dict):
            from datetime import UTC, datetime

            ms = result.get("modified_at_ms")
            if ms is not None and "modified_at" not in result:
                result["modified_at"] = datetime.fromtimestamp(ms / 1000.0, UTC).isoformat()
            ms = result.get("created_at_ms")
            if ms is not None and "created_at" not in result:
                result["created_at"] = datetime.fromtimestamp(ms / 1000.0, UTC).isoformat()
        return result

    def sys_setattr(self, path: str, **kwargs: Any) -> Any:
        """Set attributes on a path."""
        result = self._call("sys_setattr", {"path": path, **kwargs})
        if isinstance(result, dict):
            return _SysSetAttrResult(result)
        return result

    def sys_unlink(self, path: str, context: Any = None, recursive: bool = False) -> Any:
        """Delete a file/directory via Call RPC."""
        result = self._call("sys_unlink", {"path": path, "recursive": recursive})
        if isinstance(result, dict):
            return _SysUnlinkResult(result)
        return _SysUnlinkResult({})

    def sys_mkdir(
        self, path: str, context: Any = None, parents: bool = False, exist_ok: bool = True
    ) -> Any:
        """Create a directory."""
        result = self._call("sys_mkdir", {"path": path, "parents": parents, "exist_ok": exist_ok})
        if isinstance(result, dict):
            return _SysMkdirResult(result)
        return _SysMkdirResult({})

    def sys_rename(
        self,
        path: str,
        new_path: str,
        context: Any = None,
    ) -> Any:
        """Rename/move a file or directory."""
        result = self._call("sys_rename", {"path": path, "new_path": new_path})
        if isinstance(result, dict):
            return _SysRenameResult(result)
        return _SysRenameResult({})

    def sys_copy(
        self,
        src: str,
        dst: str,
        context: Any = None,
    ) -> Any:
        """Copy a file."""
        result = self._call("sys_copy", {"src": src, "dst": dst})
        if isinstance(result, dict):
            return _SysCopyResult(result)
        return _SysCopyResult({})

    def sys_readdir(
        self,
        path: str,
        zone_id: str = ROOT_ZONE_ID,
        is_admin: bool = False,
    ) -> list[tuple[str, int]]:
        """List directory contents — returns list of (path, entry_type) tuples."""
        result = self._call(
            "sys_readdir",
            {"path": path, "zone_id": zone_id},
        )
        if result is None:
            return []
        if isinstance(result, list):
            entries: list[tuple[str, int]] = []
            for e in result:
                if isinstance(e, dict):
                    entries.append((e.get("name", ""), e.get("entry_type", 0)))
                elif isinstance(e, (list, tuple)) and len(e) >= 2:
                    entries.append((e[0], e[1]))
            return entries
        return []

    def sys_lock(
        self,
        path: str,
        lock_id: str = "",
        mode: int = 1,
        max_holders: int = 1,
        ttl_secs: int = 60,
        timeout_ms: int = 5000,
        **_kwargs: Any,
    ) -> Any:
        """Acquire advisory lock."""
        result = self._call(
            "sys_lock",
            {
                "path": path,
                "lock_id": lock_id,
                "timeout_ms": timeout_ms,
            },
        )
        if isinstance(result, dict):
            return result.get("lock_id", "")
        return result

    def sys_unlock(self, path: str, lock_id: str = "", force: bool = False) -> Any:
        """Release advisory lock."""
        return self._call("sys_unlock", {"path": path, "lock_id": lock_id, "force": force})

    def read_batch(
        self,
        items: list[tuple[str, int, int | None]],
        context: Any = None,
    ) -> list[Any]:
        """Batch read — loop individual typed Read RPCs.

        Uses the existing typed Read RPC per item (same as sys_read).
        Returns list of _SysReadResult in same order as items.
        On per-item failure, surfaces error_kind so the caller
        (nexus_fs_content.read_batch) can distinguish not_found from
        other errors and implement partial/strict mode correctly.
        """
        from nexus.contracts.exceptions import NexusFileNotFoundError, NexusPermissionError

        results: list[Any] = []
        for path, offset, _count in items:
            try:
                results.append(self.sys_read(path, offset=offset))
            except NexusFileNotFoundError:
                results.append(
                    _SysReadResult(data=None, error_kind="not_found", error_message=path)
                )
            except NexusPermissionError as e:
                results.append(
                    _SysReadResult(data=None, error_kind="permission_denied", error_message=str(e))
                )
            except Exception as e:
                results.append(
                    _SysReadResult(data=None, error_kind="io_error", error_message=str(e))
                )
        return results

    def stat_batch(self, paths: list[str], zone_id: str = ROOT_ZONE_ID) -> list[Any]:
        """Batch stat multiple paths — returns list of stat dicts or None."""
        result = self._call("stat_batch", {"paths": paths, "zone_id": zone_id})
        if isinstance(result, list):
            return result
        return [None] * len(paths)

    def sys_watch(self, path: str, timeout_ms: int = 30000) -> Any:
        """Watch for file changes (blocking)."""
        return self._call("sys_watch", {"path": path, "timeout_ms": timeout_ms})

    # ── Service registry ───────────────────────────────────────────────

    def service_start_all(self, timeout_ms: int = 30000) -> None:
        self._call("service_start_all", {"timeout_ms": timeout_ms})

    def service_mark_bootstrapped(self) -> None:
        self._call("service_mark_bootstrapped", {})

    def service_lookup(self, name: str) -> Any:
        """Return None — services are kernel-internal in subprocess mode."""
        return None

    def service_swap(
        self, name: str, instance: Any, exports: list[str], timeout_ms: int = 5000
    ) -> None:
        self._call(
            "service_swap",
            {"name": name, "exports": exports, "timeout_ms": timeout_ms},
        )

    def service_enlist(
        self,
        name: str,
        instance: Any,
        exports: list[str],
        allow_overwrite: bool = False,
    ) -> None:
        """No-op — service registration is kernel-internal in subprocess mode."""
        pass

    def service_stop_all(self, timeout_ms: int = 10000) -> None:
        self._call("service_stop_all", {"timeout_ms": timeout_ms})

    def service_close_all(self) -> None:
        self._call("service_close_all", {})

    # ── Hook dispatch (stays in Python) ────────────────────────────────
    # The subprocess kernel cannot hold Python hook objects. Keep a local
    # mirror so NexusFS can run Python permission/redaction hooks before or
    # after typed kernel RPCs.

    def hook_count(self, op: str) -> int:
        return len(self._hooks.get(op, ()))

    def dispatch_post_hooks(self, op: str, ctx: Any) -> None:
        method = f"on_post_{op}"
        for hook in tuple(self._hooks.get(op, ())):
            fn = getattr(hook, method, None)
            if callable(fn):
                fn(ctx)

    def dispatch_pre_hooks(self, op: str, ctx: Any) -> None:
        method = f"on_pre_{op}"
        for hook in tuple(self._hooks.get(op, ())):
            fn = getattr(hook, method, None)
            if callable(fn):
                fn(ctx)

    def register_hook(self, op: str, hook: Any, *args: Any, **kwargs: Any) -> None:
        hooks = self._hooks.setdefault(op, [])
        if hook not in hooks:
            hooks.append(hook)

    def unregister_hook(self, op: str, hook: Any) -> bool:
        hooks = self._hooks.get(op)
        if not hooks:
            return False
        try:
            hooks.remove(hook)
        except ValueError:
            return False
        if not hooks:
            self._hooks.pop(op, None)
        return True

    def set_hook_count(self, op: str, count: int) -> None:
        """No-op — hook bitmap is kernel-internal in subprocess mode."""
        pass

    def dispatch_pre_hooks_batch_stat(
        self, paths: list[str], rust_ctx: Any, permission: Any
    ) -> list[bool]:
        """Allow all; callers with Python OperationContext dispatch per-path hooks."""
        return [True] * len(paths)

    # ── Convenience wrappers (derived from syscalls) ──────────────────

    def is_directory(self, path: str, zone_id: str = "") -> bool:
        """Check if path is a directory via sys_stat."""
        result = self.sys_stat(path, zone_id=zone_id or ROOT_ZONE_ID)
        if result is None:
            return False
        if isinstance(result, dict):
            return bool(result.get("is_directory", False))
        return bool(getattr(result, "is_directory", False))

    def get_content_id(self, path: str, zone_id: str = "") -> str | None:
        """Get content hash via sys_stat."""
        result = self.sys_stat(path, zone_id=zone_id or ROOT_ZONE_ID)
        if result is None:
            return None
        if isinstance(result, dict):
            return result.get("content_id")
        return getattr(result, "content_id", None)

    def exists_batch(self, paths: list[str], zone_id: str = "") -> list[bool]:
        """Batch existence check via sys_stat."""
        zid = zone_id or ROOT_ZONE_ID
        return [self.sys_stat(p, zone_id=zid) is not None for p in paths]

    def get_mount_points(self) -> list[str]:
        """Return zone-canonical mount points from the subprocess kernel."""
        result = self._call("get_mount_points", {})
        if isinstance(result, list):
            return [str(p) for p in result]
        return []

    def get_top_level_mounts(self, zone_id: str = "") -> list[str]:
        """Return top-level mounts via sys_readdir on /."""
        result = self.sys_readdir("/", zone_id=zone_id or ROOT_ZONE_ID)
        if not result:
            return []
        # sys_readdir now returns list of (name, entry_type) tuples
        return [name for name, _etype in result]

    def metastore_list_paginated(
        self,
        prefix: str,
        recursive: bool = True,
        limit: int = 100000,
        cursor: Any = None,
    ) -> dict[str, Any]:
        """Paginated list via sys_readdir — returns {items, next_cursor, has_more, total_count}.

        Items are FileMetadata objects (callers access .path, .zone_id etc.).
        Uses stat_batch to populate metadata when available.
        """
        from datetime import UTC, datetime

        from nexus.contracts.metadata import DT_DIR, DT_MOUNT, FileMetadata

        def _normalize_dir(path: str) -> str:
            if not path:
                return "/"
            if not path.startswith("/"):
                path = f"/{path}"
            if path != "/":
                path = path.rstrip("/")
            return path

        def _dt_from_ms(value: Any) -> datetime | None:
            if value is None:
                return None
            try:
                return datetime.fromtimestamp(int(value) / 1000.0, UTC)
            except (TypeError, ValueError, OSError):
                return None

        root = _normalize_dir(prefix)
        dir_entry_types = {DT_DIR, DT_MOUNT}
        entries: list[tuple[str, int]] = []
        seen_entries: set[str] = set()
        seen_dirs: set[str] = set()
        pending_dirs: list[str] = [root]

        while pending_dirs:
            current = pending_dirs.pop()
            if current in seen_dirs:
                continue
            seen_dirs.add(current)
            for name, etype in self.sys_readdir(current):
                if not name or name == current:
                    continue
                if name in seen_entries:
                    continue
                seen_entries.add(name)
                entries.append((name, etype))
                if etype in dir_entry_types and recursive:
                    pending_dirs.append(_normalize_dir(name))
            if not recursive:
                break

        entries.sort(key=lambda item: item[0])

        # Apply cursor-based pagination: skip entries until we pass the cursor path
        if cursor:
            entries = [(name, etype) for name, etype in entries if name > cursor]

        total = len(entries)
        page = entries[:limit]
        has_more = total > limit

        # Convert to FileMetadata objects — callers access .path, .zone_id, etc.
        # Enrich with stat data when available for size/content_id/version.
        paths = [name for name, _ in page]
        stats: list[Any] = []
        if paths:
            try:
                stats = self.stat_batch(paths)
            except Exception:
                stats = [None] * len(paths)
        if len(stats) != len(paths):
            stats = [None] * len(paths)

        items: list[FileMetadata] = []
        for i, (name, etype) in enumerate(page):
            st = stats[i] if i < len(stats) else None
            if isinstance(st, dict):
                items.append(
                    FileMetadata(
                        path=st.get("path", name),
                        size=st.get("size", 0),
                        content_id=st.get("content_id"),
                        mime_type=st.get("mime_type"),
                        created_at=_dt_from_ms(st.get("created_at_ms")),
                        modified_at=_dt_from_ms(st.get("modified_at_ms")),
                        entry_type=st.get("entry_type", etype),
                        version=st.get("version", 1),
                        gen=st.get("gen", 0),
                        zone_id=st.get("zone_id"),
                        owner_id=st.get("owner_id"),
                        last_writer_address=st.get("last_writer_address"),
                        link_target=st.get("link_target"),
                    )
                )
            else:
                items.append(FileMetadata(path=name, size=0, entry_type=etype))

        next_cursor = page[-1][0] if has_more and page else None
        return {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total_count": total,
        }

    def service_unregister(self, name: str) -> None:
        """No-op — services are kernel-internal in subprocess mode."""
        pass

    # ── Trie (resolver registration) ──────────────────────────────────
    # Trie is Python-side only (DispatchMixin). In subprocess mode the
    # Rust kernel has no trie — return no-ops / None.

    def trie_register(self, pattern: str, idx: int) -> None:
        pass

    def trie_lookup(self, path: str) -> Any:
        return None

    def trie_unregister(self, idx: int) -> Any:
        return None

    # ── Xattr (file metadata side-car) ──────────────────────────────────

    def get_xattr(self, path: str, key: str) -> str | None:
        """Get extended attribute via Rust kernel metastore."""
        try:
            result = self._call("get_xattr", {"path": path, "key": key})
            return str(result) if result is not None else None
        except Exception:
            return None

    def set_xattr(self, path: str, key: str, value: str) -> None:
        """Set extended attribute via Rust kernel metastore."""
        import contextlib

        with contextlib.suppress(Exception):
            self._call("set_xattr", {"path": path, "key": key, "value": value})

    def get_xattr_bulk(self, paths: list[str], key: str) -> dict[str, str | None]:
        """Bulk get extended attribute via Rust kernel metastore."""
        try:
            result = self._call("get_xattr_bulk", {"paths": paths, "key": key})
            if isinstance(result, dict):
                return result
        except Exception:
            pass
        return dict.fromkeys(paths)

    # ── IPC: Pipes ─────────────────────────────────────────────────────

    def create_pipe(self, path: str, capacity: int = 64) -> None:
        self._call("create_pipe", {"path": path, "capacity": capacity})

    def destroy_pipe(self, path: str) -> None:
        self._call("destroy_pipe", {"path": path})

    def close_pipe(self, path: str) -> None:
        self._call("close_pipe", {"path": path})

    def has_pipe(self, path: str) -> Any:
        return self._call("has_pipe", {"path": path})

    def close_all_pipes(self) -> None:
        self._call("close_all_pipes", {})

    # ── IPC: Streams ───────────────────────────────────────────────────

    def create_stream(self, path: str, capacity: int = 1024) -> None:
        self._call("create_stream", {"path": path, "capacity": capacity})

    def has_stream(self, path: str) -> Any:
        return self._call("has_stream", {"path": path})

    def stream_read_at_blocking(
        self, path: str, offset: int, timeout_ms: int = 30000
    ) -> tuple[bytes, int]:
        result = self._call(
            "stream_read_at_blocking",
            {"path": path, "offset": offset, "timeout_ms": timeout_ms},
        )
        return result["data"], result["next_offset"]

    def stream_write_nowait(self, path: str, data: bytes) -> Any:
        return self._call("stream_write_nowait", {"path": path, "data": data})

    def stream_read_at(self, path: str, offset: int) -> Any:
        return self._call("stream_read_at", {"path": path, "offset": offset})

    def stream_collect_all(self, path: str) -> bytes:
        result = self._call("stream_collect_all", {"path": path})
        return result if isinstance(result, bytes) else b""

    def close_stream(self, path: str) -> None:
        self._call("close_stream", {"path": path})

    def destroy_stream(self, path: str) -> None:
        self._call("destroy_stream", {"path": path})

    def close_all_streams(self) -> None:
        """Close all streams (shutdown)."""
        # No batch close RPC — streams are cleaned up when the subprocess exits.
        pass

    # ── Metastore path ─────────────────────────────────────────────────

    def set_metastore_path(self, path: str) -> None:
        """Set metastore path — handled at spawn time for subprocess."""
        # For subprocess mode, this was already passed via env.
        # For remote mode, the server manages its own metastore.
        self._metadata_path = path

    # ── Misc kernel methods ────────────────────────────────────────────

    def set_vfs_lock(self, lock: Any) -> None:
        """No-op — VFS lock is kernel-internal."""
        pass

    def register_native_hook(self, hook: Any) -> None:
        """No-op — native hooks are wired inside the kernel process."""
        pass

    def set_permission_provider(self, provider: Any) -> None:
        """No-op — permission provider lives inside the kernel process."""
        pass

    def write_batch(self, files: list[tuple[str, bytes]], context: Any = None) -> list[Any]:
        """Batch write multiple files."""
        import base64

        encoded_files = []
        for path, data in files:
            encoded_files.append(
                [path, {"__type__": "bytes", "data": base64.b64encode(data).decode()}]
            )
        result = self._call("write_batch", {"files": encoded_files})
        if isinstance(result, list):
            return [_BatchWriteItemResult(r) if isinstance(r, dict) else r for r in result]
        return []

    @property
    def agent_registry(self) -> Any:
        """Return agent registry proxy."""
        return _AgentRegistryProxy(self)


# ── Result types ───────────────────────────────────────────────────────


class _SysReadResult:
    """Matches Rust SysReadResult field names (SSOT).

    Fields: data, content_id, gen, entry_type, stream_next_offset,
    post_hook_needed, error_kind, error_message — from rust/kernel/src/kernel/mod.rs.
    """

    __slots__ = (
        "data",
        "content_id",
        "gen",
        "entry_type",
        "stream_next_offset",
        "post_hook_needed",
        "error_kind",
        "error_message",
    )

    def __init__(
        self,
        data: bytes | None = b"",
        content_id: str | None = None,
        gen: int = 0,
        entry_type: int = 1,
        stream_next_offset: int | None = None,
        error_kind: str = "",
        error_message: str = "",
        post_hook_needed: bool = False,
    ) -> None:
        self.data = data
        self.content_id = content_id
        self.gen = gen
        self.entry_type = entry_type
        self.stream_next_offset = stream_next_offset
        self.post_hook_needed = post_hook_needed
        self.error_kind = error_kind
        self.error_message = error_message


class _SysWriteResult:
    """Mimics PySysWriteResult from the old PyO3 binding."""

    __slots__ = (
        "hit",
        "content_id",
        "post_hook_needed",
        "version",
        "gen",
        "size",
        "is_new",
        "old_content_id",
        "old_size",
        "old_version",
        "old_modified_at_ms",
    )

    def __init__(self, content_id: str | None = None, size: int = 0, gen: int = 0) -> None:
        self.hit = True
        self.content_id = content_id
        self.post_hook_needed = True
        self.version = 1
        self.gen = gen
        self.size = size
        self.is_new = False
        self.old_content_id: str | None = None
        self.old_size: int | None = None
        self.old_version: int | None = None
        self.old_modified_at_ms: int | None = None


class _SysMkdirResult:
    """Result wrapper for sys_mkdir Call RPC response."""

    __slots__ = ("hit", "post_hook_needed")

    def __init__(self, d: dict[str, Any] | None = None) -> None:
        d = d or {}
        self.hit = d.get("hit", True)
        self.post_hook_needed = d.get("post_hook_needed", False)


class _SysUnlinkResult:
    """Result wrapper for sys_unlink Call RPC response."""

    __slots__ = ("hit", "post_hook_needed", "entry_type", "path", "content_id", "size")

    def __init__(self, d: dict[str, Any] | None = None) -> None:
        d = d or {}
        self.hit = d.get("hit", True)
        self.post_hook_needed = d.get("post_hook_needed", False)
        self.entry_type = d.get("entry_type", 0)
        self.path = d.get("path", "")
        self.content_id = d.get("content_id")
        self.size = d.get("size", 0)


class _SysRenameResult:
    """Result wrapper for sys_rename Call RPC response."""

    __slots__ = (
        "hit",
        "success",
        "post_hook_needed",
        "is_directory",
        "old_content_id",
        "old_size",
        "old_version",
        "old_modified_at_ms",
    )

    def __init__(self, d: dict[str, Any] | None = None) -> None:
        d = d or {}
        self.hit = d.get("hit", True)
        self.success = d.get("success", True)
        self.post_hook_needed = d.get("post_hook_needed", False)
        self.is_directory = d.get("is_directory", False)
        self.old_content_id = d.get("old_content_id")
        self.old_size = d.get("old_size")
        self.old_version = d.get("old_version")
        self.old_modified_at_ms = d.get("old_modified_at_ms")


class _SysCopyResult:
    """Result wrapper for sys_copy Call RPC response."""

    __slots__ = ("hit", "post_hook_needed", "dst_path", "content_id", "size", "version", "gen")

    def __init__(self, d: dict[str, Any] | None = None) -> None:
        d = d or {}
        self.hit = d.get("hit", True)
        self.post_hook_needed = d.get("post_hook_needed", False)
        self.dst_path = d.get("dst_path", "")
        self.content_id = d.get("content_id")
        self.size = d.get("size", 0)
        self.version = d.get("version", 1)
        self.gen = d.get("gen", 0)


class _SysSetAttrResult:
    """Result wrapper for sys_setattr Call RPC response."""

    __slots__ = ("path", "created", "entry_type")

    def __init__(self, d: dict[str, Any] | None = None) -> None:
        d = d or {}
        self.path = d.get("path", "")
        self.created = d.get("created", False)
        self.entry_type = d.get("entry_type", 0)


class _BatchWriteItemResult:
    """Result wrapper for individual write_batch item."""

    __slots__ = ("content_id", "size", "gen", "version")

    def __init__(self, d: dict[str, Any] | None = None) -> None:
        d = d or {}
        self.content_id = d.get("content_id")
        self.size = d.get("size", 0)
        self.gen = d.get("gen", 0)
        self.version = d.get("version", 1)


class _AgentRegistryProxy:
    """Proxy for kernel AgentRegistry operations via gRPC."""

    def __init__(self, client: KernelClient) -> None:
        self._client = client

    @staticmethod
    def _descriptor(raw: Any) -> Any:
        if raw is None or not isinstance(raw, dict):
            return raw

        data = dict(raw)
        state = data.get("state")
        if isinstance(state, str):
            from nexus.contracts.process_types import AgentState

            with contextlib.suppress(ValueError):
                data["state"] = AgentState(state.lower())

        kind = data.get("kind")
        if isinstance(kind, str):
            from nexus.contracts.process_types import AgentKind

            with contextlib.suppress(ValueError):
                data["kind"] = AgentKind(kind.lower())

        raw_labels = data.get("labels")
        labels: dict[str, Any] = raw_labels if isinstance(raw_labels, dict) else {}
        raw_capabilities = labels.get("capabilities", "")
        if isinstance(raw_capabilities, str):
            data["capabilities"] = [
                capability for capability in raw_capabilities.split(",") if capability
            ]
        elif isinstance(raw_capabilities, list):
            data["capabilities"] = raw_capabilities
        else:
            data["capabilities"] = []

        return SimpleNamespace(**data)

    def register(self, **kwargs: Any) -> Any:
        return self._descriptor(self._client._call("agent_register", kwargs))

    def register_external(
        self,
        name: str,
        owner_id: str,
        zone_id: str,
        *,
        connection_id: str,
        host_pid: int | None = None,
        remote_addr: str | None = None,
        protocol: str = "grpc",
        parent_pid: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Any:
        return self._descriptor(
            self._client._call(
                "agent_register_external",
                {
                    "name": name,
                    "owner_id": owner_id,
                    "zone_id": zone_id,
                    "connection_id": connection_id,
                    "host_pid": host_pid,
                    "remote_addr": remote_addr,
                    "protocol": protocol,
                    "parent_pid": parent_pid,
                    "labels": labels or {},
                },
            )
        )

    def unregister(self, pid: str) -> Any:
        return self._client._call("agent_unregister", {"pid": pid})

    def unregister_external(self, pid: str) -> None:
        self._client._call("agent_unregister_external", {"pid": pid})

    def get(self, pid: str) -> Any:
        return self._descriptor(self._client._call("agent_get", {"pid": pid}))

    def signal(self, pid: str, sig: Any, *, payload: dict[str, Any] | None = None) -> Any:
        return self._descriptor(
            self._client._call(
                "agent_signal",
                {
                    "pid": pid,
                    "sig": str(sig),
                    "payload": payload or {},
                },
            )
        )

    def update_state(self, pid: str, state: Any) -> Any:
        return self._descriptor(
            self._client._call(
                "agent_update_state",
                {
                    "pid": pid,
                    "state": str(state),
                },
            )
        )

    def heartbeat(self, pid: str) -> Any:
        return self._descriptor(self._client._call("agent_heartbeat", {"pid": pid}))

    def list_agents(self) -> Any:
        return self.list_processes()

    def list_processes(
        self,
        *,
        zone_id: str | None = None,
        owner_id: str | None = None,
        kind: Any | None = None,
        state: Any | None = None,
    ) -> list[Any]:
        raw = self._client._call(
            "agent_list",
            {
                "zone_id": zone_id,
                "owner_id": owner_id,
                "kind": str(kind) if kind is not None else None,
                "state": str(state) if state is not None else None,
            },
        )
        if not isinstance(raw, list):
            return []
        return [self._descriptor(item) for item in raw]


# ── Helpers ────────────────────────────────────────────────────────────


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    import socket

    reserved_ports: set[int] = set()
    for env_name in ("NEXUS_GRPC_PORT", "NEXUS_APPROVALS_GRPC_PORT"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            with contextlib.suppress(ValueError):
                reserved_ports.add(int(raw))

    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port: int = s.getsockname()[1]
        if port not in reserved_ports:
            return port
