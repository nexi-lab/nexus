"""KernelVFSAdapter — async adapter wrapping sync NexusFS for IPC.

Routes all IPC storage operations through the kernel VFS (NexusFS),
gaining PathRouter routing, ReBAC permission checks, MetastoreABC
metadata tracking, EventLog auditing, content caching, and Raft
replication — none of which the old RecordStoreStorageDriver provided.

Uses a lazy-bind pattern: created with ``zone_id`` only during
``_create_bricks()``, then ``bind(nexus_fs)`` is called once NexusFS
exists in ``_boot_wired_services()``.

Issue: #1178
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.types import VFSOperations

logger = logging.getLogger(__name__)


@dataclass
class KernelVFSAdapter:
    """Async IPC storage adapter delegating to the kernel VFS (NexusFS).

    Satisfies the ``VFSOperations`` protocol from
    ``nexus.bricks.ipc.protocols`` (async methods with ``zone_id``).

    Parameters
    ----------
    zone_id:
        Default zone for constructing ``OperationContext``.
    """

    zone_id: str
    _nx: Any = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Lazy binding
    # ------------------------------------------------------------------

    def bind(self, nexus_fs: "VFSOperations") -> None:
        """Bind to a live NexusFS instance (called after kernel init)."""
        self._nx = nexus_fs
        logger.info("[IPC] KernelVFSAdapter bound to NexusFS (zone=%s)", self.zone_id)

    @property
    def is_bound(self) -> bool:
        return self._nx is not None

    # ------------------------------------------------------------------
    # Context helper
    # ------------------------------------------------------------------

    def _ctx(self, zone_id: str) -> Any:
        """Build an ``OperationContext`` for IPC system operations."""
        from nexus.contracts.types import OperationContext

        return OperationContext(
            user_id="system",
            groups=[],
            zone_id=zone_id,
            is_system=True,
        )

    def _require_bound(self) -> None:
        if self._nx is None:
            raise RuntimeError("KernelVFSAdapter.bind(nexus_fs) has not been called yet")

    # ------------------------------------------------------------------
    # VFSOperations protocol (async)
    # ------------------------------------------------------------------

    async def sys_read(self, path: str, zone_id: str) -> bytes:
        self._require_bound()
        ctx = self._ctx(zone_id)
        try:
            result: bytes = await self._nx.sys_read(path, context=ctx)
            return result
        except AttributeError:
            # Rust kernel not initialized (embedded/test mode without full boot).
            # Fall back to the DLC read path which routes directly to the backend
            # without requiring the kernel — same as _read_via_dlc in nexus_fs.py.
            return bytes(self._nx._read_via_dlc(path, True, ctx))

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        self._require_bound()
        ctx = self._ctx(zone_id)
        await self._nx.write(path, data, context=ctx)

    async def list_dir(self, path: str, zone_id: str) -> list[str]:  # noqa: ARG002
        self._require_bound()
        # Route through PathRouter directly to the LocalConnector backend for
        # enumeration — Raft prefix scans may not index /agents entries.
        # Then cross-reference each entry against the metastore to filter out
        # logically-deleted files: nexus_fs.sys_unlink removes the metastore
        # entry immediately but defers physical deletion to background GC
        # (HDFS/GFS pattern). Without this filter, sys_unlink'd files would
        # still appear in listing results.
        import asyncio

        route = self._nx.router.route(path, is_admin=True, check_write=False)
        raw: list[str] = await asyncio.to_thread(route.backend.list_dir, route.backend_path)
        prefix = path.rstrip("/")
        result = []
        for name in raw:
            if "/" in name:
                continue
            full_path = f"{prefix}/{name}"
            meta = route.metastore.get(full_path)
            if meta is None and not route.metastore.is_implicit_directory(full_path):
                continue  # physically present but logically deleted — skip
            result.append(name)
        return result

    async def count_dir(self, path: str, zone_id: str) -> int:
        entries = await self.list_dir(path, zone_id)
        return len(entries)

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        self._require_bound()
        ctx = self._ctx(zone_id)

        # sys_rename is metadata-only (nexus_fs.py:3325 — "does NOT copy file
        # content"). For path-based backends (LocalConnector, which uses
        # context.backend_path not content hash for storage), we must also
        # physically move the file so reads from dst work.
        # Pre-compute physical paths BEFORE renaming while src still routes correctly.
        import asyncio

        src_phys = dst_phys = None
        try:
            src_route = self._nx.router.route(src, is_admin=True, check_write=False)
            dst_route = self._nx.router.route(dst, is_admin=True, check_write=False)
            to_phys_s = getattr(src_route.backend, "_to_physical", None)
            to_phys_d = getattr(dst_route.backend, "_to_physical", None)
            if to_phys_s and to_phys_d:
                src_phys = to_phys_s(src_route.backend_path)
                dst_phys = to_phys_d(dst_route.backend_path)
        except Exception:
            pass

        await self._nx.sys_rename(src, dst, context=ctx)

        # After metadata rename, physically move the file so reads from dst work.
        if src_phys is not None and dst_phys is not None:
            try:
                if await asyncio.to_thread(src_phys.exists):
                    await asyncio.to_thread(
                        lambda: (
                            dst_phys.parent.mkdir(parents=True, exist_ok=True),
                            src_phys.rename(dst_phys),
                        )
                    )
            except Exception:
                pass  # best-effort; metadata rename already succeeded

    async def mkdir(self, path: str, zone_id: str) -> None:
        self._require_bound()
        ctx = self._ctx(zone_id)
        await self._nx.mkdir(path, parents=True, exist_ok=True, context=ctx)

    async def access(self, path: str, zone_id: str) -> bool:  # noqa: ARG002
        self._require_bound()
        import asyncio

        try:
            route = self._nx.router.route(path, is_admin=True, check_write=False)
            return await asyncio.to_thread(route.backend.exists, route.backend_path)
        except (FileNotFoundError, KeyError):
            return False

    # Alias for backward compatibility
    exists = access

    async def sys_unlink(self, path: str, zone_id: str) -> None:
        self._require_bound()
        ctx = self._ctx(zone_id)
        await self._nx.sys_unlink(path, context=ctx)

    async def file_mtime(self, path: str, zone_id: str) -> "datetime | None":  # noqa: ARG002
        """Return the server-observed modification time.

        Tries two sources in order:
        1. Metastore ``modified_at`` — authoritative for all backends but may miss
           on ``/agents`` mounts where Raft prefix scans are bypassed (see list_dir).
        2. OS ``stat()`` via the backend's physical path — server-controlled and
           reliable for LocalConnector; silently skipped for remote/object backends.

        Returns ``None`` when neither source is available. Callers must handle
        ``None`` as a safe-fail (skip retention action) — never fall back to
        filename timestamps, which are sender-controlled.
        """
        self._require_bound()
        try:
            route = self._nx.router.route(path, is_admin=True, check_write=False)

            # Source 1: metastore modified_at
            meta = route.metastore.get(path)
            if meta is not None:
                mtime: datetime | None = getattr(meta, "modified_at", None)
                if mtime is not None:
                    return mtime

            # Source 2: OS stat via backend physical path (LocalConnector only)
            import asyncio

            physical_path = getattr(route, "backend_path", None)
            if physical_path:
                # Resolve through backend if it exposes _to_physical
                to_phys = getattr(route.backend, "_to_physical", None)
                if to_phys is not None:
                    try:
                        phys = to_phys(physical_path)

                        def _stat_phys(p: "Any") -> "Any":
                            return p.stat() if p.exists() else None

                        stat = await asyncio.to_thread(_stat_phys, phys)
                        if stat is not None:
                            return datetime.fromtimestamp(stat.st_mtime, UTC)
                    except Exception:
                        pass
        except Exception:
            pass
        return None
