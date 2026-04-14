"""InternalMixin — shared helpers used by Content/Metadata/Dispatch mixins.

Extracts cross-cutting utility methods that are called from multiple VFS
domains (read, write, stat, rename, …). Having them in a dedicated mixin
decouples the large ContentMixin and MetadataMixin from each other — both
depend on InternalMixin via MRO rather than on each other directly.

Mixin rules (Phase 6 established):
  • ``from __future__ import annotations`` + TYPE_CHECKING stubs
  • Single stub: ``_kernel: Any`` — other NexusFS attrs accessed via MRO
  • Listed BEFORE NexusFilesystemABC in MRO
  • No new ``type: ignore``
"""

from __future__ import annotations

import logging
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING, Any, NamedTuple

from nexus.contracts.metadata import FileMetadata
from nexus.contracts.types import OperationContext
from nexus.core.path_utils import validate_path

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class _WriteContentResult(NamedTuple):
    """Result of content-only write phase."""

    content_hash: str
    size: int
    metadata: "FileMetadata"  # Built metadata ready for metastore
    new_version: int
    is_new: bool  # True if file didn't exist before
    old_etag: str | None
    old_metadata: "FileMetadata | None"  # Pre-write metadata for post-write hooks
    context: "OperationContext"  # Augmented context (with backend_path/virtual_path)
    zone_id: str | None
    agent_id: str | None
    is_remote: bool  # Remote backend — skip local metadata.put


class InternalMixin:
    """Shared helpers for NexusFS mixins: context, validation, .readme overlay, events."""

    _kernel: Any  # Rust Kernel — single stub, other attrs via MRO
    _zone_id: str
    _init_cred: Any
    router: Any

    # ── Context helpers ──────────────────────────────────────────────

    def _validate_path(self, path: str, allow_root: bool = False) -> str:
        """Validate and normalize virtual path. Delegates to lib/path_utils."""
        return validate_path(path, allow_root=allow_root)

    def _parse_context(self, context: OperationContext | dict | None = None) -> OperationContext:
        """Parse context dict or OperationContext into OperationContext."""
        from nexus.lib.context_utils import parse_context

        return parse_context(context)

    def _build_rust_ctx(self, context: "OperationContext | None", is_admin: bool) -> object:
        """Build Rust OperationContext from Python context with all fields.

        Caches the built Rust ctx on the Python OperationContext instance so
        repeat calls within the same syscall chain skip 11 string field copies
        across the PyO3 boundary.  Cache key is ``is_admin`` — identity fields
        are immutable within a request.
        """
        if context is not None:
            cached = context.__dict__.get("_rust_ctx_cache")
            if cached is not None and cached[0] == is_admin:
                return cached[1]

        from nexus_kernel import OperationContext as _RustCtx

        rust_ctx = _RustCtx(
            user_id=context.user_id if context else "anonymous",
            zone_id=self._zone_id,  # routing zone (always set)
            is_admin=is_admin,
            agent_id=getattr(context, "agent_id", None) if context else None,
            is_system=getattr(context, "is_system", False) if context else False,
            groups=context.groups if context else [],
            admin_capabilities=list(context.admin_capabilities) if context else [],
            subject_type=getattr(context, "subject_type", "user") if context else "user",
            subject_id=getattr(context, "subject_id", None) if context else None,
            request_id=getattr(context, "request_id", "") if context else "",
            context_zone_id=context.zone_id if context else None,  # caller's zone
        )

        if context is not None:
            context.__dict__["_rust_ctx_cache"] = (is_admin, rust_ctx)

        return rust_ctx

    def _get_context_identity(
        self, context: OperationContext | dict | None = None
    ) -> tuple[str | None, str | None, bool]:
        """Extract (zone_id, agent_id, is_admin) from context."""
        if context is None:
            ctx = self._resolve_cred(None)
            return (ctx.zone_id, ctx.agent_id, ctx.is_admin)
        if isinstance(context, dict):
            fallback = self._resolve_cred(None)
            return (
                context.get("zone_id", fallback.zone_id),
                context.get("agent_id", fallback.agent_id),
                context.get("is_admin", fallback.is_admin),
            )
        return context.zone_id, context.agent_id, getattr(context, "is_admin", False)

    def _resolve_cred(self, context: OperationContext | None) -> OperationContext:
        """Return *context* or the kernel init_cred; raise if neither available.

        Issue #1801: kernel never fabricates identity — like Linux VFS,
        every syscall requires credentials from the caller.  Renamed from
        ``_require_context`` to reflect its role: resolve the credential
        chain (explicit → init_cred → error).
        """
        if context is not None:
            return context
        if self._init_cred is not None:
            return self._init_cred
        raise ValueError(
            "No operation context provided and no init_cred configured. "
            "Use factory create_nexus_fs(init_cred=...) or pass context= to each syscall."
        )

    def _ensure_context_ttl(self, context: OperationContext | None, ttl: float) -> OperationContext:
        """Ensure context exists and has ttl_seconds set (Issue #3405)."""
        if context is not None:
            return _dc_replace(context, ttl_seconds=ttl)
        return OperationContext(user_id="anonymous", groups=[], ttl_seconds=ttl)

    # ── Batch permission check ─────────────────────────────────────

    def _batch_permission_check(
        self,
        paths: list[str],
        context: "OperationContext | None",
        permission: str = "READ",
    ) -> set[str]:
        """Return the set of *paths* that pass stat permission hooks.

        Uses a single FFI call to Rust ``dispatch_pre_hooks_batch_stat``
        which loops N paths internally — avoiding N PyO3 boundary crossings.
        Fast path: no hooks → all paths allowed (checked in Rust).
        """
        ctx = self._resolve_cred(context)
        is_admin = getattr(ctx, "is_admin", False)
        rust_ctx = self._build_rust_ctx(ctx, is_admin)
        results = self._kernel.dispatch_pre_hooks_batch_stat(paths, rust_ctx, permission)
        return {p for p, allowed in zip(paths, results, strict=True) if allowed}

    # ── Virtual .readme/ overlay (Issue #3728) ───────────────────────

    def _try_virtual_readme_stat(
        self,
        path: str,
        ctx: OperationContext,
    ) -> dict[str, Any] | None:
        """Return a stat dict for a virtual ``.readme/`` entry, or ``None``.

        Called from ``sys_stat`` when the metastore has no entry for the
        path — if the path routes to a skill backend and falls under
        ``.readme/``, synthesize a stat dict from the virtual tree.

        Returns ``None`` for any of: path doesn't route anywhere, backend
        has no skill docs, path isn't under ``.readme/``, or normalization
        fails.  The caller treats ``None`` as "really not found" and
        continues with the normal sys_stat miss path.
        """
        if "/.readme" not in path:
            return None
        try:
            _, _, is_admin = self._get_context_identity(ctx)
            route = self.router.route(path, zone_id=self._zone_id)
        except Exception:
            return None

        backend = getattr(route, "backend", None)
        if backend is None:
            return None

        mount_point = getattr(route, "mount_point", "") or ""
        backend_path = getattr(route, "backend_path", "") or ""

        from nexus.backends.connectors.schema_generator import (
            _parse_readme_path_parts,
            _readme_dir_for,
            get_virtual_readme_tree_for_backend,
            overlay_owns_path,
        )

        # Round 5 finding #13: defer to real backend when it owns this
        # path, so ``sys_stat`` can't report virtual metadata for a file
        # that ``sys_read`` serves from real storage.  Malformed paths
        # propagate as None so sys_stat returns its normal miss result.
        try:
            _owns = overlay_owns_path(backend, mount_point, backend_path, context=ctx)
        except ValueError:
            return None
        if not _owns:
            return None

        try:
            parts = _parse_readme_path_parts(backend_path, readme_dir=_readme_dir_for(backend))
        except ValueError:
            return None  # malformed path — let sys_stat return None

        if parts is None:
            return None  # not under .readme/

        try:
            tree = get_virtual_readme_tree_for_backend(backend, mount_point)
        except Exception:
            return None

        entry = tree.find(parts)
        if entry is None:
            return None  # under .readme/ but not a known file

        backend_name = getattr(backend, "name", "") or ""
        return {
            "path": path,
            "backend_name": backend_name,
            "physical_path": "",
            "size": entry.size(),
            "etag": None,
            "mime_type": "inode/directory" if entry.is_dir else "text/markdown",
            "created_at": None,
            "modified_at": None,
            "is_directory": entry.is_dir,
            "entry_type": 1 if entry.is_dir else 0,
            "owner": ctx.user_id,
            "group": ctx.user_id,
            # Read-only — virtual files cannot be modified.
            "mode": 0o555 if entry.is_dir else 0o444,
            "version": 1,
            "zone_id": self._zone_id,
        }

    def _reject_if_virtual_readme(
        self,
        path: str,
        context: OperationContext | None,
        op: str = "write",
    ) -> None:
        """Raise ``PermissionError`` if ``path`` is under a virtual ``.readme/``.

        Issue #3728: the overlay advertises virtual files as mode 0o444
        (read-only), but the write/delete/rename/mkdir paths route on
        backend_path directly — without this guard, a caller writing to
        ``/<mount>/.readme/README.md`` would create a real file in the
        backend (e.g. a stray ``.readme/README.md`` in the user's Google
        Drive).  This helper blocks every mutating entry point at the
        kernel layer so the virtual tree cannot be mutated.

        Called from: ``sys_write``, ``write``,
        ``mkdir``, ``rmdir``, ``sys_unlink``, ``sys_rename``,
        ``write_batch``, ``delete_batch``, ``rename_batch``.
        """
        if "/.readme" not in path:
            return
        try:
            _, _, is_admin = self._get_context_identity(context)
            route = self.router.route(path, zone_id=self._zone_id)
        except Exception:
            return  # non-routable path — let the real call surface the error

        backend = getattr(route, "backend", None)
        if backend is None:
            return

        backend_path = getattr(route, "backend_path", "") or ""
        mount_point = getattr(route, "mount_point", "") or ""

        from nexus.backends.connectors.schema_generator import overlay_owns_path

        # Shared ownership check — only reject when the virtual overlay
        # is authoritative for this path.  On deferring backends
        # (native gdrive) with real data here, the mutation is a
        # legitimate operation against user data and passes through.
        # Malformed paths (traversal etc.) fall through so the real
        # call surfaces the underlying error with its own message.
        try:
            owns = overlay_owns_path(backend, mount_point, backend_path, context=context)
        except ValueError:
            return
        if not owns:
            return

        raise PermissionError(
            f"Cannot {op} virtual .readme/ path: {path} "
            f"(skill docs are read-only and generated from class metadata)"
        )

    def _try_virtual_readme_bytes(
        self,
        path: str,
        context: OperationContext | None,
    ) -> bytes | None:
        """Return the bytes for a virtual ``.readme/`` path, or ``None``.

        Synchronous sibling of ``_try_virtual_readme_stat`` used by
        ``read_bulk`` (which is sync and cannot ``await sys_read``) to
        serve virtual skill docs when the Rust fast path misses.
        """
        if "/.readme" not in path:
            return None
        try:
            _, _, is_admin = self._get_context_identity(context)
            route = self.router.route(path, zone_id=self._zone_id)
        except Exception:
            return None

        backend = getattr(route, "backend", None)
        if backend is None:
            return None

        mount_point = getattr(route, "mount_point", "") or ""
        backend_path = getattr(route, "backend_path", "") or ""

        from nexus.backends.connectors.schema_generator import dispatch_virtual_readme_read

        try:
            return dispatch_virtual_readme_read(backend, mount_point, backend_path, context=context)
        except Exception:
            return None

    # ── Post-write event dispatch ────────────────────────────────────

    def _dispatch_write_events(
        self,
        path: str,
        result: _WriteContentResult,
        content: bytes,
    ) -> dict[str, Any]:
        """Post-write event dispatch (sync, outside lock).

        Fires FileEvent notify (OBSERVE) + dispatch_post_hooks (INTERCEPT).
        Uses context from Rust sys_write result (stored in result).

        Returns:
            Dict with metadata {etag, version, modified_at, size}.
        """
        # --- Lock released — event dispatch + side effects (like Linux inotify after i_rwsem) ---

        # OBSERVE dispatch: Rust kernel fires OBSERVE via ThreadPool (§11 Phase 5).

        # INTERCEPT POST hooks
        from nexus.contracts.vfs_hooks import WriteHookContext

        _write_ctx = WriteHookContext(
            path=path,
            content=content,
            context=result.context,
            zone_id=result.zone_id,
            agent_id=result.agent_id,
            is_new_file=result.is_new,
            content_hash=result.content_hash,
            metadata=result.metadata,
            old_metadata=result.old_metadata,
            new_version=result.new_version,
        )
        self._kernel.dispatch_post_hooks("write", _write_ctx)

        # Return metadata for optimistic concurrency control
        return {
            "etag": result.content_hash,
            "version": result.new_version,
            "modified_at": result.metadata.modified_at,
            "size": result.size,
        }
