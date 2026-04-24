"""Thin NexusFS facade for the slim package.

Exposes ~10 public methods from the kernel NexusFS. Internal methods
(sandbox, workflows, bulk operations, dispatch hooks) are hidden.

The facade also provides optimized implementations where the full kernel
path is unnecessarily heavy for slim-package use (e.g., single-lookup stat).

Usage:
    from nexus.fs._facade import SlimNexusFS

    facade = SlimNexusFS(kernel_fs)
    content = facade.read("/s3/bucket/file.txt")
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import replace as _dc_replace
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import FileMetadata
from nexus.contracts.types import OperationContext
from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


def _make_stat_dict(
    *,
    path: str,
    size: int,
    etag: str | None,
    mime_type: str,
    created_at: str | None,
    modified_at: str | None,
    is_directory: bool,
    version: int,
    zone_id: str | None,
    entry_type: int,
) -> dict[str, Any]:
    """Build the stat response dict.  Single source of truth for the shape."""
    return {
        "path": path,
        "size": size,
        "etag": etag,
        "mime_type": mime_type,
        "created_at": created_at,
        "modified_at": modified_at,
        "is_directory": is_directory,
        "version": version,
        "zone_id": zone_id,
        "entry_type": entry_type,
    }


# Default context for slim-mode (single-user, no auth)
_SLIM_CONTEXT = OperationContext(
    user_id="local",
    groups=[],
    zone_id=ROOT_ZONE_ID,
    is_admin=True,
)


class _LockPoolHolder:
    """Thin wrapper so a lock stripe pool can live in a WeakValueDictionary.

    Python's WeakValueDictionary doesn't accept tuples as values (they
    aren't weak-referenceable), but a user-defined class instance is.
    The pool field stays immutable — replace holders, don't mutate.
    """

    __slots__ = ("pool", "__weakref__")

    def __init__(self, pool: tuple[threading.Lock, ...]) -> None:
        self.pool = pool


class SlimNexusFS:
    """Slim facade over the NexusFS kernel.

    Provides a clean, minimal API surface for the standalone nexus-fs package.
    All methods use a default local context (no auth, single-user).

    Public API (~10 methods):
        read, write, ls, stat, delete, mkdir, rmdir, rename, exists, copy
    """

    # Stripe count for the slim-mode per-path lock pool.  64 stripes is
    # plenty to keep same-path writes serialized while avoiding the
    # unbounded-dict growth a per-path Lock registry would produce in a
    # long-lived process.  Collisions (two unrelated paths sharing a
    # stripe) only cost brief contention — never incorrectness, since
    # same-path writes still serialize on the same stripe.
    _SLIM_LOCK_STRIPES = 64

    # Shared stripe pools keyed by kernel identity — two SlimNexusFS
    # wrappers around the same NexusFS must share locks, otherwise
    # concurrent writers via different wrappers can both read version
    # N and both persist N+1.  WeakValueDictionary so pools get GC'd
    # along with their kernel.
    _shared_lock_pools: "Any" = None  # lazily set below
    _shared_lock_pools_mutex: "threading.Lock" = threading.Lock()

    def __init__(self, kernel: NexusFS) -> None:
        self._kernel = kernel
        self._ctx = _SLIM_CONTEXT
        self._closed = False
        # Holder kept as an attribute so the WeakValueDictionary entry
        # survives as long as at least one facade references it.  Once
        # every facade around this kernel is gone the holder is GC'd
        # and the shared-pools dict entry drops automatically.
        self._slim_lock_pool_holder = self._resolve_shared_lock_pool(kernel)
        self._slim_lock_pool = self._slim_lock_pool_holder.pool

    @classmethod
    def _resolve_shared_lock_pool(cls, kernel: NexusFS) -> _LockPoolHolder:
        """Return a stripe pool shared across every facade wrapping ``kernel``.

        Keying on ``id(kernel)`` means two SlimNexusFS instances built
        on the same NexusFS resolve to the same pool, so writes through
        either wrapper serialize correctly on the same path's stripe.
        Pools are stored in a WeakValueDictionary so they release once
        no facade references them anymore — no long-lived growth.
        """
        import weakref

        with cls._shared_lock_pools_mutex:
            if cls._shared_lock_pools is None:
                cls._shared_lock_pools = weakref.WeakValueDictionary()
            holder: _LockPoolHolder | None = cls._shared_lock_pools.get(id(kernel))
            if holder is None:
                pool = tuple(threading.Lock() for _ in range(cls._SLIM_LOCK_STRIPES))
                holder = _LockPoolHolder(pool)
                cls._shared_lock_pools[id(kernel)] = holder
            return holder

    @property
    def kernel(self) -> NexusFS:
        """Escape hatch: access the underlying kernel for advanced use."""
        return self._kernel

    # -- Read operations --

    def read(self, path: str) -> bytes:
        """Read file content.

        Args:
            path: Virtual file path (e.g., "/s3/my-bucket/file.txt")

        Returns:
            File content as bytes.

        Raises:
            NexusFileNotFoundError: If file does not exist.
        """
        from nexus.contracts.exceptions import NexusFileNotFoundError

        try:
            return self._kernel.sys_read(path, context=self._ctx)
        except NexusFileNotFoundError:
            # Three fallback paths share this handler so the hot path stays
            # a single Rust call.  Order: external connectors first
            # (DT_EXTERNAL_STORAGE virtual files never hit the metastore,
            # so the kernel always raises even though the router can serve
            # the read); then the passthrough disk fallback for path-
            # addressed local mounts (pre-existing files on disk the
            # facade never wrote — #3831); then the slim-metastore CAS
            # fallback (#3821).
            external = self._try_external_read(path)
            if external is not None:
                return external
            passthrough = self._try_disk_passthrough_read(path)
            if passthrough is not None:
                return passthrough
            data = self._slim_metastore_read(path)
            if data is None:
                raise
            return data

    def _try_external_read(self, path: str) -> bytes | None:
        """Delegate to the Python backend for ``DT_EXTERNAL_STORAGE`` mounts.

        Returns ``None`` when the path does not resolve to an external
        connector route, letting ``read()`` surface the original 404.

        Connector-backed virtual files (Gmail messages, Drive objects,
        calendar events) never have metastore entries of their own, so
        the Rust kernel's ``sys_read`` raises before the ``route.is_external``
        short-circuit gets a chance to fire on mount registrations that
        didn't propagate the flag.  Resolving the path through the Python
        router recovers the backend and calls ``read_content`` directly.
        """
        from nexus.contracts.exceptions import (
            AccessDeniedError,
            InvalidPathError,
            PathNotMountedError,
        )
        from nexus.contracts.types import OperationContext
        from nexus.core.path_utils import validate_path
        from nexus.core.router import ExternalRouteResult

        try:
            normalized = validate_path(path)
        except Exception:
            return None

        router = getattr(self._kernel, "router", None)
        if router is None:
            return None

        caller_ctx = getattr(self, "_ctx", None)
        is_admin = bool(getattr(caller_ctx, "is_admin", True))
        zone_id = getattr(caller_ctx, "zone_id", None) or "root"

        try:
            route = router.route(normalized, zone_id=zone_id)
        except (PathNotMountedError, AccessDeniedError, InvalidPathError):
            return None

        if not isinstance(route, ExternalRouteResult):
            return None

        backend = getattr(route, "backend", None)
        backend_path = getattr(route, "backend_path", "") or ""
        mount_point = getattr(route, "mount_point", "") or ""
        if backend is None or getattr(backend, "read_content", None) is None:
            return None

        # Carry the caller's identity so auth/audit stays accurate, but
        # fill in the mount/backend-path slots the connector expects.
        if caller_ctx is not None:
            ctx = _dc_replace(
                caller_ctx,
                backend_path=backend_path,
                virtual_path=normalized,
                mount_path=mount_point,
            )
        else:
            ctx = OperationContext(
                user_id="local",
                groups=[],
                zone_id=zone_id,
                is_admin=is_admin,
                backend_path=backend_path,
                virtual_path=normalized,
                mount_path=mount_point,
            )

        # Virtual .readme/ overlay check (Issue #3728) — serve generated
        # docs without touching the live backend.
        from nexus.backends.connectors.schema_generator import dispatch_virtual_readme_read

        virtual = dispatch_virtual_readme_read(backend, mount_point, backend_path, context=ctx)
        if virtual is not None:
            return bytes(virtual) if isinstance(virtual, (bytes, bytearray)) else None

        data = backend.read_content(backend_path, context=ctx)
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        return None

    def _try_disk_passthrough_read(self, path: str) -> bytes | None:
        """Read directly from a path-addressed local backend (#3831).

        The Rust kernel only sees entries that went through its own write
        path; files dropped into the mount root on disk (pre-existing
        files, files placed out-of-band) never get a dcache entry and
        ``sys_read`` therefore raises even though the backend can serve
        them.  For path-addressed backends like ``PathLocalBackend`` the
        virtual path *is* the on-disk path (no content hashing), so we
        can resolve the mount through the router and let the backend
        read the file straight off disk.

        Returns ``None`` when the path doesn't resolve to an eligible
        path-addressed local backend so ``read()`` falls through to the
        CAS-local fast path and re-raises the original NOT_FOUND.

        Gate is the inverse of ``_slim_metastore_read`` — we want
        path-addressed backends with a local root, specifically skipping
        CAS backends (addressed by hash, not path) and connector mounts
        (DT_EXTERNAL_STORAGE, handled by ``_try_external_read``).
        """
        from nexus.contracts.exceptions import (
            AccessDeniedError,
            InvalidPathError,
            NexusFileNotFoundError,
            PathNotMountedError,
        )
        from nexus.contracts.types import OperationContext
        from nexus.core.path_utils import validate_path
        from nexus.core.router import ExternalRouteResult, RouteResult

        try:
            normalized = validate_path(path)
        except Exception:
            return None

        router = getattr(self._kernel, "router", None)
        if router is None:
            return None

        caller_ctx = getattr(self, "_ctx", None)
        is_admin = bool(getattr(caller_ctx, "is_admin", True))
        zone_id = getattr(caller_ctx, "zone_id", None) or "root"

        try:
            route = router.route(normalized, zone_id=zone_id)
        except (PathNotMountedError, AccessDeniedError, InvalidPathError):
            return None

        if not isinstance(route, RouteResult) or isinstance(route, ExternalRouteResult):
            return None

        backend = route.backend
        # Path-addressed + local-root gate.  CAS backends use content_id
        # (hash), not backend_path, so they'd raise on read_content("", ...)
        # — stays with _slim_metastore_read's hash-based fallback.
        if not getattr(backend, "has_root_path", False):
            return None
        if type(backend).__name__.startswith("CAS"):
            return None
        read_content = getattr(backend, "read_content", None)
        if read_content is None:
            return None

        backend_path = route.backend_path or ""
        mount_point = route.mount_point or ""
        if caller_ctx is not None:
            ctx = _dc_replace(
                caller_ctx,
                backend_path=backend_path,
                virtual_path=normalized,
                mount_path=mount_point,
            )
        else:
            ctx = OperationContext(
                user_id="local",
                groups=[],
                zone_id=zone_id,
                is_admin=is_admin,
                backend_path=backend_path,
                virtual_path=normalized,
                mount_path=mount_point,
            )

        try:
            data = read_content("", context=ctx)
        except NexusFileNotFoundError:
            # Backend says the path doesn't exist — fall through to the
            # original NOT_FOUND from the caller.
            return None
        # Any other failure (BackendError, permission, I/O, corruption)
        # must NOT be silently collapsed into a 404.  Propagate so
        # operators and clients see the real signal instead of a
        # misleading "missing file" that hides data-integrity issues.
        return bytes(data) if isinstance(data, (bytes, bytearray)) else None

    def _slim_metastore_read(self, path: str) -> bytes | None:
        """Read via Python metastore + CAS backend (slim fallback, #3821).

        Returns ``None`` when the metadata/backend can't be resolved so the
        caller re-raises the original ``NexusFileNotFoundError``.

        Scope is deliberately narrow:

        * Only CAS-local backends are eligible — #3821 only affects slim
          ``local://`` mounts where the Rust kernel cannot see the Python
          SQLiteMetastore.  Path-addressed backends (``PathS3Backend`` etc.)
          resolve content via ``context.backend_path``, not ``etag``; the
          default ``_SLIM_CONTEXT`` does not carry that field, and misusing
          ``read_content(etag, ...)`` on them would raise.  Callers of
          path-addressed slim mounts therefore keep the pre-#3821 behaviour
          (FileNotFound on cold-start) until a proper fix lands — no
          regression, no silent mis-reads.
        * The backend is resolved via ``router.route()`` so LPM,
          readonly/admin_only, and mount-boundary checks all apply exactly
          as they do for ``sys_read``.  External connector mounts
          (DT_EXTERNAL_STORAGE) are skipped — they have their own read
          semantics and bubble through ``sys_readdir``/``sys_read``.
        """
        from nexus.contracts.exceptions import (
            AccessDeniedError,
            InvalidPathError,
            PathNotMountedError,
        )
        from nexus.core.path_utils import validate_path

        try:
            normalized = validate_path(path)
        except Exception:
            return None
        meta = self._kernel.metadata.get(normalized)
        if meta is None or not meta.etag:
            return None
        try:
            _rr = self._kernel._kernel.route(normalized, self._kernel._zone_id)
        except (PathNotMountedError, AccessDeniedError, InvalidPathError, ValueError):
            return None
        if _rr.is_external:
            return None
        _dlc_info = self._kernel._driver_coordinator.get_mount_info_canonical(_rr.mount_point)
        if _dlc_info is None:
            return None
        backend = _dlc_info.backend
        expected_name = (meta.backend_name or "").split(":", 1)[0].split("@", 1)[0]
        actual_name = getattr(backend, "name", None)
        if expected_name and actual_name and expected_name != actual_name:
            return None
        # CAS-local gate: backend must be content-addressed *and* carry a
        # local root path.  This matches how MountTable detects CAS-local
        # backends for the kernel (see core/mount_table.py::add).
        is_cas_local = getattr(backend, "has_root_path", False) and type(
            backend
        ).__name__.startswith("CAS")
        if not is_cas_local:
            return None
        read_content = getattr(backend, "read_content", None)
        if read_content is None:
            return None
        from nexus.contracts.exceptions import NexusFileNotFoundError

        try:
            data = read_content(meta.etag, context=self._ctx)
        except NexusFileNotFoundError:
            # Real not-found at the backend layer — caller re-raises the
            # original FileNotFound; no signal loss.
            return None
        # Permission errors, disk IO failures, corruption, BackendError,
        # etc. propagate unchanged so operators see the real cause instead
        # of a misleading "missing file" signal masking a data-integrity
        # incident.
        return bytes(data) if isinstance(data, (bytes, bytearray)) else None

    def read_range(self, path: str, start: int, end: int) -> bytes:
        """Read a specific byte range from a file.

        Memory-efficient — only fetches the requested range from the backend.

        Args:
            path: Virtual file path.
            start: Start byte offset (inclusive).
            end: End byte offset (exclusive).

        Returns:
            Bytes in the requested range.
        """
        return self._kernel.read_range(path, start, end, context=self._ctx)

    # -- Write operations --

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        """Write content to a file (creates or overwrites).

        Args:
            path: Virtual file path.
            content: File content as bytes.

        Returns:
            Dict with path, size, etag, version.
        """
        # Slim mode (no Rust kernel) + ExternalRouteResult: kernel.write
        # trips on dispatch_pre_hooks, but the kernel's external-write
        # body is just backend.write_content + metastore.put.  Replicate
        # that path directly so connector mounts (Gmail, Calendar, etc.)
        # work in the slim package, connector-agnostic.
        external = self._try_external_write(path, content)
        if external is not None:
            return external
        return self._kernel.write(path, content, context=self._ctx)

    def _try_external_write(self, path: str, content: bytes) -> dict[str, Any] | None:
        """Connector-agnostic write for slim-mode external routes.

        Returns ``None`` when the path doesn't resolve to an external
        connector mount, letting ``write()`` fall through to the kernel
        (which is the right path for CAS / path-local / remote mounts
        and for full-kernel mode where hooks dispatch correctly).

        Gated on ``_is_slim_mode()`` — in full-kernel mode the kernel
        path handles hooks, quotas, observers, and OCC properly and we
        shouldn't bypass any of that.  In slim mode none of those exist,
        so calling ``backend.write_content`` + ``metastore.put`` directly
        matches what the kernel would have done for this route.
        """
        if not self._is_slim_mode():
            return None
        resolved = self._resolve_external_route(path)
        if resolved is None:
            return None
        route, ctx, normalized = resolved
        backend = route.backend
        write_content = getattr(backend, "write_content", None)
        if write_content is None:
            return None

        # Per-path lock — serializes the read-existing / backend-write /
        # metastore-put sequence so concurrent writers don't both read
        # the same version N and both persist N+1.  Mirrors
        # NexusFS._write_content's `_vfs_locked` discipline, but in
        # pure Python since the slim package has no Rust VFS lock.
        with self._slim_path_lock(normalized):
            # Carry existing metadata so a re-write increments version
            # correctly and preserves created_at / owner_id.
            existing_meta = self._kernel.metadata.get(normalized)

            # Mirror NexusFS._write_content: content_id is only passed for
            # offset>0 splice writes, never for full overwrites.  Slim
            # facade.write() only does full overwrites (offset=0), so always
            # empty — backend_path on ctx is authoritative.  Forwarding the
            # stale physical_path here would misroute rewrites on path-
            # addressed connectors whose returned content_id differs from
            # backend_path.
            wr = write_content(
                content,
                content_id="",
                offset=0,
                context=ctx,
            )
            content_hash = getattr(wr, "content_id", "") or ""
            size = getattr(wr, "size", None)
            if not isinstance(size, int):
                size = len(content)

            # Persist metadata locally for non-remote backends (remote
            # backends manage their own metastore via RPC).  Same split
            # as core/nexus_fs.py::_write_content.
            is_remote = hasattr(backend, "_rpc_client") or "remote" in (
                getattr(backend, "name", "") or ""
            )
            from datetime import UTC, datetime

            from nexus.contracts.metadata import FileMetadata

            now = datetime.now(UTC)
            new_version = (existing_meta.version + 1) if existing_meta else 1
            backend_name_full = route.backend.name if getattr(route.backend, "name", None) else ""
            metadata = FileMetadata(
                path=normalized,
                backend_name=backend_name_full,
                physical_path=content_hash,
                size=size,
                etag=content_hash,
                created_at=existing_meta.created_at if existing_meta else now,
                modified_at=now,
                version=new_version,
                zone_id=getattr(ctx, "zone_id", None) or "root",
                owner_id=existing_meta.owner_id
                if existing_meta
                else (getattr(ctx, "subject_id", None) or getattr(ctx, "user_id", None) or "local"),
            )
            if not is_remote:
                route.metastore.put(metadata)

        return {
            "path": normalized,
            "etag": content_hash,
            "version": new_version,
            "size": size,
            "modified_at": now.isoformat(),
        }

    def _is_slim_mode(self) -> bool:
        """True when the Rust kernel (NexusFS._kernel) is absent.

        Slim mode = Python-only nexus-fs install with no ``nexus_kernel``
        extension.  In this mode every ``dispatch_pre_hooks`` /
        ``dispatch_post_hooks`` call in NexusFS raises AttributeError,
        so mutating ops that touch those must route around the kernel
        and talk to the backend directly.
        """
        return getattr(self._kernel, "_kernel", "__absent__") is None

    def _slim_path_lock(self, path: str) -> threading.Lock:
        """Return a striped per-path lock for slim-mode serialization.

        The Rust kernel's VFS lock isn't available in slim mode, so
        writes/deletes serialize on a ``threading.Lock`` selected by
        hashing the virtual path into a fixed-size stripe pool.  Same
        path always maps to the same stripe, so concurrent writers on
        one path serialize correctly.  Different paths may share a
        stripe (brief contention) but never incorrectness.  The
        fixed-size pool avoids the unbounded memory growth a per-path
        dict would produce in long-lived processes.
        """
        # Python's built-in hash() is randomized per-process but stable
        # within a process, which is exactly the property we need here.
        return self._slim_lock_pool[hash(path) % self._SLIM_LOCK_STRIPES]

    def _resolve_external_route(self, path: str) -> tuple[Any, Any, str] | None:
        """Route ``path`` and return ``(route, augmented_ctx, normalized_path)``.

        Returns a tuple for any route whose mount root is DT_EXTERNAL_STORAGE
        — this covers both the first-write case (router returns
        ``ExternalRouteResult`` because no file metadata yet) and the
        subsequent-write case (router returns ``RouteResult`` because
        file metadata now exists and takes precedence over mount-root
        metadata).  Every other route type (non-connector CAS, path-local,
        pipe, stream) falls back to the kernel path.
        """
        from nexus.contracts.exceptions import InvalidPathError, PathNotMountedError
        from nexus.contracts.types import OperationContext
        from nexus.core.path_utils import validate_path
        from nexus.core.router import ExternalRouteResult, RouteResult

        try:
            normalized = validate_path(path)
        except Exception:
            return None

        router = getattr(self._kernel, "router", None)
        if router is None:
            return None

        caller_ctx = getattr(self, "_ctx", None)
        is_admin = bool(getattr(caller_ctx, "is_admin", True))
        zone_id = getattr(caller_ctx, "zone_id", None) or "root"

        try:
            route = router.route(normalized, zone_id=zone_id)
        except (PathNotMountedError, InvalidPathError):
            return None

        if not isinstance(route, (ExternalRouteResult, RouteResult)):
            return None

        if not isinstance(route, ExternalRouteResult):
            # Non-external RouteResult is only eligible when the mount
            # ROOT is DT_EXTERNAL_STORAGE — i.e. a connector mount where
            # file metadata was persisted on a previous write and now
            # shadows the mount-root external flag in the router.
            mount_meta = self._kernel.metadata.get(route.mount_point)
            if mount_meta is None or not mount_meta.is_external_storage:
                return None

        backend_path = getattr(route, "backend_path", "") or ""
        mount_point = getattr(route, "mount_point", "") or ""
        if caller_ctx is not None:
            ctx = _dc_replace(
                caller_ctx,
                backend_path=backend_path,
                virtual_path=normalized,
                mount_path=mount_point,
            )
        else:
            ctx = OperationContext(
                user_id="local",
                groups=[],
                zone_id=zone_id,
                is_admin=is_admin,
                backend_path=backend_path,
                virtual_path=normalized,
                mount_path=mount_point,
            )
        return route, ctx, normalized

    def write_batch(self, files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
        """Write multiple files atomically in a single transaction.

        All files are written atomically — either all succeed or all fail.
        13× faster than N sequential ``write()`` calls for small files.

        Args:
            files: List of ``(path, content)`` tuples.

        Returns:
            List of result dicts (same order as input), each with
            ``etag``, ``version``, ``modified_at``, and ``size``.

        Raises:
            NexusFileNotFoundError: Never — writes always create.
            InvalidPathError: If any path is invalid.
        """
        return self._kernel.write_batch(files, context=self._ctx)

    def read_batch(
        self,
        paths: list[str],
        *,
        partial: bool = False,
    ) -> list[dict[str, Any]]:
        """Read multiple files in a single atomic round-trip.

        Uses the Rust kernel's parallel read path — faster and more
        consistent than N sequential ``read()`` calls.

        Args:
            paths:   List of virtual file paths.
            partial: If ``False`` (default), raises ``NexusFileNotFoundError``
                     on the first missing or inaccessible path.
                     If ``True``, returns a per-item result for every path
                     (successes and errors alike).

        Returns:
            List of dicts in the same order as *paths*.

            Successful item::

                {
                    "path":        str,
                    "content":     bytes,
                    "etag":        str | None,
                    "version":     int,
                    "modified_at": datetime | None,
                    "size":        int,
                }

            Failed item (only when ``partial=True``)::

                {"path": str, "error": "not_found"}

        Raises:
            NexusFileNotFoundError: If any path is missing and ``partial=False``.
            InvalidPathError: If any path is invalid (always raised).
        """
        return self._kernel.read_batch(paths, partial=partial, context=self._ctx)

    # -- Directory operations --

    def ls(
        self,
        path: str = "/",
        detail: bool = False,
        recursive: bool = False,
    ) -> list[str] | list[dict[str, Any]]:
        """List directory contents.

        Args:
            path: Directory path to list.
            detail: If True, return dicts with metadata. If False, return paths.
            recursive: If True, list recursively.

        Returns:
            List of paths (detail=False) or list of metadata dicts (detail=True).
        """
        from nexus.contracts.exceptions import NexusFileNotFoundError

        kernel_err: NexusFileNotFoundError | None = None
        kernel_entries: list[Any]
        try:
            kernel_entries = list(
                self._kernel.sys_readdir(
                    path,
                    recursive=recursive,
                    details=detail,
                    context=self._ctx,
                )
            )
        except NexusFileNotFoundError as exc:
            kernel_err = exc
            kernel_entries = []

        # Merge in pre-existing on-disk files under path-addressed local
        # mounts (#3831) — the kernel only sees dcache entries, so files
        # dropped in the mount root out-of-band are invisible without
        # this pass.
        merged = self._merge_passthrough_ls(path, kernel_entries, detail, recursive)

        # Re-raise NOT_FOUND only when the disk scan didn't find anything
        # either — a pre-existing on-disk subdir with no metastore entry
        # is a legit hit, not an error.
        if kernel_err is not None and not merged:
            raise kernel_err
        return merged

    def _merge_passthrough_ls(
        self,
        path: str,
        kernel_entries: list[Any],
        detail: bool,
        recursive: bool,
    ) -> list[Any]:
        """Augment ``sys_readdir`` output with live backend listing (#3831).

        For path-addressed local mounts (``PathLocalBackend``), scan the
        backend directly and merge any entries the kernel didn't know
        about.  No-ops for CAS-local (hash-addressed, not browsable by
        path), external connectors (listed via their own sync path), and
        unmounted paths.
        """
        from nexus.contracts.exceptions import (
            AccessDeniedError,
            InvalidPathError,
            PathNotMountedError,
        )
        from nexus.core.path_utils import validate_path
        from nexus.core.router import ExternalRouteResult, RouteResult

        try:
            normalized = validate_path(path, allow_root=True)
        except Exception:
            return kernel_entries

        router = getattr(self._kernel, "router", None)
        if router is None:
            return kernel_entries

        caller_ctx = getattr(self, "_ctx", None)
        zone_id = getattr(caller_ctx, "zone_id", None) or "root"

        try:
            route = router.route(normalized, zone_id=zone_id)
        except (PathNotMountedError, AccessDeniedError, InvalidPathError):
            return kernel_entries

        if not isinstance(route, RouteResult) or isinstance(route, ExternalRouteResult):
            return kernel_entries

        backend = route.backend
        if not getattr(backend, "has_root_path", False):
            return kernel_entries
        if type(backend).__name__.startswith("CAS"):
            return kernel_entries
        list_dir = getattr(backend, "list_dir", None)
        if list_dir is None:
            return kernel_entries

        virtual_prefix = normalized.rstrip("/")
        if not virtual_prefix:
            virtual_prefix = ""
        known: set[str] = set()
        for e in kernel_entries:
            if isinstance(e, str):
                known.add(e.rstrip("/"))
            elif isinstance(e, dict):
                p = e.get("path")
                if isinstance(p, str):
                    known.add(p.rstrip("/"))

        from nexus.contracts.exceptions import (
            AuthenticationError as _AuthErr,
        )
        from nexus.contracts.exceptions import (
            BackendError as _BErr,
        )

        def _list_backend(rel: str) -> list[str]:
            try:
                return list(list_dir(rel, context=caller_ctx))
            except FileNotFoundError:
                # Directory simply doesn't exist under this prefix —
                # normal for an empty mount root or a freshly-created
                # mount point; return [] so the merge is a no-op.
                return []
            except (_BErr, _AuthErr):
                # Propagate real backend failures — swallowing them
                # would look like "data disappeared" to the caller and
                # hide auth expiry / permission / I/O incidents.
                raise

        added: list[Any] = []

        def _push(entry_path: str, is_dir: bool) -> None:
            entry_path = entry_path.rstrip("/")
            if entry_path in known:
                return
            known.add(entry_path)
            if detail:
                added.append(
                    _make_stat_dict(
                        path=entry_path,
                        size=4096 if is_dir else 0,
                        etag=None,
                        mime_type=("inode/directory" if is_dir else "application/octet-stream"),
                        created_at=None,
                        modified_at=None,
                        is_directory=is_dir,
                        version=0,
                        zone_id=zone_id,
                        entry_type=1 if is_dir else 0,
                    )
                )
            else:
                added.append(entry_path)

        def _walk(rel_backend: str, rel_virtual: str) -> None:
            for name in _list_backend(rel_backend):
                is_dir = name.endswith("/")
                clean = name.rstrip("/")
                if not clean:
                    continue
                next_virtual = f"{rel_virtual}/{clean}" if rel_virtual else f"/{clean}"
                next_backend = f"{rel_backend}/{clean}" if rel_backend else clean
                _push(next_virtual, is_dir)
                if recursive and is_dir:
                    _walk(next_backend, next_virtual)

        _walk(route.backend_path or "", virtual_prefix)

        if not added:
            return kernel_entries
        # Preserve kernel order, then append new backend-only entries.
        return list(kernel_entries) + added

    def mkdir(self, path: str, parents: bool = True) -> None:
        """Create a directory.

        Args:
            path: Directory path to create.
            parents: If True, create parent directories as needed (mkdir -p).
        """
        self._kernel.mkdir(
            path,
            parents=parents,
            exist_ok=True,
            context=self._ctx,
        )

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """Remove a directory.

        Args:
            path: Directory path to remove.
            recursive: If True, remove contents recursively (rm -rf).
        """
        self._kernel.rmdir(path, recursive=recursive, context=self._ctx)

    # -- File operations --

    def delete(self, path: str) -> None:
        """Delete a file.

        Args:
            path: Virtual file path to delete.

        Raises:
            NexusFileNotFoundError: If file does not exist.
            ValueError: If path is a mount root — use unmount() instead.
        """
        from nexus.core.path_utils import validate_path

        normalized = validate_path(path)
        meta = self._kernel.metadata.get(normalized)
        if meta is not None and meta.is_mount:
            raise ValueError(
                f"Cannot delete mount root '{normalized}' — use unmount() to remove a mount."
            )
        # Slim mode + external route: see _try_external_write for the
        # rationale — replicate the kernel's external-delete path
        # (backend.delete_content + metastore.delete) directly.
        if self._try_external_delete(normalized):
            return
        self._kernel.sys_unlink(path, context=self._ctx)

    def _try_external_delete(self, path: str) -> bool:
        """Connector-agnostic delete for slim-mode external routes."""
        if not self._is_slim_mode():
            return False
        resolved = self._resolve_external_route(path)
        if resolved is None:
            return False
        route, ctx, normalized = resolved
        backend = route.backend
        delete_content = getattr(backend, "delete_content", None)
        if delete_content is None:
            return False

        with self._slim_path_lock(normalized):
            existing_meta = self._kernel.metadata.get(normalized)
            content_id = existing_meta.physical_path if existing_meta else ""
            delete_content(content_id, context=ctx)

            is_remote = hasattr(backend, "_rpc_client") or "remote" in (
                getattr(backend, "name", "") or ""
            )
            if not is_remote and existing_meta is not None:
                self._kernel.metadata.delete(normalized)
        return True

    def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move a file.

        Args:
            old_path: Current file path.
            new_path: New file path.
        """
        self._kernel.sys_rename(old_path, new_path, context=self._ctx)

    def exists(self, path: str) -> bool:
        """Check if a path exists.

        Args:
            path: Virtual file path.

        Returns:
            True if the path exists (file or directory).
        """
        return self._kernel.access(path, context=self._ctx)

    def copy(self, src: str, dst: str) -> dict[str, Any]:
        """Copy a file from src to dst.

        Delegates to the kernel's sys_copy which uses backend-native
        server-side copy when available (S3 CopyObject, GCS rewrite),
        CAS metadata duplication for content-addressed backends, or
        chunked streaming as a fallback.

        Args:
            src: Source file path.
            dst: Destination file path.

        Returns:
            Dict with path, size, etag of the new file.
        """
        return self._kernel.sys_copy(src, dst, context=self._ctx)

    def edit(
        self,
        path: str,
        edits: list[tuple[str, str]] | list[dict[str, Any]],
        *,
        if_match: str | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Apply surgical search/replace edits to a file.

        Uses a layered matching strategy (exact -> whitespace-normalized -> fuzzy)
        to find and replace text without rewriting the entire file.

        Args:
            path: Virtual file path.
            edits: List of edit operations. Each can be:
                - Tuple: (old_str, new_str)
                - Dict: {"old_str": str, "new_str": str, "hint_line": int | None,
                         "allow_multiple": bool}
            if_match: Optional etag for optimistic concurrency control.
            fuzzy_threshold: Similarity threshold (0.0-1.0) for fuzzy matching.
            preview: If True, return preview without writing.

        Returns:
            Dict with success, diff, matches, applied_count, etag, version, errors.

        Note:
            The underlying kernel edit is NOT atomic — there is a TOCTOU window
            between the read and the final write.  ``if_match`` catches stale
            reads but cannot prevent a concurrent writer from updating the file
            between the ETag check and the write.  For concurrent-writer safety,
            use an external lock or wait for kernel-level OCC-aware writes.
        """
        return self._kernel.edit(
            path,
            edits,
            context=self._ctx,
            if_match=if_match,
            fuzzy_threshold=fuzzy_threshold,
            preview=preview,
        )

    # -- Metadata (optimized single-lookup) --

    def stat(self, path: str) -> dict[str, Any] | None:
        """Get file/directory metadata with a single metadata lookup.

        Optimized for the slim package — avoids the kernel's double-lookup
        pattern (is_directory + metadata.get) by doing one read and
        deriving directory status from the result.

        Args:
            path: Virtual file path.

        Returns:
            Metadata dict, or None if path does not exist.
        """
        from nexus.core.path_utils import validate_path

        normalized = validate_path(path, allow_root=True)

        # Route through the kernel's sys_stat so zone-relative key translation
        # is handled centrally. Direct ``metadata.get(global_path)`` returns
        # ``None`` after F4 zone-relative key refactor for paths under mounts
        # (the entry lives at the mount's zone-local key).
        _kstat = self._kernel.sys_stat(normalized, context=self._ctx)
        if _kstat is not None:
            return _kstat

        meta: FileMetadata | None = None

        if meta is not None:
            is_dir = meta.is_dir or meta.is_mount or meta.mime_type == "inode/directory"
            return _make_stat_dict(
                path=meta.path,
                size=meta.size or (4096 if is_dir else 0),
                etag=meta.etag,
                mime_type=meta.mime_type
                or ("inode/directory" if is_dir else "application/octet-stream"),
                created_at=meta.created_at.isoformat() if meta.created_at else None,
                modified_at=meta.modified_at.isoformat() if meta.modified_at else None,
                is_directory=is_dir,
                version=meta.version,
                zone_id=meta.zone_id,
                entry_type=meta.entry_type,
            )

        # No explicit entry — check if it's an implicit directory.
        # is_implicit_directory is on concrete metastore classes, not the ABC.
        _meta = self._kernel.metadata
        _is_implicit = getattr(_meta, "is_implicit_directory", None)
        if _is_implicit is not None and _is_implicit(normalized):
            return _make_stat_dict(
                path=normalized,
                size=4096,
                etag=None,
                mime_type="inode/directory",
                created_at=None,
                modified_at=None,
                is_directory=True,
                version=0,
                zone_id=ROOT_ZONE_ID,
                entry_type=1,
            )

        # Virtual .readme/ overlay check (Issue #3728).  The slim facade
        # has its own fast stat path that bypasses NexusFS.stat(), so we
        # run the same fallback helper here before returning None.
        _vstat = self._kernel._try_virtual_readme_stat(normalized, self._ctx)
        if _vstat is not None:
            return _vstat

        return None

    # -- Search operations --

    # Issue #3711: threshold above which lazy trigram index build is worthwhile.
    _TRIGRAM_LAZY_BUILD_THRESHOLD = 500

    def _trigram_index_path(self) -> str:
        """Return the expected trigram index path for this facade's zone."""
        zone_id = self._ctx.zone_id or ROOT_ZONE_ID
        index_dir = os.path.join(os.path.expanduser("~"), ".nexus", "indexes")
        return os.path.join(index_dir, f"{os.path.basename(zone_id)}.trgm")

    # Per-zone guard: prevents duplicate background builds for the same zone.
    _trigram_build_lock = threading.Lock()
    _trigram_builds_in_progress: set[str] = set()

    # Max file size to include in trigram index (skip large binaries).
    _TRIGRAM_MAX_FILE_SIZE = 1024 * 1024  # 1 MB

    def _ensure_trigram_index(self, file_paths: list[str]) -> str | None:
        """Return the trigram index path if it exists, or kick off a background build.

        Issue #3711: The trigram index was never built because
        ``build_trigram_index_for_zone`` had no callers.

        Design: the first grep is NOT slowed down.  If no index exists,
        we start a background thread that builds it from the file list.
        The *current* grep proceeds without the index (full scan).  The
        *next* grep finds the index on disk and uses the fast path.

        Returns the index path when the index already exists, None otherwise.
        """
        index_path = self._trigram_index_path()
        if os.path.isfile(index_path):
            return index_path

        if len(file_paths) < self._TRIGRAM_LAZY_BUILD_THRESHOLD:
            return None

        # Kick off background build (non-blocking).
        self._maybe_build_trigram_background(file_paths, index_path)
        return None

    def _maybe_build_trigram_background(self, file_paths: list[str], index_path: str) -> None:
        """Start a background thread to build the trigram index if not already running."""
        with SlimNexusFS._trigram_build_lock:
            if index_path in SlimNexusFS._trigram_builds_in_progress:
                return
            SlimNexusFS._trigram_builds_in_progress.add(index_path)

        # Snapshot the kernel + ctx references for the background thread.
        kernel = self._kernel
        ctx = self._ctx
        max_size = self._TRIGRAM_MAX_FILE_SIZE

        def _build() -> None:
            try:
                from nexus_kernel import build_trigram_index_from_entries

                entries: list[tuple[str, bytes]] = []
                for fp in file_paths:
                    try:
                        content = kernel.sys_read(fp, context=ctx)
                        if isinstance(content, bytes) and len(content) <= max_size:
                            entries.append((fp, content))
                    except Exception:
                        continue

                if entries:
                    os.makedirs(os.path.dirname(index_path), exist_ok=True)
                    build_trigram_index_from_entries(entries, index_path)
                    logger.debug(
                        "Issue #3711: Built trigram index at %s (%d files)",
                        index_path,
                        len(entries),
                    )
            except Exception:
                logger.debug("Background trigram build failed", exc_info=True)
            finally:
                with SlimNexusFS._trigram_build_lock:
                    SlimNexusFS._trigram_builds_in_progress.discard(index_path)

        thread = threading.Thread(target=_build, daemon=True)
        thread.start()

    def _trigram_candidates(
        self,
        index_path: str,
        pattern: str,
        path: str,
        ignore_case: bool,
    ) -> list[str] | None:
        """Return candidate file paths from trigram index, or None on error."""
        try:
            from nexus_kernel import trigram_search_candidates
        except (ImportError, OSError):
            return None

        try:
            candidates = trigram_search_candidates(index_path, pattern, ignore_case)
        except (OSError, ValueError, RuntimeError):
            return None

        if candidates is None:
            return None

        # Filter to files under the requested path.
        if path != "/":
            prefix = path if path.endswith("/") else path + "/"
            candidates = [c for c in candidates if c.startswith(prefix) or c == path]

        return candidates

    def grep(
        self,
        pattern: str,
        path: str = "/",
        *,
        ignore_case: bool = False,
        max_results: int = 1000,
    ) -> list[dict[str, Any]]:
        """Search file contents for a regex pattern.

        Recursively lists files under *path*, reads their contents, and
        searches using Rust-accelerated regex (nexus_kernel) when available,
        falling back to Python ``re`` otherwise.

        Args:
            pattern: Regex pattern to search for.
            path: Directory to search under (default root).
            ignore_case: Case-insensitive matching.
            max_results: Cap on returned matches.

        Returns:
            List of match dicts with keys: file, line, content, match.
        """
        import re

        flags = re.IGNORECASE if ignore_case else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc

        # List all files first (needed for both lazy index build and fallback).
        entries = self._kernel.sys_readdir(
            path,
            recursive=True,
            details=True,
            context=self._ctx,
        )
        all_files = [
            e["path"] for e in entries if isinstance(e, dict) and not e.get("is_directory", False)
        ]

        # Issue #3711: Lazy-build trigram index on first grep above threshold,
        # then use it to narrow candidates.  Falls back to full scan on miss.
        file_paths = all_files
        index_path = self._ensure_trigram_index(all_files)
        if index_path is not None:
            narrowed = self._trigram_candidates(index_path, pattern, path, ignore_case)
            if narrowed is not None:
                file_paths = narrowed

        matches: list[dict[str, Any]] = []
        # Process in bounded batches for Rust bulk grep when available
        _BATCH_SIZE = 64
        _rust_grep: Any = None
        _has_rust_grep = False
        try:
            from nexus_kernel import grep_bulk

            _rust_grep = grep_bulk
            _has_rust_grep = True
        except (ImportError, OSError):
            pass

        for batch_start in range(0, len(file_paths), _BATCH_SIZE):
            if len(matches) >= max_results:
                break
            batch = file_paths[batch_start : batch_start + _BATCH_SIZE]
            batch_contents: dict[str, bytes] = {}
            for fp in batch:
                try:
                    batch_contents[fp] = self._kernel.sys_read(fp, context=self._ctx)
                except Exception:
                    continue

            if not batch_contents:
                continue

            remaining = max_results - len(matches)

            # Try Rust bulk grep on this batch
            if _has_rust_grep:
                try:
                    batch_results = _rust_grep(pattern, batch_contents, ignore_case, remaining)
                    if batch_results is not None:
                        matches.extend(batch_results)
                        continue
                except (ValueError, RuntimeError):
                    pass

            # Python fallback for this batch
            for fp, content in batch_contents.items():
                try:
                    text = content.decode("utf-8", errors="replace")
                except Exception:
                    continue
                for line_no, line in enumerate(text.splitlines(), 1):
                    m = compiled.search(line)
                    if m:
                        matches.append(
                            {
                                "file": fp,
                                "line": line_no,
                                "content": line,
                                "match": m.group(0),
                            }
                        )
                        if len(matches) >= max_results:
                            return matches
        return matches

    def glob(
        self,
        pattern: str,
        path: str = "/",
    ) -> list[str]:
        """Find files matching a glob pattern.

        Recursively lists files under *path* and filters them using
        Rust-accelerated glob matching (nexus_kernel) when available,
        falling back to Python ``fnmatch`` otherwise.

        Args:
            pattern: Glob pattern (e.g., ``"**/*.py"``, ``"*.txt"``).
            path: Directory to search under (default root).

        Returns:
            List of matching file paths.
        """
        entries = self._kernel.sys_readdir(
            path,
            recursive=True,
            details=False,
            context=self._ctx,
        )
        all_paths = [e for e in entries if isinstance(e, str)]
        if not all_paths:
            return []

        # Try Rust-accelerated glob
        try:
            from nexus_kernel import glob_match_bulk as _rust_glob

            results = _rust_glob([pattern], all_paths)
            if results is not None:
                return list(results)
        except (ImportError, OSError, ValueError, RuntimeError):
            pass

        # Python fallback
        import fnmatch

        return [p for p in all_paths if fnmatch.fnmatch(p, pattern)]

    # -- Mount management (delegated to kernel router) --

    def list_mounts(self) -> list[str]:
        """List all mount points.

        Returns:
            Sorted list of mount point paths.
        """
        return self._kernel._driver_coordinator.mount_points()

    def unmount(self, mount_point: str) -> None:
        """Remove a mount and clean up all associated state.

        Removes the mount from the runtime router, deletes its metadata entry
        and all cached child metadata, and removes it from the persisted
        mounts.json so it does not reappear on the next process start.

        Args:
            mount_point: Mount point path (e.g. "/gdrive/my-drive").

        Raises:
            ValueError: If mount_point is not a mounted path.
        """
        from nexus.core.path_utils import validate_path

        normalized = validate_path(mount_point, allow_root=False)
        meta = self._kernel.metadata.get(normalized)
        if meta is None or not meta.is_mount:
            raise ValueError(f"'{normalized}' is not a mount point")

        # 1. Remove from runtime mount table
        self._kernel._driver_coordinator.unmount(normalized)

        # 2. Delete mount root metadata row + evict dcache
        self._kernel.metadata.delete(normalized)
        if hasattr(self._kernel.metadata, "dcache_evict_prefix"):
            self._kernel.metadata.dcache_evict_prefix(normalized + "/")

        # 3. Sweep cached child metadata (best-effort — connector mounts may
        #    have populated entries via the sync loop or explicit writes)
        prefix = normalized.rstrip("/") + "/"
        children = list(self._kernel.metadata.list(prefix))
        if children:
            self._kernel.metadata.delete_batch([c.path for c in children])

        # 4. Remove from mounts.json so the mount does not resurrect on restart
        import contextlib

        with contextlib.suppress(OSError):
            from nexus.fs._paths import load_persisted_mounts, save_persisted_mounts

            existing = load_persisted_mounts()
            # Remove any entry whose derived mount point matches
            from nexus.fs._uri import derive_mount_point, parse_uri

            filtered = []
            for entry in existing:
                try:
                    spec = parse_uri(entry["uri"])
                    mp = derive_mount_point(spec, at=entry.get("at"))
                    if mp != normalized:
                        filtered.append(entry)
                except Exception:
                    filtered.append(entry)
            if len(filtered) != len(existing):
                save_persisted_mounts(filtered, merge=False)

    # -- Lifecycle --

    def close(self) -> None:
        """Close the filesystem and release resources.

        Closes the kernel (NexusFS.close is sync) and then closes
        the metastore's SQLite connection.  Safe to call multiple
        times — subsequent calls are no-ops.
        """
        if self._closed:
            return

        import contextlib

        try:
            _close = getattr(self._kernel, "close", None)
            if _close is not None:
                _close()
        finally:
            with contextlib.suppress(Exception):
                self._kernel.metadata.close()
            self._closed = True

    def __enter__(self) -> SlimNexusFS:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
