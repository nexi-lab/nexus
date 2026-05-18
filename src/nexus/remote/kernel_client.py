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

import logging
import os
import signal
import subprocess
import time
from typing import IO, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.remote.rpc_transport import RPCTransport

logger = logging.getLogger(__name__)

# Default port for local kernel subprocess.
_DEFAULT_LOCAL_PORT = 2126


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
        cmd = ["nexus-cluster"]
        env = os.environ.copy()
        # Pass data directory if provided (Rust binary reads NEXUS_DATA_DIR).
        if self._metadata_path:
            env["NEXUS_DATA_DIR"] = self._metadata_path
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
            "Spawned nexus-cluster (pid=%d) at %s, log=%s",
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
        context: dict[str, Any] | None = None,
        timeout_ms: int = 0,
        offset: int = 0,
    ) -> Any:
        """Read file content via typed Read RPC."""
        assert self._transport is not None
        content = self._transport.read_file(path, content_id="", read_timeout=self._timeout)
        # Return a result object matching the old PySysReadResult shape.
        return _SysReadResult(content=content)

    def sys_write(
        self,
        path: str,
        context: dict[str, Any] | None = None,
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
        """Stat a path — returns metadata dict."""
        return self._call("sys_stat", {"path": path, "zone_id": zone_id})

    def sys_setattr(self, path: str, **kwargs: Any) -> Any:
        """Set attributes on a path."""
        return self._call("sys_setattr", {"path": path, **kwargs})

    def sys_unlink(self, path: str, context: dict[str, Any] | None = None) -> Any:
        """Delete a file/directory."""
        assert self._transport is not None
        return self._transport.delete_file(path, recursive=False)

    def sys_mkdir(self, path: str, context: dict[str, Any] | None = None) -> Any:
        """Create a directory."""
        return self._call("sys_mkdir", {"path": path, **(context or {})})

    def sys_rename(
        self,
        path: str,
        new_path: str,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Rename/move a file or directory."""
        return self._call("sys_rename", {"path": path, "new_path": new_path, **(context or {})})

    def sys_copy(
        self,
        src: str,
        dst: str,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Copy a file."""
        return self._call("sys_copy", {"src": src, "dst": dst, **(context or {})})

    def sys_readdir(
        self,
        path: str,
        context: dict[str, Any] | None = None,
        page_size: int = 0,
        page_token: str = "",
    ) -> Any:
        """List directory contents."""
        return self._call(
            "sys_readdir",
            {"path": path, "page_size": page_size, "page_token": page_token},
        )

    def sys_lock(
        self,
        path: str,
        context: dict[str, Any] | None = None,
        timeout_ms: int = 5000,
    ) -> Any:
        """Acquire advisory lock."""
        return self._call("sys_lock", {"path": path, "timeout_ms": timeout_ms, **(context or {})})

    def sys_unlock(self, path: str, lock_id: str = "", force: bool = False) -> Any:
        """Release advisory lock."""
        return self._call("sys_unlock", {"path": path, "lock_id": lock_id, "force": force})

    def sys_read_batch(
        self,
        items: list[tuple[str, int, int | None]],
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Batch read via generic Call RPC (no typed BatchRead endpoint)."""
        return self._call(
            "sys_read_batch",
            {"items": [(path, offset, count) for path, offset, count in items]},
        )

    def stat_batch(self, paths: list[str], zone_id: str = ROOT_ZONE_ID) -> Any:
        """Batch stat multiple paths."""
        return self._call("stat_batch", {"paths": paths, "zone_id": zone_id})

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
    # Python-level hooks are dispatched by the Python DispatchMixin.
    # The kernel client provides no-op stubs for hook_count and
    # dispatch_post_hooks since native Rust hooks run inside the kernel
    # process automatically.

    def hook_count(self, op: str) -> int:
        """Return 0 — native hooks run in-kernel, Python hooks use DispatchMixin."""
        return 0

    def dispatch_post_hooks(self, op: str, ctx: dict[str, Any]) -> None:
        """No-op — native hooks fire inside the kernel process."""
        pass

    def dispatch_pre_hooks(self, op: str, ctx: dict[str, Any]) -> None:
        """No-op — native hooks fire inside the kernel process."""
        pass

    def register_hook(self, *args: Any, **kwargs: Any) -> None:
        """No-op — hooks are kernel-internal in subprocess mode."""
        pass

    def unregister_hook(self, op: str, hook: Any) -> bool:
        """No-op — hooks are kernel-internal in subprocess mode."""
        return False

    def set_hook_count(self, op: str, count: int) -> None:
        """No-op — hook bitmap is kernel-internal in subprocess mode."""
        pass

    def dispatch_pre_hooks_batch_stat(
        self, paths: list[str], rust_ctx: Any, permission: Any
    ) -> list[bool]:
        """Allow all — permission hooks run inside the kernel process."""
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
        """Return empty — mount points are kernel-internal."""
        return []

    def get_top_level_mounts(self, zone_id: str = "") -> list[str]:
        """Return top-level mounts via sys_readdir on /."""
        result = self.sys_readdir("/")
        if result is None:
            return []
        if isinstance(result, list):
            return [e.get("name", e) if isinstance(e, dict) else str(e) for e in result]
        return []

    def metastore_list_paginated(
        self,
        prefix: str,
        recursive: bool = True,
        limit: int = 100000,
        cursor: Any = None,
    ) -> dict[str, Any]:
        """Paginated list via sys_readdir — returns {"items": [...]}."""
        result = self.sys_readdir(prefix)
        items: list[dict[str, Any]] = []
        if isinstance(result, list):
            for e in result:
                if isinstance(e, dict):
                    items.append(e)
                else:
                    items.append({"name": str(e)})
        return {"items": items[:limit], "next_cursor": None}

    def service_unregister(self, name: str) -> None:
        """No-op — services are kernel-internal in subprocess mode."""
        pass

    # ── Trie (resolver registration) ──────────────────────────────────

    def trie_register(self, pattern: str, idx: int) -> None:
        self._call("trie_register", {"pattern": pattern, "idx": idx})

    def trie_lookup(self, path: str) -> Any:
        return self._call("trie_lookup", {"path": path})

    def trie_unregister(self, idx: int) -> Any:
        return self._call("trie_unregister", {"idx": idx})

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

    def write_batch(
        self, files: list[tuple[str, bytes]], context: dict[str, Any] | None = None
    ) -> Any:
        """Batch write multiple files."""
        return self._call(
            "write_batch",
            {"files": [(p, None) for p, _ in files], **(context or {})},
        )

    def agent_registry(self) -> Any:
        """Return agent registry proxy."""
        return _AgentRegistryProxy(self)


# ── Result types ───────────────────────────────────────────────────────


class _SysReadResult:
    """Mimics PySysReadResult from the old PyO3 binding."""

    __slots__ = ("content", "content_id", "size", "gen", "hit")

    def __init__(
        self,
        content: bytes = b"",
        content_id: str | None = None,
        size: int = 0,
        gen: int = 0,
    ) -> None:
        self.content = content
        self.content_id = content_id
        self.size = size
        self.gen = gen
        self.hit = content is not None


class _SysWriteResult:
    """Mimics PySysWriteResult from the old PyO3 binding."""

    __slots__ = ("content_id", "size", "gen")

    def __init__(self, content_id: str | None = None, size: int = 0, gen: int = 0) -> None:
        self.content_id = content_id
        self.size = size
        self.gen = gen


class _AgentRegistryProxy:
    """Thin proxy for agent registry operations via gRPC."""

    def __init__(self, client: KernelClient) -> None:
        self._client = client

    def register(self, **kwargs: Any) -> Any:
        return self._client._call("agent_register", kwargs)

    def unregister(self, pid: str) -> Any:
        return self._client._call("agent_unregister", {"pid": pid})

    def list_agents(self) -> Any:
        return self._client._call("agent_list", {})


# ── Helpers ────────────────────────────────────────────────────────────


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port
