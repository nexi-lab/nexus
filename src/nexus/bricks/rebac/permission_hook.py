"""PermissionCheckHook — VFS pre-intercept hook for permission enforcement.

Registered by factory on all KernelDispatch hook lists (read, write,
delete, rename, mkdir, rmdir).  Implements ``on_pre_*`` methods that
run before each VFS operation; raises ``PermissionError`` to abort.

The kernel never imports this module — factory creates and registers it.
Kernel just calls generic ``dispatch.intercept_pre_*()`` which iterates
the existing hook lists and calls ``on_pre_*`` via getattr.

Issue #899: Extracted from NexusFS kernel (was ``self._permission_checker``).
Issue #3394: Permission write leases — check once, write many.
Issue #3398: Extended leases to read, delete, rmdir hooks.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import SYSTEM_PATH_PREFIX
from nexus.contracts.types import Permission
from nexus.core.path_utils import parent_path

if TYPE_CHECKING:
    from nexus.bricks.rebac.cache.permission_lease import PermissionLeaseTable
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.contracts.vfs_hooks import (
        AccessHookContext,
        CopyHookContext,
        DeleteHookContext,
        MkdirHookContext,
        ReadHookContext,
        RenameHookContext,
        RmdirHookContext,
        StatHookContext,
        WriteHookContext,
    )

logger = logging.getLogger(__name__)


class PermissionCheckHook:
    """VFS pre-intercept permission gate (Issue #899).

    Wraps ``PermissionChecker`` and provides ``on_pre_*`` / ``on_post_*``
    methods so it can sit in the standard ``_read_hooks`` / ``_write_hooks``
    lists alongside other hooks.

    Pre methods check permission and raise ``PermissionError`` to abort.
    Post methods are no-ops (required for protocol compatibility with
    ``dispatch_post_hooks`` which calls ``on_post_*`` directly).
    """

    name = "permission_check"

    # ── Hook spec (duck-typed) (Issue #1610) ──────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(
            read_hooks=(self,),
            write_hooks=(self,),
            delete_hooks=(self,),
            rename_hooks=(self,),
            copy_hooks=(self,),
            mkdir_hooks=(self,),
            rmdir_hooks=(self,),
            stat_hooks=(self,),
            access_hooks=(self,),
        )

    def __init__(
        self,
        *,
        checker: Any,
        metadata_store: Any,
        default_context: Any,
        enforce_permissions: bool = True,
        permission_enforcer: Any = None,
        descendant_checker: Any = None,
        lease_table: "PermissionLeaseTable | None" = None,
    ) -> None:
        self._checker = checker
        self._metadata_store = metadata_store
        self._default_context = default_context
        self._enforce_permissions = enforce_permissions
        self._permission_enforcer = permission_enforcer
        self._descendant_checker = descendant_checker
        self._lease_table = lease_table

    # ------------------------------------------------------------------
    # Lease helpers — shared by on_pre_write, on_pre_read, on_pre_delete,
    # on_pre_rmdir (Issue #3398 decisions 5A, 16A).
    # ------------------------------------------------------------------

    def _lease_check(self, path: str, agent_id: str | None) -> bool:
        """Return True if a valid lease exists for (path, agent_id)."""
        return bool(
            agent_id and self._lease_table is not None and self._lease_table.check(path, agent_id)
        )

    def _lease_stamp(self, path: str, agent_id: str | None) -> None:
        """Stamp a lease after a successful permission check."""
        if agent_id and self._lease_table is not None:
            self._lease_table.stamp(path, agent_id)

    @staticmethod
    def _extract_agent_id(context: Any) -> str | None:
        """Extract agent_id from context, or None if unavailable."""
        return getattr(context, "agent_id", None) if context else None

    @staticmethod
    def _is_system_path(path: str) -> bool:
        """``True`` for paths under the kernel system namespace ``/__sys__/``.

        Kernel-internal infrastructure (mount table, ReBAC namespace store,
        ReBAC version store, …) lives under ``SYSTEM_PATH_PREFIX`` and must
        bypass user-facing permission checks. Without this short-circuit,
        e.g. a ``check`` call would reload the namespace via ``sys_read``,
        which would re-enter the same hook, which would re-check, …
        triggering unbounded recursion (PR #3890 CI hang investigation).
        """
        return path.startswith(SYSTEM_PATH_PREFIX)

    # ------------------------------------------------------------------
    # PRE hooks — permission gating (raise PermissionError to abort)
    # ------------------------------------------------------------------

    def on_pre_read(self, ctx: ReadHookContext) -> None:
        """Check READ permission before read/stream/stat operations.

        Fast path (Issue #3398): if a permission lease exists for the
        (path, agent_id) pair, skip the full ReBAC check.
        """
        if not self._enforce_permissions:
            return
        if self._is_system_path(ctx.path):
            return
        # stat() for implicit directories uses TRAVERSE — signalled via extra
        if ctx.extra.get("is_implicit_directory"):
            self._check_traverse(ctx)
            return

        context = ctx.context or self._default_context
        agent_id = self._extract_agent_id(context)

        # Fast path: check permission lease
        if self._lease_check(ctx.path, agent_id):
            return

        # Slow path: full ReBAC check
        self._checker.check(ctx.path, Permission.READ, context)

        # Stamp lease on successful check
        self._lease_stamp(ctx.path, agent_id)

    def on_pre_write(self, ctx: WriteHookContext) -> None:
        """Check WRITE permission before write operations.

        Fast path (Issue #3394): if a permission lease exists for the
        (checked_path, agent_id) pair, skip the full ReBAC check (~1μs
        vs ~50-200μs).  On cache miss, do the full check and stamp a
        lease for subsequent writes.
        """
        if not self._enforce_permissions:
            return
        if self._is_system_path(ctx.path):
            return
        context = ctx.context or self._default_context

        # Resolve the path that will actually be permission-checked.
        # For existing files: the file path.  For new files: the parent dir.
        # Keying leases by checked_path covers both "repeated writes to same
        # file" and "many new files in same directory" (Decision #14B).
        if ctx.old_metadata is not None:
            checked_path = ctx.path
        else:
            checked_parent = parent_path(ctx.path)
            if checked_parent is None:
                return  # root path — no parent to check
            checked_path = checked_parent

        agent_id = self._extract_agent_id(context)

        # Fast path: check permission lease (~100-200ns)
        if self._lease_check(checked_path, agent_id):
            return  # lease valid — skip full ReBAC check

        # Slow path: full ReBAC check (raises PermissionError on denial)
        if ctx.old_metadata is not None:
            self._checker.check(ctx.path, Permission.WRITE, context, file_metadata=ctx.old_metadata)
        else:
            self._checker.check(checked_path, Permission.WRITE, context)

        # Stamp lease on successful check (Decision #6A)
        self._lease_stamp(checked_path, agent_id)

    def on_pre_delete(self, ctx: DeleteHookContext) -> None:
        """Check WRITE permission before delete.

        Fast path (Issue #3398 decision 5A): lease check before full
        ReBAC check, same pattern as on_pre_write.
        """
        if not self._enforce_permissions:
            return
        if self._is_system_path(ctx.path):
            return
        context = ctx.context or self._default_context
        agent_id = self._extract_agent_id(context)

        if self._lease_check(ctx.path, agent_id):
            return

        self._checker.check(ctx.path, Permission.WRITE, context)
        self._lease_stamp(ctx.path, agent_id)

    def on_pre_rename(self, ctx: RenameHookContext) -> None:
        """Check WRITE permission on both source and destination."""
        if self._is_system_path(ctx.old_path) and self._is_system_path(ctx.new_path):
            return
        self._checker.check(ctx.old_path, Permission.WRITE, ctx.context)

    def on_pre_copy(self, ctx: "CopyHookContext") -> None:
        """Check READ on source, WRITE on destination (Issue #3329)."""
        if not (self._is_system_path(ctx.src_path) and self._is_system_path(ctx.dst_path)):
            self._checker.check(ctx.src_path, Permission.READ, ctx.context)
            self._checker.check(ctx.dst_path, Permission.WRITE, ctx.context)

    def on_pre_mkdir(self, ctx: MkdirHookContext) -> None:
        """Check WRITE permission on nearest existing ancestor."""
        if not self._enforce_permissions:
            return
        if self._is_system_path(ctx.path):
            return
        # Find nearest existing ancestor (kernel populates extra if needed)
        check_path = ctx.extra.get("check_path")
        if check_path is None:
            # Fallback: resolve ancestor ourselves
            check_path = ctx.path
            while check_path and check_path != "/" and not self._metadata_store.exists(check_path):
                check_path = parent_path(check_path)
        if check_path and self._metadata_store.exists(check_path):
            context = ctx.context or self._default_context
            self._checker.check(check_path, Permission.WRITE, context)

    def on_pre_rmdir(self, ctx: RmdirHookContext) -> None:
        """Check WRITE permission before rmdir.

        Fast path (Issue #3398 decision 5A): lease check before full
        ReBAC check, same pattern as on_pre_write.
        """
        if not self._enforce_permissions:
            return
        if self._is_system_path(ctx.path):
            return
        context = ctx.context or self._default_context
        agent_id = self._extract_agent_id(context)

        if self._lease_check(ctx.path, agent_id):
            return

        self._checker.check(ctx.path, Permission.WRITE, context)
        self._lease_stamp(ctx.path, agent_id)

    def on_pre_stat(self, ctx: "StatHookContext") -> None:
        """Permission check for stat/is_directory (Issue #1815).

        Uses TRAVERSE or READ based on ctx.permission.
        For implicit directories, falls back to descendant access check.
        Raises ``PermissionDeniedError`` to deny.
        """
        if not self._enforce_permissions:
            return
        if self._is_system_path(ctx.path):
            return
        context = ctx.context or self._default_context
        perm = Permission.TRAVERSE if ctx.permission == "TRAVERSE" else Permission.READ
        is_implicit = ctx.extra.get("is_implicit_directory", False)

        if is_implicit:
            # Try TRAVERSE first, fall back to descendant access check
            if self._permission_enforcer is not None:
                has_perm = self._permission_enforcer.check(ctx.path, Permission.TRAVERSE, context)
                if not has_perm and self._descendant_checker is not None:
                    has_perm = self._descendant_checker.has_access(
                        ctx.path, Permission.READ, context
                    )
                if not has_perm:
                    from nexus.contracts.exceptions import PermissionDeniedError

                    raise PermissionDeniedError(
                        f"Access denied: User '{getattr(context, 'user_id', '?')}' does not have "
                        f"TRAVERSE permission for '{ctx.path}'",
                        path=ctx.path,
                    )
        else:
            # Non-implicit: check the requested permission directly
            if self._permission_enforcer is not None and not self._permission_enforcer.check(
                ctx.path, perm, context
            ):
                from nexus.contracts.exceptions import PermissionDeniedError

                raise PermissionDeniedError(
                    f"Access denied: no {ctx.permission} permission for '{ctx.path}'",
                    path=ctx.path,
                )

    def on_pre_access(self, ctx: "AccessHookContext") -> None:
        """Permission check for access (Issue #1815).

        For implicit directories: TRAVERSE first, descendant fallback.
        For files: direct permission check.
        Raises ``PermissionDeniedError`` to deny.
        """
        if not self._enforce_permissions:
            return
        if self._is_system_path(ctx.path):
            return
        context = ctx.context or self._default_context
        perm = Permission.TRAVERSE if ctx.permission == "TRAVERSE" else Permission.READ
        is_implicit = ctx.extra.get("is_implicit_directory", False)

        if is_implicit:
            # Try TRAVERSE first
            if self._permission_enforcer is not None:
                if self._permission_enforcer.check(ctx.path, Permission.TRAVERSE, context):
                    return  # allowed
                # Fall back to descendant access check
                if self._descendant_checker is not None and self._descendant_checker.has_access(
                    ctx.path, Permission.READ, context
                ):
                    return  # allowed
                from nexus.contracts.exceptions import PermissionDeniedError

                raise PermissionDeniedError(
                    f"Access denied for '{ctx.path}'",
                    path=ctx.path,
                )
        else:
            # Direct permission check for real files
            if self._permission_enforcer is not None and not self._permission_enforcer.check(
                ctx.path, perm, context
            ):
                from nexus.contracts.exceptions import PermissionDeniedError

                raise PermissionDeniedError(
                    f"Access denied: no {ctx.permission} permission for '{ctx.path}'",
                    path=ctx.path,
                )

    def filter_stat_bulk(self, paths: list[str], context: Any) -> list[str]:
        """Batch permission filter for stat_bulk/read_bulk (Issue #1815).

        Returns the subset of *paths* where READ permission is granted.
        """
        if not self._enforce_permissions or self._permission_enforcer is None:
            return paths
        result: list[str] = self._permission_enforcer.filter_list(paths, context)
        return result

    # ------------------------------------------------------------------
    # POST hooks — no-op (protocol compatibility)
    # ------------------------------------------------------------------

    def on_post_read(self, ctx: ReadHookContext) -> None:
        pass

    def on_post_write(self, ctx: WriteHookContext) -> None:
        pass

    def on_post_delete(self, ctx: DeleteHookContext) -> None:
        pass

    def on_post_rename(self, ctx: RenameHookContext) -> None:
        pass

    def on_post_mkdir(self, ctx: MkdirHookContext) -> None:
        pass

    def on_post_rmdir(self, ctx: RmdirHookContext) -> None:
        pass

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_traverse(self, ctx: ReadHookContext) -> None:
        """TRAVERSE permission check for implicit directories (stat)."""
        context = ctx.context or self._default_context
        if self._permission_enforcer is None:
            return
        has_permission = self._permission_enforcer.check(ctx.path, Permission.TRAVERSE, context)
        if not has_permission and self._descendant_checker is not None:
            has_permission = self._descendant_checker.has_access(ctx.path, Permission.READ, context)
        if not has_permission:
            raise PermissionError(
                f"Access denied: User '{getattr(context, 'user_id', '?')}' does not have "
                f"TRAVERSE permission for '{ctx.path}'"
            )
