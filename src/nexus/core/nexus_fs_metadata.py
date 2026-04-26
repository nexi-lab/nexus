"""MetadataMixin — metadata operations (sys_stat, sys_setattr, sys_unlink, sys_rename, sys_copy, sys_readdir).

Extracts all file metadata operations from NexusFS. Depends on
InternalMixin (context helpers, .readme overlay) and DispatchMixin
(resolve_delete, dispatch hooks) via MRO.

Mixin rules (Phase 6 established):
  * ``from __future__ import annotations`` + TYPE_CHECKING stubs
  * Single stub: ``_kernel: Any`` — other NexusFS attrs accessed via MRO
  * Listed BEFORE NexusFilesystemABC in MRO
  * @rpc_expose decorators stay on mixin methods
  * No new ``type: ignore``
"""

from __future__ import annotations

import builtins
import logging
import time
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    BackendError,
    InvalidPathError,
    NexusFileNotFoundError,
)
from nexus.contracts.metadata import DT_DIR, DT_MOUNT, FileMetadata
from nexus.contracts.types import OperationContext
from nexus.lib.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_SENTINEL = object()  # default for _meta param in _check_is_directory


class MetadataMixin:
    """Metadata operations: sys_stat, sys_setattr, sys_unlink, sys_rename, sys_copy, sys_readdir."""

    _kernel: Any  # Rust Kernel
    _zone_id: str
    _hook_specs: dict  # Hook specs stored on NexusFS
    metadata: Any
    _driver_coordinator: Any

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _extract_rust_backend_params(backend: Any, cls_name: str) -> dict[str, Any] | None:
        """Extract typed params for Rust native backend construction.

        Returns a dict of kwargs for kernel.sys_setattr if the backend has
        a Rust-native equivalent, or None when the backend type is not recognized.
        """
        # S3 backends (PathS3Backend, CASS3Backend)
        if "S3" in cls_name:
            return {
                "backend_type": "s3",
                "s3_bucket": getattr(backend, "bucket_name", None) or "",
                "s3_prefix": getattr(backend, "prefix", None) or "",
                "aws_region": getattr(backend, "region_name", None),
                "aws_access_key": getattr(backend, "_access_key_id", None)
                or getattr(backend, "access_key_id", None),
                "aws_secret_key": getattr(backend, "_secret_access_key", None)
                or getattr(backend, "secret_access_key", None),
                "s3_endpoint": getattr(backend, "endpoint_url", None),
            }
        # GCS backends (PathGCSBackend, CASGCSBackend)
        if "GCS" in cls_name:
            return {
                "backend_type": "gcs",
                "gcs_bucket": getattr(backend, "bucket_name", None) or "",
                "gcs_prefix": getattr(backend, "prefix", None) or "",
                "access_token": getattr(backend, "access_token", None),
            }
        # GDrive connector
        if "GDrive" in cls_name or "Gdrive" in cls_name:
            token = getattr(backend, "_access_token", None) or getattr(
                backend, "access_token", None
            )
            if token:
                return {
                    "backend_type": "gdrive",
                    "access_token": token,
                    "root_folder_id": getattr(backend, "root_folder_id", None) or "root",
                }
        # Gmail connector
        if "Gmail" in cls_name:
            token = getattr(backend, "_access_token", None) or getattr(
                backend, "access_token", None
            )
            if token:
                return {
                    "backend_type": "gmail",
                    "access_token": token,
                }
        # Slack connector
        if "Slack" in cls_name:
            token = getattr(backend, "bot_token", None) or getattr(backend, "_bot_token", None)
            if token:
                return {
                    "backend_type": "slack",
                    "bot_token": token,
                    "default_channel": getattr(backend, "default_channel", None) or "",
                }
        # HN connector (PathHNBackend)
        if "HN" in cls_name and "Backend" in cls_name:
            return {
                "backend_type": "hn",
                "hn_stories_per_feed": getattr(backend, "stories_per_feed", 10),
                "hn_include_comments": getattr(backend, "include_comments", True),
            }
        # X/Twitter connector (PathXBackend)
        if cls_name == "PathXBackend":
            # X uses OAuth — extract token from transport if available
            transport = getattr(backend, "_transport", None) or getattr(
                backend, "_hn_transport", None
            )
            token = getattr(transport, "_bearer_token", None) if transport else None
            return {
                "backend_type": "x",
                "x_bearer_token": token or "",
            }
        # Calendar OAuth connector (PathCalendarBackend) → route through CLI "gws calendar"
        if "Calendar" in cls_name and "Backend" in cls_name:
            import json as _json

            token = getattr(backend, "_access_token", None) or getattr(
                backend, "access_token", None
            )
            auth_env: dict[str, str] = {}
            if token:
                auth_env["GWS_ACCESS_TOKEN"] = token
            return {
                "backend_type": "cli",
                "cli_command": "gws",
                "cli_service": "calendar",
                "cli_auth_env_json": _json.dumps(auth_env) if auth_env else "",
            }
        # CLI-based connectors (GitHubConnector, SheetsConnector, DocsConnector, etc.)
        cli_name = getattr(backend, "CLI_NAME", None)
        if isinstance(cli_name, str) and cli_name:
            import json as _json

            cli_service = getattr(backend, "CLI_SERVICE", "") or ""
            # Build auth env from token if available
            cli_auth: dict[str, str] = {}
            env_key = f"{cli_name.upper().replace('-', '_')}_ACCESS_TOKEN"
            # Try to get token from backend's token manager
            _tm = getattr(backend, "_token_manager", None)
            if _tm:
                try:
                    _tok = _tm.get_cached_token(provider=cli_name)
                    if _tok:
                        cli_auth[env_key] = _tok
                except Exception:
                    pass
            return {
                "backend_type": "cli",
                "cli_command": cli_name,
                "cli_service": cli_service,
                "cli_auth_env_json": _json.dumps(cli_auth) if cli_auth else "",
            }
        return None

    def _get_parent_path(self, path: str) -> str | None:
        """Get parent directory path, or None if root."""
        if path == "/":
            return None

        # Remove trailing slash if present
        path = path.rstrip("/")

        # Find last slash
        last_slash = path.rfind("/")
        if last_slash == 0:
            # Parent is root
            return "/"
        elif last_slash > 0:
            return path[:last_slash]
        else:
            # No parent (shouldn't happen for valid paths)
            return None

    def _ensure_parent_directories(self, path: str, ctx: OperationContext) -> None:
        """Create metadata entries for all parent directories that don't exist.

        Walks from the immediate parent of *path* upward toward ``/``, collecting
        every path that has no metastore entry, then creates directory metadata
        entries from top to bottom (shallowest first) so that ``sys_readdir``
        lists them correctly.

        This is factored out of ``mkdir`` so it can be called both on the
        normal code-path *and* on the early-return path when the target path
        already exists (e.g. a DT_MOUNT entry written by ``MountTable.add()``).
        """
        parent_path = self._get_parent_path(path)
        parents_to_create: list[str] = []

        while parent_path and parent_path != "/":
            if not self.metadata.exists(parent_path):
                parents_to_create.append(parent_path)
            else:
                break
            parent_path = self._get_parent_path(parent_path)

        for parent_dir in reversed(parents_to_create):
            self._kernel.sys_setattr(
                parent_dir,
                DT_DIR,
                zone_id=ctx.zone_id or ROOT_ZONE_ID,
            )

    def _check_is_directory(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
        _meta: Any = _SENTINEL,
    ) -> bool:
        """Internal: check if path is a directory (explicit or implicit).

        Synchronous check used by sys_stat.

        Args:
            _meta: Pre-fetched FileMetadata from caller (avoids duplicate
                metadata.get). Pass ``None`` to indicate "already looked up,
                not found". Omit to let this method fetch it.
        """
        try:
            path = self._validate_path(path)
            ctx = self._resolve_cred(context)

            # Check if it's an implicit directory first (for optimization)
            is_implicit_dir = self.metadata.is_implicit_directory(path)

            # Permission check via KernelDispatch INTERCEPT hook.
            from nexus.contracts.exceptions import PermissionDeniedError
            from nexus.contracts.vfs_hooks import StatHookContext as _SHC

            try:
                self._kernel.dispatch_pre_hooks(
                    "stat",
                    _SHC(
                        path=path,
                        context=ctx,
                        permission="TRAVERSE" if is_implicit_dir else "READ",
                        extra={"is_implicit_directory": is_implicit_dir},
                    ),
                )
            except PermissionDeniedError:
                return False

            # Use pre-fetched meta if provided, otherwise fetch
            meta = self.metadata.get(path) if _meta is _SENTINEL else _meta
            if meta is not None and (meta.is_dir or meta.is_mount or meta.is_external_storage):
                return True

            # Metadata check + implicit dir detection covers all cases.
            # Rust sys_stat handles backend-level directory detection.
            return is_implicit_dir
        except (InvalidPathError, NexusFileNotFoundError):
            return False

    @rpc_expose(description="Check if path is a directory")
    def is_directory(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
    ) -> bool:
        """Tier 2: convenience wrapper — derives from sys_stat.

        Equivalent to ``(await sys_stat(path)).get("is_directory", False)``.
        """
        try:
            stat = self.sys_stat(path, context=context)
            return stat is not None and stat.get("is_directory", False)
        except (InvalidPathError, NexusFileNotFoundError):
            return False

    # ── Tier 1 syscalls ───────────────────────────────────────────────

    @rpc_expose(description="Get available namespaces")
    def get_top_level_mounts(self, context: OperationContext | None = None) -> builtins.list[str]:
        """Return top-level mount names visible to the current user.

        Reads DT_MOUNT entries from metastore (kernel's single source of
        truth for mount points).
        """
        self._resolve_cred(context)
        names: set[str] = set()
        for meta in self.metadata.list("/"):
            if not (meta.is_mount or meta.is_external_storage):
                continue
            top = meta.path.lstrip("/").split("/")[0]
            if not top:
                continue
            names.add(top)
        return sorted(names)

    @rpc_expose(description="Get file metadata for FUSE operations")
    def sys_stat(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any] | None:
        """Get file metadata without reading content (FUSE getattr).

        Lock info is always included (Rust LockManager lookup is free).
        ``include_lock`` kwarg accepted for backward compat but ignored.
        """
        ctx = self._resolve_cred(context)
        normalized = self._validate_path(path, allow_root=True)

        # Build the base stat via a single code path. F3 C1 guarantees a
        # metastore is always wired (``Kernel::new()`` installs
        # ``MemoryMetastore`` by default), so the Rust kernel is the
        # authoritative source for explicit entries — a second
        # ``self.metadata.get`` would be a TOCTOU duplicate with a
        # different view of the per-mount ``ZoneMetastore`` in
        # federation mode. The two ``None``-returning cases handled
        # after the kernel call are the ones the kernel cannot see:
        # implicit directories (paths with children but no explicit
        # entry) and the Python-side ``.readme/`` virtual-doc overlay
        # (Issue #3728).
        # Rust sys_stat handles: dcache → metastore → implicit directory.
        result = self._kernel.sys_stat(normalized, self._zone_id)
        if result is not None:
            result["owner"] = ctx.user_id
            result["group"] = ctx.user_id
        else:
            return None

        return result

    @rpc_expose(description="Upsert file metadata attributes")
    def sys_setattr(
        self,
        path: str,
        *,
        context: OperationContext | None = None,  # noqa: ARG002
        **attrs: Any,
    ) -> dict[str, Any]:
        """Upsert file metadata (chmod/chown/utimensat + mknod analog).

        Rust kernel handles ALL filesystem entry types. Python dispatches
        ``/__sys__/`` (ServiceRegistry) before the Rust call.

        Upsert semantics — create-on-write for metadata:
        - Path missing + entry_type provided → CREATE inode
        - Path missing + no entry_type → NexusFileNotFoundError
        - Path exists + no entry_type → UPDATE mutable fields
        - Path exists + same entry_type (DT_PIPE/DT_STREAM) → IDEMPOTENT OPEN (recover buffer)
        - Path exists + different entry_type → PermissionDenied (immutable after creation)

        Args:
            path: Virtual file path. Paths under ``/__sys__/`` are kernel
            management operations (service/hook registration), not filesystem
            metadata.
            context: Operation context.
            **attrs: Metadata attributes. Include ``entry_type`` to create.

        Returns:
            Dict with path, created flag, and type-specific fields.
        """
        # ── /__sys__/ kernel management dispatch ──────────────────────
        # Service registration via syscall. These paths bypass the normal
        # metastore path — kernel routes them to ServiceRegistry.
        if path.startswith("/__sys__/services/"):
            name = path.rsplit("/", 1)[-1]
            service = attrs.get("service")
            if service is None:
                raise ValueError(
                    f"sys_setattr(/__sys__/services/{name}) requires 'service' attribute"
                )
            exports = attrs.get("exports", ())
            allow_overwrite = attrs.get("allow_overwrite", False)
            self._kernel.service_enlist(name, service, list(exports), allow_overwrite)
            # Auto-capture hooks via duck-typed hook_spec()
            from nexus.core.nexus_fs import _declares_hook_spec, _register_hooks_for_spec

            if _declares_hook_spec(service):
                spec = service.hook_spec()
                if spec is not None and not spec.is_empty:
                    self._hook_specs[name] = spec
                    _register_hooks_for_spec(self, spec)
            return {"path": path, "registered": True, "service": name}

        entry_type = attrs.get("entry_type", 0)
        # DT_MOUNT allows root "/" (root mount); other types don't.
        path = self._validate_path(path, allow_root=(entry_type == DT_MOUNT))

        # ── DT_MOUNT: resolve backend params for Rust kernel ─────────
        if entry_type == DT_MOUNT:
            backend_type = attrs.get("backend_type", "cas")
            backend = attrs.get("backend")
            zone_id = attrs.get("zone_id", ROOT_ZONE_ID)
            metastore = attrs.get("metastore")

            # Rust-native backends — Rust owns the ObjectStore; no Python shim.
            # `backend_type="openai"` / `"anthropic"` / `"remote"` triggers
            # the native construction path in `PyKernel::sys_setattr` via
            # the typed kwarg passthrough below.
            if backend_type == "remote" and backend is None:
                _backend_name = attrs.get("backend_name", "remote")
                result = self._kernel.sys_setattr(
                    path,
                    entry_type,
                    _backend_name,
                    backend_type="remote",
                    server_address=attrs.get("server_address"),
                    remote_auth_token=attrs.get("remote_auth_token"),
                    remote_ca_pem=attrs.get("remote_ca_pem"),
                    remote_cert_pem=attrs.get("remote_cert_pem"),
                    remote_key_pem=attrs.get("remote_key_pem"),
                    remote_timeout=float(attrs.get("remote_timeout", 90.0)),
                    zone_id=zone_id,
                )
                return result

            # LLM backends — Rust owns the ObjectStore; no Python shim.
            if backend_type in ("openai", "anthropic") and backend is None:
                _backend_name = attrs.get("backend_name", backend_type)
                result = self._kernel.sys_setattr(
                    path,
                    entry_type,
                    _backend_name,
                    backend_type=backend_type,
                    openai_base_url=attrs.get("openai_base_url"),
                    openai_api_key=attrs.get("openai_api_key"),
                    openai_model=attrs.get("openai_model"),
                    openai_blob_root=attrs.get("openai_blob_root"),
                    anthropic_base_url=attrs.get("anthropic_base_url"),
                    anthropic_api_key=attrs.get("anthropic_api_key"),
                    anthropic_model=attrs.get("anthropic_model"),
                    anthropic_blob_root=attrs.get("anthropic_blob_root"),
                    zone_id=zone_id,
                )
                return result

            if backend is None:
                raise ValueError(
                    "sys_setattr(entry_type=DT_MOUNT) requires 'backend' attribute "
                    "(pre-constructed ObjectStoreABC instance)"
                )
            _backend_name = backend.name if isinstance(backend.name, str) else str(backend.name)

            # R20.18.6: federation DT_MOUNT auto-resolves its raft backing via
            # kernel-internal `resolve_federation_mount_backing`; no Python
            # ZoneHandle crosses the PyO3 boundary here. Non-federation mounts
            # may still ship a LocalMetastore redb path.
            _ms_path = getattr(metastore, "_redb_path", None) if metastore is not None else None
            _ms_path_str = str(_ms_path) if _ms_path else None

            _is_external = bool(attrs.get("is_external", False))

            # ── Rust native backend detection ────────────────────────
            # For connectors with Rust-native backends, extract typed params
            # from the Python instance so Rust constructs the backend without
            # All backends are Rust-native now.
            _cls_name = type(backend).__name__
            _rust_typed = self._extract_rust_backend_params(backend, _cls_name)
            if _rust_typed is not None:
                result = self._kernel.sys_setattr(
                    path,
                    entry_type,
                    _backend_name,
                    zone_id=zone_id,
                    metastore_path=_ms_path_str,
                    is_external=_is_external,
                    **_rust_typed,
                )
                return result

            # ── Local backend detection — Rust takes ownership natively ──
            # CASLocalBackend has root_path; PathLocalBackend has root_path;
            # LocalConnectorBackend has local_path. Rust constructs the matching
            # backend from local_root param.
            _root = getattr(backend, "root_path", None)
            if _root is None:
                _root = getattr(backend, "local_path", None)
            if _root is None:
                # No local root and not matched by _extract_rust_backend_params.
                # Mount a kernel-only entry (no Rust backend); Python DLC holds
                # the backend ref for RouteResult.  This path handles test mocks
                # and any future connectors not yet ported to Rust.
                logger.debug(
                    "No Rust-native backend for %s — kernel-only mount",
                    _cls_name,
                )
                result = self._kernel.sys_setattr(
                    path,
                    entry_type,
                    _backend_name,
                    zone_id=zone_id,
                    metastore_path=_ms_path_str,
                    is_external=_is_external,
                )
                return result
            _local_root = str(_root)

            # Determine local backend type for Rust dispatch
            if "LocalConnector" in _cls_name:
                _local_type = "local_connector"
            elif "PathLocal" in _cls_name:
                _local_type = "path_local"
            else:
                _local_type = "cas"  # CASLocalBackend (default)

            result = self._kernel.sys_setattr(
                path,
                entry_type,
                _backend_name,
                local_root=_local_root,
                backend_type=_local_type,
                fsync=True,
                zone_id=zone_id,
                metastore_path=_ms_path_str,
                is_external=_is_external,
            )
            return result

        # ── All other FS types → Rust kernel sys_setattr ─────────────
        capacity = attrs.get("capacity", 65_536)
        io_profile = attrs.get("io_profile", "memory")
        mime_type = attrs.get("mime_type")
        modified_at_ms = attrs.get("modified_at_ms")
        zone_id = attrs.get("zone_id", ROOT_ZONE_ID)

        result = self._kernel.sys_setattr(
            path,
            entry_type,
            zone_id=zone_id,
            io_profile=io_profile,
            capacity=capacity,
            mime_type=mime_type,
            modified_at_ms=modified_at_ms,
            read_fd=attrs.get("read_fd"),
            write_fd=attrs.get("write_fd"),
        )

        return result

    @rpc_expose(description="Get ETag (content hash) for HTTP caching")
    def get_etag(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> str | None:
        """Get content hash for HTTP If-None-Match checks."""
        _ = context  # Reserved for future use
        normalized = self._validate_path(path, allow_root=False)

        # Get file metadata (lightweight - doesn't read content)
        file_meta = self.metadata.get(normalized)
        if file_meta is None:
            return None

        # Return the etag (content_hash) from metadata
        return file_meta.etag

    # ── Tier 2 directory ──────────────────────────────────────────────

    @rpc_expose(description="Create directory")
    def mkdir(
        self,
        path: str,
        parents: bool = True,
        exist_ok: bool = True,
        *,
        context: OperationContext | None = None,
    ) -> None:
        """Create a directory (Tier 2 convenience over sys_setattr).

        Defaults: parents=True, exist_ok=True (mkdir -p semantics).
        DT_DIR metadata creation delegated to Rust kernel sys_setattr.
        """
        path = self._validate_path(path)
        ctx = self._resolve_cred(context)

        # Rust kernel handles existence check (explicit + implicit directory),
        # exist_ok/parents semantics, backend.mkdir, ensure_parent_directories,
        # DT_DIR metadata creation, and dcache update.
        _rust_ctx = self._build_rust_ctx(ctx, ctx.is_admin)
        _mkdir_result = self._kernel.sys_mkdir(path, _rust_ctx, parents, exist_ok)
        if _mkdir_result.post_hook_needed:
            from nexus.contracts.vfs_hooks import MkdirHookContext

            self._kernel.dispatch_post_hooks(
                "mkdir",
                MkdirHookContext(
                    path=path,
                    context=ctx,
                    zone_id=ctx.zone_id,
                    agent_id=ctx.agent_id,
                ),
            )

    @rpc_expose(description="Remove directory")
    def rmdir(
        self,
        path: str,
        recursive: bool = True,
        context: OperationContext | None = None,
    ) -> None:
        """Remove a directory with lenient defaults (Tier 2 convenience).

        Defaults to recursive=True (rm -rf semantics).
        Delegates directly to sys_unlink.
        """
        self.sys_unlink(path, recursive=recursive, context=context)

    # ── Tier 1 delete/rename/copy ─────────────────────────────────────

    @rpc_expose(description="Delete file")
    def sys_unlink(
        self,
        path: str,
        *,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Remove a file or directory entry.

        Unified delete syscall — handles both files and directories.
        For directories, set ``recursive=True`` to delete non-empty dirs.

        Args:
            path: Virtual path to delete (supports memory, pipe, stream paths).
            recursive: If True and target is a directory, delete all children
                first (rm -rf). If False and directory is non-empty, raises
                OSError(ENOTEMPTY). Ignored for regular files.
            context: Optional operation context for permission checks.

        Returns:
            Dict on success.

        Raises:
            NexusFileNotFoundError: If file doesn't exist.
            InvalidPathError: If path is invalid.
            BackendError: If delete operation fails.
            OSError(ENOTEMPTY): If directory is non-empty and recursive=False.
            PermissionError: If path is read-only or user doesn't have write permission.
        """
        # ── /__sys__/ kernel management dispatch ──────────────────────
        if path.startswith("/__sys__/services/"):
            name = path.rsplit("/", 1)[-1]
            # Unregister hooks first
            from nexus.core.nexus_fs import _unregister_hooks_for_spec

            spec = self._hook_specs.pop(name, None)
            if spec is not None:
                _unregister_hooks_for_spec(self, spec)
            self._kernel.service_unregister(name)
            return {"path": path, "unregistered": True, "service": name}

        path = self._validate_path(path)

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _result = self.resolve_delete(path, context=context)
        if _handled:
            return _result

        # ── Call Rust first — handles DT_REG, DT_PIPE, DT_STREAM ────
        zone_id, agent_id, is_admin = self._get_context_identity(context)
        _rust_ctx = self._build_rust_ctx(context, is_admin)
        _unlink_result = self._kernel.sys_unlink(path, _rust_ctx)

        if _unlink_result.hit:
            # Rust handled: DT_REG (file delete), DT_PIPE (pipe destroy),
            # DT_STREAM (stream destroy). Fire POST-hooks and return.
            if _unlink_result.post_hook_needed:
                from nexus.contracts.vfs_hooks import DeleteHookContext

                self._kernel.dispatch_post_hooks(
                    "delete",
                    DeleteHookContext(
                        path=path,
                        context=context,
                        zone_id=zone_id,
                        agent_id=agent_id,
                    ),
                )
            return {}

        # ── Rust miss: branch on entry_type ──────────────────────────
        et = _unlink_result.entry_type
        if et == 0:
            # Not found in metastore/dcache
            raise NexusFileNotFoundError(path)

        # DT_DIR: delegate to Rust sys_rmdir (recursive child delete,
        # backend rmdir, dcache evict, observer dispatch).
        from nexus.contracts.metadata import DT_DIR as _DT_DIR

        if et == _DT_DIR:
            _rmdir_result = self._kernel.sys_rmdir(path, _rust_ctx, recursive)
            if _rmdir_result.post_hook_needed:
                from nexus.contracts.vfs_hooks import RmdirHookContext

                ctx = self._resolve_cred(context)
                self._kernel.dispatch_post_hooks(
                    "rmdir",
                    RmdirHookContext(
                        path=path,
                        context=ctx,
                        zone_id=zone_id,
                        agent_id=agent_id,
                        recursive=recursive,
                    ),
                )
            return {}

        # DT_MOUNT (2) / DT_EXTERNAL_STORAGE (5): unmount via DLC (Python service-tier)
        if et in (2, 5):
            ctx = self._resolve_cred(context)
            from nexus.contracts.vfs_hooks import RmdirHookContext

            self._kernel.dispatch_pre_hooks("rmdir", RmdirHookContext(path=path, context=ctx))
            removed = self._driver_coordinator.unmount(path)
            if removed:
                self.metadata.delete(path)
                logger.info("sys_unlink: unmounted %s", path)
            return {}

        # Unknown entry type — should not happen
        logger.warning("sys_unlink: unexpected entry_type=%d for %s", et, path)
        return {}

    @rpc_expose(description="Rename/move file")
    def sys_rename(
        self,
        old_path: str,
        new_path: str,
        *,
        force: bool = False,  # noqa: ARG002 — forwarded to Rust when kernel supports it
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """
        Rename/move a file by updating its path in metadata.

        This is a metadata-only operation that does NOT copy file content.
        The file's content remains in the same location in CAS storage,
        only the virtual path is updated in the metadata database.

        This makes rename/move operations instant, regardless of file size.

        Args:
            old_path: Current virtual path
            new_path: New virtual path
            force: If True, delete the destination before renaming (overwrite).
            context: Optional operation context for permission checks (uses default if not provided)

        Returns:
            Empty dict on success.

        Raises:
            NexusFileNotFoundError: If source file doesn't exist
            FileExistsError: If destination path already exists (and force=False)
            InvalidPathError: If either path is invalid
            PermissionError: If either path is read-only
            AccessDeniedError: If access is denied (zone isolation)

        Example:
            >>> nx.sys_rename('/workspace/old.txt', '/workspace/new.txt')
            >>> nx.sys_rename('/folder-a/file.txt', '/shared/folder-a/file.txt')
        """
        old_path = self._validate_path(old_path)
        new_path = self._validate_path(new_path)
        # Normalize context dict to OperationContext dataclass (CLI passes dicts)
        context = self._parse_context(context)

        zone_id, agent_id, is_admin = self._get_context_identity(context)

        # PRE-INTERCEPT hooks dispatched by Rust kernel
        _rust_ctx = self._build_rust_ctx(context, is_admin)
        _rename_result = self._kernel.sys_rename(old_path, new_path, _rust_ctx)

        # Rust handles all entry types (files, dirs, mounts, external storage).
        # Dispatch POST hooks with reconstructed metadata for audit trail.
        if _rename_result.post_hook_needed:
            from nexus.contracts.metadata import FileMetadata as _FM
            from nexus.contracts.vfs_hooks import RenameHookContext

            # Reconstruct old metadata from Rust result fields for the
            # audit trail (record_store_write_observer uses .etag + .to_dict()).
            _old_meta: _FM | None = None
            if _rename_result.old_etag is not None or _rename_result.old_size is not None:
                from datetime import UTC, datetime

                _mod_at = (
                    datetime.fromtimestamp(_rename_result.old_modified_at_ms / 1000.0, UTC)
                    if _rename_result.old_modified_at_ms is not None
                    else None
                )
                _old_meta = _FM(
                    path=old_path,
                    backend_name="",
                    physical_path=_rename_result.old_etag or "",
                    size=_rename_result.old_size or 0,
                    etag=_rename_result.old_etag,
                    version=_rename_result.old_version or 1,
                    modified_at=_mod_at,
                )

            _rename_ctx = RenameHookContext(
                old_path=old_path,
                new_path=new_path,
                context=context,
                zone_id=zone_id,
                agent_id=agent_id,
                is_directory=bool(_rename_result.is_directory),
                metadata=_old_meta,
            )
            self._kernel.dispatch_post_hooks("rename", _rename_ctx)

        return {}

    # ------------------------------------------------------------------
    # sys_copy — Issue #3329 (Workstream 3: native copy/move)
    # ------------------------------------------------------------------

    @rpc_expose(description="Copy file with native backend support")
    def sys_copy(
        self, src_path: str, dst_path: str, *, context: OperationContext | None = None
    ) -> dict[str, Any]:
        """Copy a file from src_path to dst_path.

        Uses the optimal strategy based on backend capabilities:
        - **Same backend, path-addressed**: Backend-native server-side copy
          (S3 CopyObject / GCS rewrite). Zero client bandwidth.
        - **Same backend, CAS**: Metadata duplication — the content blob
          is already deduplicated, so no I/O is needed.
        - **Cross-backend**: Read from source, write to destination.
          Bounded by ``NEXUS_FS_MAX_INMEMORY_SIZE`` (1 GB).

        Args:
            src_path: Source virtual path.
            dst_path: Destination virtual path.
            context: Operation context for permission checks.

        Returns:
            Dict with path, size, etag of the new file.

        Raises:
            NexusFileNotFoundError: If source file doesn't exist.
            FileExistsError: If destination path already exists.
            PermissionError: If source or destination is read-only.
            ValueError: If cross-backend copy exceeds size limit.
        """
        src_path = self._validate_path(src_path)
        dst_path = self._validate_path(dst_path)
        context = self._parse_context(context)

        zone_id, agent_id, is_admin = self._get_context_identity(context)

        # PRE-INTERCEPT hooks dispatched by Rust kernel via sys_copy.
        # Rust validates source existence + rejects directories internally.
        _rust_ctx = self._build_rust_ctx(context, is_admin)
        _copy_result = self._kernel.sys_copy(src_path, dst_path, _rust_ctx)

        # POST-INTERCEPT hooks (zero consumers use metadata field)
        if _copy_result.post_hook_needed:
            from nexus.contracts.vfs_hooks import CopyHookContext

            _copy_ctx = CopyHookContext(
                src_path=src_path,
                dst_path=dst_path,
                context=context,
                zone_id=zone_id,
                agent_id=agent_id,
                metadata=None,
            )
            self._kernel.dispatch_post_hooks("copy", _copy_ctx)

        if _copy_result.hit:
            return {
                "src_path": src_path,
                "dst_path": dst_path,
                "size": _copy_result.size,
                "etag": _copy_result.etag,
                "version": _copy_result.version,
            }

        # Python fallback — Rust sys_copy returned miss (should be rare)
        logger.debug("sys_copy miss for %s → %s, falling back to Python", src_path, dst_path)
        if self.metadata.exists(dst_path):
            raise FileExistsError(f"Destination path already exists: {dst_path}")

        # Read source content and write to destination (no VFS lock needed —
        # Rust kernel handles I/O locking internally via sys_read/sys_write).
        src_content = self.sys_read(src_path, context=context)
        write_result = self.write(dst_path, src_content, context=context)
        return {
            "src_path": src_path,
            "dst_path": dst_path,
            "size": len(src_content),
            "etag": write_result.get("etag"),
            "version": write_result.get("version"),
        }

    # ── Tier 2 metadata ──────────────────────────────────────────────

    @rpc_expose(description="Get file metadata without reading content")
    def stat(self, path: str, context: OperationContext | None = None) -> dict[str, Any]:
        """
        Get file metadata without reading the file content.

        This is useful for getting file size before streaming, or checking
        file properties without the overhead of reading large files.

        Args:
            path: Virtual path to stat
            context: Optional operation context for permission checks

        Returns:
            Dict with file metadata:
                - size: File size in bytes
                - etag: Content hash
                - version: Version number
                - modified_at: Last modification timestamp
                - is_directory: Whether path is a directory

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If user doesn't have read permission

        Example:
            >>> info = nx.stat("/workspace/large_file.bin")
            >>> print(f"File size: {info['size']} bytes")
        """
        path = self._validate_path(path)

        # Check if it's an implicit directory first (for permission check optimization)
        is_implicit_dir = self.metadata.is_implicit_directory(path)

        # Issue #1815: permission check via KernelDispatch INTERCEPT hook.
        ctx = self._resolve_cred(context)
        if is_implicit_dir:
            from nexus.contracts.exceptions import PermissionDeniedError
            from nexus.contracts.vfs_hooks import StatHookContext as _SHC

            try:
                self._kernel.dispatch_pre_hooks(
                    "stat",
                    _SHC(
                        path=path,
                        context=ctx,
                        permission="TRAVERSE",
                        extra={"is_implicit_directory": True},
                    ),
                )
            except PermissionDeniedError:
                raise PermissionError(
                    f"Access denied: User '{ctx.user_id}' does not have TRAVERSE "
                    f"permission for '{path}'"
                ) from None
        else:
            from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

            self._kernel.dispatch_pre_hooks("read", _RHC(path=path, context=context))

        # Return directory info for implicit directories
        if is_implicit_dir:
            return {
                "size": 0,
                "etag": None,
                "version": None,
                "modified_at": None,
                "is_directory": True,
            }

        # Get file metadata
        meta = self.metadata.get(path)
        if meta is None:
            raise NexusFileNotFoundError(path)

        # Get size from backend if not in metadata
        size = meta.size
        if size is None and meta.etag and meta.is_external_storage:
            # External connectors: try Rust kernel sys_stat for size.
            # CAS backends always have size set in metastore by sys_write.
            try:
                _stat = self._kernel.sys_stat(path, self._zone_id)
                size = _stat.get("size") if isinstance(_stat, dict) else None
            except Exception as exc:
                logger.debug("Failed to get content size for %s: %s", path, exc)
                size = None

        # Convert datetime to ISO string for wire compatibility with Rust FUSE client
        # The client expects a plain string, not the wrapped {"__type__": "datetime", ...} format
        modified_at_str = meta.modified_at.isoformat() if meta.modified_at else None

        return {
            "size": size,
            "etag": meta.etag,
            "version": meta.version,
            "modified_at": modified_at_str,
            "is_directory": False,
        }

    @rpc_expose(description="Get metadata for multiple files in bulk")
    def stat_bulk(
        self,
        paths: list[str],
        context: OperationContext | None = None,
        skip_errors: bool = True,
    ) -> dict[str, dict[str, Any] | None]:
        """
        Get metadata for multiple files in a single RPC call.

        This is optimized for bulk operations where many file stats are needed.
        It batches permission checks and metadata lookups for better performance.

        Args:
            paths: List of virtual paths to stat
            context: Optional operation context for permission checks
            skip_errors: If True, skip files that can't be stat'd and return None.
                        If False, raise exception on first error.

        Returns:
            Dict mapping path -> stat dict (or None if skip_errors=True and stat failed)
            Each stat dict contains: size, etag, version, modified_at, is_directory

        Performance:
            - Single RPC call instead of N calls
            - Batch permission checks (one DB query instead of N)
            - Batch metadata lookups
            - Expected speedup: 10-50x for 100+ files
        """

        bulk_start = time.time()
        results: dict[str, dict[str, Any] | None] = {}

        # Validate all paths
        validated_paths = []
        for path in paths:
            try:
                validated_path = self._validate_path(path)
                validated_paths.append(validated_path)
            except Exception as exc:
                logger.debug("Path validation failed in metadata_bulk for %s: %s", path, exc)
                if skip_errors:
                    results[path] = None
                    continue
                raise

        if not validated_paths:
            return results

        # Batch permission check via shared helper (hook_count fast path).
        perm_start = time.time()
        try:
            allowed_set = self._batch_permission_check(validated_paths, context)
        except Exception as e:
            logger.error("[STAT-BULK] Permission check failed: %s", e)
            if not skip_errors:
                raise
            allowed_set = set()

        perm_elapsed = time.time() - perm_start
        logger.info(
            f"[STAT-BULK] Permission check: {len(allowed_set)}/{len(validated_paths)} allowed in {perm_elapsed * 1000:.1f}ms"
        )

        # Mark denied files
        for path in validated_paths:
            if path not in allowed_set:
                results[path] = None

        # Batch metadata lookup - single SQL query for all paths
        meta_start = time.time()

        # Batch fetch metadata for all files in single query
        # Note: We assume paths are files (not implicit directories) since stat_bulk
        # is typically called on paths returned by list(). If a path isn't found,
        # we check if it's an implicit directory as a fallback.
        try:
            batch_meta = self.metadata.get_batch(list(allowed_set))
            for path, meta in batch_meta.items():
                if meta is None:
                    # Path not found in metadata - check if it's an implicit directory
                    if self.metadata.is_implicit_directory(path):
                        results[path] = {
                            "size": 0,
                            "etag": None,
                            "version": None,
                            "modified_at": None,
                            "is_directory": True,
                        }
                    elif skip_errors:
                        results[path] = None
                    else:
                        raise NexusFileNotFoundError(path)
                else:
                    modified_at_str = meta.modified_at.isoformat() if meta.modified_at else None
                    results[path] = {
                        "size": meta.size,
                        "etag": meta.etag,
                        "version": meta.version,
                        "modified_at": modified_at_str,
                        "is_directory": False,
                    }
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            logger.warning("[STAT-BULK] Batch metadata failed: %s: %s", type(e).__name__, e)
            if not skip_errors:
                raise

        meta_elapsed = time.time() - meta_start
        bulk_elapsed = time.time() - bulk_start

        logger.info(
            f"[STAT-BULK] Completed: {len(results)} files in {bulk_elapsed * 1000:.1f}ms "
            f"(perm={perm_elapsed * 1000:.0f}ms, meta={meta_elapsed * 1000:.0f}ms)"
        )

        return results

    @rpc_expose(description="Check if file exists")
    def access(self, path: str, *, context: OperationContext | None = None) -> bool:
        """Tier 2: check if path explicitly exists and is accessible.

        Returns True if path has explicit metadata or is an implicit directory,
        False otherwise. Unlike sys_stat, does NOT synthesize directory entries.
        """
        try:
            path = self._validate_path(path)
            ctx = self._resolve_cred(context)

            is_implicit_dir = self.metadata.is_implicit_directory(path)

            # Permission check via stat hook (same as _check_is_directory)
            from nexus.contracts.exceptions import PermissionDeniedError
            from nexus.contracts.vfs_hooks import StatHookContext as _SHC

            try:
                self._kernel.dispatch_pre_hooks(
                    "stat",
                    _SHC(
                        path=path,
                        context=ctx,
                        permission="TRAVERSE" if is_implicit_dir else "READ",
                        extra={"is_implicit_directory": is_implicit_dir},
                    ),
                )
            except PermissionDeniedError:
                return False

            # Rust kernel fast-path: dcache hit → redb metastore fallback
            if getattr(self, "_kernel", None) is not None and self._kernel.access(
                path, self._zone_id
            ):
                return True

            if self.metadata.exists(path):
                return True
            # Fallback: check Rust dcache/metastore (sys_write only updates Rust side)
            try:
                _stat = self.sys_stat(path, context=context)
                if _stat is not None:
                    return True
            except Exception:
                pass
            # Check implicit directory (path has children but no explicit entry)
            return bool(is_implicit_dir)
        except (InvalidPathError, NexusFileNotFoundError, BackendError):
            return False

    @rpc_expose(description="Check existence of multiple paths in single call")
    def exists_batch(
        self, paths: list[str], context: OperationContext | None = None
    ) -> dict[str, bool]:
        """
        Check existence of multiple paths in a single call (Issue #859).

        This reduces network round trips when checking many paths at once.
        Processing 10 paths requires 1 round trip instead of 10.

        Args:
            paths: List of virtual paths to check
            context: Operation context for permission checks (uses default if None)

        Returns:
            Dictionary mapping each path to its existence status (True/False)

        Performance:
            - Single RPC call instead of N calls
            - 10x fewer round trips for multi-path operations
            - Each path is checked independently (errors don't affect others)

        Examples:
            >>> results = nx.exists_batch(["/file1.txt", "/file2.txt", "/missing.txt"])
            >>> print(results)
            {"/file1.txt": True, "/file2.txt": True, "/missing.txt": False}
        """
        results: dict[str, bool] = {}
        for path in paths:
            try:
                results[path] = self.access(path, context=context)
            except Exception as exc:
                # Any error means file doesn't exist or isn't accessible
                logger.debug("Exists check failed for %s: %s", path, exc)
                results[path] = False
        return results

    @rpc_expose(description="Get metadata for multiple paths in single call")
    def metadata_batch(
        self, paths: list[str], context: OperationContext | None = None
    ) -> dict[str, dict[str, Any] | None]:
        """
        Get metadata for multiple paths in a single call (Issue #859).

        This reduces network round trips when fetching metadata for many files.
        Processing 10 paths requires 1 round trip instead of 10.

        Args:
            paths: List of virtual paths to get metadata for
            context: Operation context for permission checks (uses default if None)

        Returns:
            Dictionary mapping each path to its metadata dict or None if not found.
            Metadata includes: path, size, etag, mime_type, created_at, modified_at,
            version, zone_id, is_directory.

        Performance:
            - Single RPC call instead of N calls
            - 10x fewer round trips for multi-path operations
            - Leverages batch metadata fetch from database

        Examples:
            >>> results = nx.metadata_batch(["/file1.txt", "/missing.txt"])
            >>> print(results["/file1.txt"]["size"])
            1024
            >>> print(results["/missing.txt"])
            None
        """
        results: dict[str, dict[str, Any] | None] = {}

        # Validate paths and collect valid ones
        valid_paths: list[str] = []
        for path in paths:
            try:
                validated = self._validate_path(path)
                valid_paths.append(validated)
            except Exception as exc:
                logger.debug("Path validation failed in metadata_batch for %s: %s", path, exc)
                results[path] = None

        # Batch fetch metadata from database
        if valid_paths and hasattr(self.metadata, "get_batch"):
            batch_metadata = self.metadata.get_batch(valid_paths)
        else:
            # Fallback to individual fetches if get_batch not available
            batch_metadata = {p: self.metadata.get(p) for p in valid_paths}

        # Process results with permission checks
        for path in valid_paths:
            try:
                meta = batch_metadata.get(path)

                if meta is None:
                    results[path] = None
                    continue

                # Permission check via KernelDispatch INTERCEPT.
                from nexus.contracts.exceptions import PermissionDeniedError
                from nexus.contracts.vfs_hooks import StatHookContext as _SHC

                ctx = self._resolve_cred(context)
                try:
                    self._kernel.dispatch_pre_hooks(
                        "stat", _SHC(path=path, context=ctx, permission="READ")
                    )
                except PermissionDeniedError:
                    results[path] = None
                    continue

                # Check if it's a directory
                is_dir = self.is_directory(path, context=context)

                results[path] = {
                    "path": meta.path,
                    "backend_name": meta.backend_name,
                    "physical_path": meta.physical_path,
                    "size": meta.size,
                    "etag": meta.etag,
                    "mime_type": meta.mime_type,
                    "created_at": meta.created_at,
                    "modified_at": meta.modified_at,
                    "version": meta.version,
                    "zone_id": meta.zone_id,
                    "is_directory": is_dir,
                }
            except Exception as exc:
                logger.debug("Failed to build metadata result for %s: %s", path, exc)
                results[path] = None

        return results

    @rpc_expose(description="Delete multiple files/directories")
    def delete_batch(
        self,
        paths: list[str],
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, dict]:
        """
        Delete multiple files or directories in a single operation.

        Each path is processed independently - failures on one path don't affect others.
        Directories require recursive=True to delete non-empty directories.

        Args:
            paths: List of virtual paths to delete
            recursive: If True, delete non-empty directories (like rm -rf)
            context: Optional operation context for permission checks

        Returns:
            Dictionary mapping each path to its result:
                {"success": True} or {"success": False, "error": "error message"}

        Example:
            >>> results = nx.delete_batch(['/a.txt', '/b.txt', '/folder'])
            >>> for path, result in results.items():
            ...     if result['success']:
            ...         print(f"Deleted {path}")
            ...     else:
            ...         print(f"Failed {path}: {result['error']}")
        """
        # Validate all paths first
        validated: list[str] = []
        results: dict[str, dict] = {}
        for path in paths:
            try:
                validated.append(self._validate_path(path))
            except Exception as e:
                results[path] = {"success": False, "error": str(e)}

        if not validated:
            return results

        # Batch metadata lookup (single query instead of N)
        batch_meta = self.metadata.get_batch(validated)

        for path in validated:
            try:
                meta = batch_meta.get(path)

                # Check for implicit directory (exists because it has files beneath it)
                is_implicit_dir = meta is None and self.metadata.is_implicit_directory(path)

                if meta is None and not is_implicit_dir:
                    results[path] = {"success": False, "error": "File not found"}
                    continue

                # Check if this is a directory (explicit or implicit)
                is_dir = is_implicit_dir or (meta and meta.mime_type == "inode/directory")

                if is_dir:
                    self.sys_unlink(path, recursive=recursive, context=context)
                else:
                    self.sys_unlink(path, context=context)

                results[path] = {"success": True}
            except Exception as e:
                results[path] = {"success": False, "error": str(e)}

        return results

    def _rmdir_internal(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
        is_implicit: bool | None = None,  # noqa: ARG002
    ) -> None:
        """Internal rmdir — delegates to sys_unlink which routes through Rust sys_rmdir."""
        self.sys_unlink(path, recursive=recursive, context=context)

    @rpc_expose(description="Rename/move multiple files")
    def rename_batch(
        self,
        renames: list[tuple[str, str]],
        context: OperationContext | None = None,
    ) -> dict[str, dict]:
        """
        Rename/move multiple files in a single operation.

        Each rename is processed independently - failures on one don't affect others.
        This is a metadata-only operation (instant, regardless of file size).

        Args:
            renames: List of (old_path, new_path) tuples
            context: Optional operation context for permission checks

        Returns:
            Dictionary mapping each old_path to its result:
                {"success": True, "new_path": "..."} or {"success": False, "error": "..."}

        Example:
            >>> results = nx.rename_batch([
            ...     ('/old1.txt', '/new1.txt'),
            ...     ('/old2.txt', '/new2.txt'),
            ... ])
            >>> for old_path, result in results.items():
            ...     if result['success']:
            ...         print(f"Renamed {old_path} -> {result['new_path']}")
        """
        results = {}
        for old_path, new_path in renames:
            try:
                self.sys_rename(old_path, new_path, context=context)
                results[old_path] = {"success": True, "new_path": new_path}
            except Exception as e:
                results[old_path] = {"success": False, "error": str(e)}

        return results

    # ── Search/listing ────────────────────────────────────────────────

    def _matches_patterns(
        self,
        file_path: str,
        include_patterns: builtins.list[str] | None = None,
        exclude_patterns: builtins.list[str] | None = None,
    ) -> bool:
        """Check if file path matches include/exclude patterns."""
        import fnmatch as _fnmatch

        # Check include patterns
        if include_patterns and not any(_fnmatch.fnmatch(file_path, p) for p in include_patterns):
            return False

        # Check exclude patterns
        return not (
            exclude_patterns and any(_fnmatch.fnmatch(file_path, p) for p in exclude_patterns)
        )

    # --- Search (sys_readdir/glob/grep) ---

    def _entry_to_detail_dict(self, entry: FileMetadata, recursive: bool) -> dict[str, Any]:
        """Convert a FileMetadata entry to a detail dict for sys_readdir.

        Promotes entry_type=0 (DT_REG) to 1 (DT_DIR) for implicit directories
        in non-recursive listings, matching ls -l semantics.
        """
        return {
            "path": entry.path,
            "size": entry.size,
            "etag": entry.etag,
            "entry_type": 1
            if (
                not recursive
                and entry.entry_type == 0
                and self.metadata.is_implicit_directory(entry.path)
            )
            else entry.entry_type,
            "zone_id": entry.zone_id,
            "owner_id": entry.owner_id,
            "modified_at": entry.modified_at.isoformat() if entry.modified_at else None,
            "version": entry.version,
        }

    # Issue #3388: Internal metastore prefixes that must not appear in
    # user-facing directory listings (search checkpoints, ReBAC namespaces).
    # These are bare keys (no leading "/") — user paths always start with "/".
    _INTERNAL_PATH_PREFIXES = ("cfg:", "ns:")

    @staticmethod
    def _is_internal_path(path: str) -> bool:
        """Return True for system-internal metastore paths (bare keys)."""
        return path.startswith(MetadataMixin._INTERNAL_PATH_PREFIXES)

    @rpc_expose(description="List directory entries")
    def sys_readdir(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,  # noqa: ARG002
        *,
        context: OperationContext | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]] | Any:
        # ── /__sys__/locks/ virtual namespace (like /proc/locks) ──
        sys_locks_prefix = "/__sys__/locks"
        stripped = path.rstrip("/")
        if stripped == sys_locks_prefix or stripped.startswith(sys_locks_prefix + "/"):
            prefix = stripped[len(sys_locks_prefix) :]
            lock_limit = limit or 1024
            locks = self._kernel.metastore_list_locks(prefix, lock_limit)
            if details:
                return locks
            return [lk["path"] for lk in locks]

        # §12d Phase 2: Rust readdir merges backend list_dir for all
        # backends (CAS, path-local, external connectors) uniformly.
        # No Python-side external connector intercept needed.

        # Non-recursive, non-detailed, unbounded listings go through the
        # Rust kernel so they see per-mount metastore entries (F2 C5).
        # Python's ``self.metadata.list_iter`` only hits the default global
        # metastore, which is empty for federation zones.
        _kernel = getattr(self, "_kernel", None)
        if (
            _kernel is not None
            and not recursive
            and not details
            and limit is None
            and not self._is_internal_path(path)
        ):
            _is_admin = (
                getattr(context, "is_admin", False)
                if context is not None and not isinstance(context, dict)
                else (context.get("is_admin", False) if isinstance(context, dict) else False)
            )
            try:
                _kernel_entries = _kernel.readdir(path, self._zone_id, _is_admin)
            except (OSError, ValueError) as exc:
                logger.debug("kernel.readdir failed for %s: %s", path, exc)
                _kernel_entries = None
            if _kernel_entries:
                return [
                    child for child, _etype in _kernel_entries if not self._is_internal_path(child)
                ]

        prefix = path if path != "/" else ""
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"

        # Issue #3779 follow-up: filter list results by the caller's zone_id.
        # The metastore is a single store shared across zones (each row carries
        # a zone_id column). Without this filter, V2 API callers see every
        # zone's files. Admins and root-zone callers keep the global view.
        # Handle OperationContext, dict, and None uniformly — missing zone
        # falls open to ROOT (admin-equivalent view) by design: a caller
        # without a zone claim is either the kernel or an unauthenticated
        # path, neither of which should be zone-restricted here.
        if isinstance(context, dict):
            caller_zone = context.get("zone_id") or ROOT_ZONE_ID
            caller_is_admin = bool(context.get("is_admin", False))
        elif context is not None:
            caller_zone = getattr(context, "zone_id", None) or ROOT_ZONE_ID
            caller_is_admin = bool(getattr(context, "is_admin", False))
        else:
            caller_zone = ROOT_ZONE_ID
            caller_is_admin = False

        def _zone_allowed(entry: Any) -> bool:
            if caller_is_admin or caller_zone == ROOT_ZONE_ID:
                return True
            entry_zone = getattr(entry, "zone_id", None) or ROOT_ZONE_ID
            # Root zone is the global namespace, not any user's private zone:
            # standalone NexusFS tags every file as zone_id=root, and
            # federation-root-mounted files are visible from every zone by
            # design. Filtering them out would break sys_readdir in
            # standalone mode entirely (surface-level cost: the
            # test_embedded_namespaces_rebac tests that write under
            # /workspace/acme/ and fail to readdir them). Per-zone isolation
            # continues to work because non-root entry_zone still has to
            # match caller_zone below.
            if entry_zone == ROOT_ZONE_ID:
                return True
            return entry_zone == caller_zone

        if limit is not None:
            from nexus.core.pagination import paginate_iter

            items_iter = (
                e
                for e in self.metadata.list_iter(prefix=prefix, recursive=recursive)
                if not self._is_internal_path(e.path) and _zone_allowed(e)
            )
            result = paginate_iter(items_iter, limit=limit, cursor_path=cursor)
            if details:
                result.items = [self._entry_to_detail_dict(e, recursive) for e in result.items]
            else:
                result.items = [e.path for e in result.items]
            return result

        # Issue #3706: Use list_iter() instead of list() to avoid creating a
        # second filtered copy in Python and to bypass RustMetastoreProxy's
        # _dcache (prevents unbounded cache growth).  Note: the underlying
        # Rust/Raft engines still materialise the full result set internally;
        # true streaming requires a Rust-level paginated API (future work).
        entries_iter = (
            e
            for e in self.metadata.list_iter(prefix=prefix, recursive=recursive)
            if not self._is_internal_path(e.path) and _zone_allowed(e)
        )
        if details:
            return [self._entry_to_detail_dict(e, recursive) for e in entries_iter]
        return [e.path for e in entries_iter]

    @rpc_expose(description="Backfill sparse directory index for fast listings", admin_only=True)
    def backfill_directory_index(
        self,
        prefix: str = "/",
        zone_id: str | None = None,
        _context: Any = None,  # noqa: ARG002 - RPC interface requires context param
    ) -> dict[str, Any]:
        """Backfill sparse directory index from existing files.

        Use this to populate the index for directories that existed before
        the sparse index feature was added. This improves list() performance
        from O(n) LIKE queries to O(1) index lookups.

        Args:
            prefix: Path prefix to backfill (default: "/" for all)
            zone_id: Zone ID to backfill (None for all zones)
            _context: Operation context (admin required, enforced by @rpc_expose)

        Returns:
            Dict with entries_created count
        """
        created = self.metadata.backfill_directory_index(prefix=prefix, zone_id=zone_id)
        return {"entries_created": created, "prefix": prefix}

    @rpc_expose(description="Flush pending write observer events to DB", admin_only=True)
    def flush_write_observer(
        self,
        _context: Any = None,  # noqa: ARG002 - RPC interface requires context param
    ) -> dict[str, Any]:
        """Flush the write observer so pending version/audit records are committed.

        The RecordStoreWriteObserver accumulates events dispatched by the
        Rust kernel and flushes them to RecordStore in debounced batches.
        This method forces an immediate flush, guaranteeing that subsequent
        queries (e.g. list_versions) see the data.

        Returns:
            Dict with ``flushed`` count.
        """
        # Issue #1801: use service registry to find write_observer — no closure needed.
        _wo = self.service("write_observer")
        if _wo is None or not hasattr(_wo, "flush"):
            return {"flushed": 0}
        from nexus.lib.sync_bridge import run_sync

        flushed: int = run_sync(_wo.flush())
        return {"flushed": flushed}
