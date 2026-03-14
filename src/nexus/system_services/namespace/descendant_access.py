"""Descendant-access checking extracted from NexusFS.

Provides hierarchical directory navigation: users can see parent directories
if they have access to any child/descendant (even if deeply nested).

Extracted from ``nexus.core.nexus_fs._has_descendant_access`` and
``_has_descendant_access_bulk`` as part of the NexusFS slim-down (#2033).
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext, Permission

logger = logging.getLogger(__name__)


class DescendantAccessChecker:
    """Stateless service that checks descendant-level access for directory listing.

    All collaborators are injected via the constructor — there is **no** dependency
    on ``NexusFS`` itself.
    """

    def __init__(
        self,
        *,
        rebac_manager: Any,
        rebac_service: Any,
        dir_visibility_cache: Any | None,
        permission_enforcer: Any,
        metadata_store: Any,
    ) -> None:
        self._rebac_manager = rebac_manager
        self._rebac_service = rebac_service
        self._dir_visibility_cache = dir_visibility_cache
        self._permission_enforcer = permission_enforcer
        self._metadata_store = metadata_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def has_access(
        self,
        path: str,
        permission: "Permission",
        context: "OperationContext",
    ) -> bool:
        """Check if user has access to *path* OR any of its descendants.

        This enables hierarchical directory navigation: users can see parent
        directories if they have access to any child/descendant (even if deeply
        nested).

        Workflow (Issue #919 optimization):
        1. Check DirectoryVisibilityCache first (O(1) cache hit)
        2. Check Tiger Cache direct access (O(1) bitmap lookup)
        3. If cache miss, compute from Tiger bitmap (O(bitmap) — no descendant
           enumeration!)
        4. Only fall back to slow O(n) path if Tiger Cache unavailable

        Args:
            path: Path to check (e.g., "/workspace")
            permission: Permission to check (e.g., Permission.READ)
            context: User context with subject info

        Returns:
            True if user has access to path OR any descendant, False otherwise

        Performance Notes:
            - Issue #919: Uses DirectoryVisibilityCache for O(1) lookups
            - Uses Tiger bitmap scan instead of N descendant queries
            - /workspace with 10K files: ~2000ms -> ~5ms
            - Skips descendant check if no ReBAC manager available

        Examples:
            >>> # Joe has access to /workspace/joe/file.txt
            >>> checker.has_access("/workspace", READ, joe_ctx)
            True  # Can access /workspace because has access to descendant

            >>> checker.has_access("/other", READ, joe_ctx)
            False  # No access to /other or any descendants
        """
        from nexus.contracts.types import OperationContext, Permission

        # Admin/system bypass
        if context.is_admin or context.is_system:
            return True

        # Check if ReBAC is available
        has_rebac = self._rebac_manager is not None

        if not has_rebac:
            # Fallback to permission enforcer if no ReBAC
            assert isinstance(context, OperationContext), "Context must be OperationContext"
            return bool(self._permission_enforcer.check(path, permission, context))

        # Validate subject_id (required for ReBAC checks)
        if context.subject_id is None:
            return False

        # Type narrowing - create local variables with explicit types
        subject_id: str = context.subject_id  # Now guaranteed non-None after check
        subject_tuple: tuple[str, str] = (context.subject_type, subject_id)

        # Map permission to ReBAC permission name
        permission_map = {
            Permission.READ: "read",
            Permission.WRITE: "write",
            Permission.EXECUTE: "execute",
            Permission.TRAVERSE: "traverse",
        }
        rebac_permission = permission_map.get(permission, "read")
        zone_id = context.zone_id or ROOT_ZONE_ID

        # =============================================================
        # Issue #919 OPTIMIZATION 1: Check DirectoryVisibilityCache (O(1))
        # =============================================================
        if self._dir_visibility_cache is not None:
            cached_visible = self._dir_visibility_cache.is_visible(
                zone_id=zone_id,
                subject_type=context.subject_type,
                subject_id=subject_id,
                dir_path=path,
            )
            if cached_visible is not None:
                logger.debug(f"has_access: DirVisCache HIT for {path} = {cached_visible}")
                return bool(cached_visible)

        # =============================================================
        # OPTIMIZATION 2: Try Tiger Cache direct access (O(1) lookup)
        # =============================================================
        if hasattr(self._rebac_manager, "tiger_check_access"):
            tiger_result = self._rebac_manager.tiger_check_access(
                subject=subject_tuple,
                permission=rebac_permission,
                object=("file", path),
            )
            if tiger_result is True:
                # Cache this positive result
                if self._dir_visibility_cache is not None:
                    self._dir_visibility_cache.set_visible(
                        zone_id,
                        context.subject_type,
                        subject_id,
                        path,
                        True,
                        "direct_tiger_access",
                    )
                return True
            # If tiger_result is None, cache miss - continue with normal check
            # If tiger_result is False, explicitly denied - but still check descendants

        # =============================================================
        # OPTIMIZATION 3: Check direct access via rebac_check (fast path)
        # =============================================================
        direct_access = self._rebac_service.rebac_check_sync(
            subject=subject_tuple,
            permission=rebac_permission,
            object=("file", path),
            zone_id=zone_id,
        )
        if direct_access:
            # Cache this positive result
            if self._dir_visibility_cache is not None:
                self._dir_visibility_cache.set_visible(
                    zone_id, context.subject_type, subject_id, path, True, "direct_rebac_access"
                )
            return True

        # =============================================================
        # Issue #919 OPTIMIZATION 4: Compute from Tiger bitmap (O(bitmap))
        # This is the KEY optimization - no descendant enumeration!
        # Instead of querying N descendants from metadata, scan the Tiger
        # bitmap of accessible resources for prefix matches.
        # =============================================================
        if self._dir_visibility_cache is not None:
            bitmap_result = self._dir_visibility_cache.compute_from_tiger_bitmap(
                zone_id=zone_id,
                subject_type=context.subject_type,
                subject_id=subject_id,
                dir_path=path,
                permission=rebac_permission,
            )
            if bitmap_result is not None:
                logger.debug(f"has_access: Tiger bitmap compute for {path} = {bitmap_result}")
                return bool(bitmap_result)

        # =============================================================
        # SLOW PATH FALLBACK: Only reached if Tiger Cache unavailable
        # Query all descendants from metadata and check permissions
        # =============================================================
        logger.debug(f"has_access: Falling back to slow path for {path}")

        # Get all files/directories under this path (recursive)
        prefix = path if path.endswith("/") else path + "/"
        if path == "/":
            prefix = ""

        try:
            all_descendants = self._metadata_store.list(prefix)
        except Exception as exc:
            # If metadata query fails, return False
            logger.debug("Metadata query failed for prefix %s: %s", prefix, exc)
            return False

        # OPTIMIZATION 5 (legacy): Use Tiger Cache for batch descendant check
        if hasattr(self._rebac_manager, "tiger_get_accessible_resources"):
            try:
                # Get all accessible resources for this subject
                accessible_ids = self._rebac_manager.tiger_get_accessible_resources(
                    subject=subject_tuple,
                    permission=rebac_permission,
                    resource_type="file",
                    zone_id=zone_id,
                )
                if accessible_ids:
                    # Check if any descendant is in the accessible set
                    # Note: This requires Tiger resource map integration
                    logger.debug(
                        f"has_access: Tiger Cache has {len(accessible_ids)} accessible resources"
                    )
            except Exception as e:
                logger.debug(f"has_access: Tiger Cache lookup failed: {e}")

        # 4. OPTIMIZATION (issue #380): Use bulk permission checking for descendants
        # Instead of checking each descendant individually (N queries), use rebac_check_bulk()
        if self._rebac_manager is not None and hasattr(self._rebac_manager, "rebac_check_bulk"):
            logger.debug(
                f"has_access: Using bulk check for {len(all_descendants)} descendants of {path}"
            )

            # Build list of checks for all descendants
            checks = [
                (subject_tuple, rebac_permission, ("file", meta.path)) for meta in all_descendants
            ]

            try:
                # Perform bulk permission check
                results = self._rebac_manager.rebac_check_bulk(checks, zone_id=zone_id)

                # OPTIMIZATION 5: Early exit on first accessible descendant
                for check in checks:
                    if results.get(check, False):
                        logger.debug(f"has_access: Found accessible descendant {check[2][1]}")
                        # Cache positive result from slow path
                        if self._dir_visibility_cache is not None:
                            self._dir_visibility_cache.set_visible(
                                zone_id,
                                context.subject_type,
                                subject_id,
                                path,
                                True,
                                f"slow_path:{check[2][1]}",
                            )
                        return True

                logger.debug("has_access: No accessible descendants found")
                # Cache negative result from slow path
                if self._dir_visibility_cache is not None:
                    self._dir_visibility_cache.set_visible(
                        zone_id,
                        context.subject_type,
                        subject_id,
                        path,
                        False,
                        "slow_path:no_descendants",
                    )
                return False

            except Exception as e:
                logger.warning(
                    f"has_access: Bulk check failed, falling back to individual checks: {e}"
                )
                # Fall through to original implementation

        # Fallback: Check ReBAC permissions on descendants.
        # Use rebac_service batch if available, otherwise individual checks with early exit.
        if hasattr(self._rebac_service, "rebac_check_bulk_sync"):
            try:
                checks = [
                    (subject_tuple, rebac_permission, ("file", meta.path))
                    for meta in all_descendants
                ]
                results = self._rebac_service.rebac_check_bulk_sync(
                    checks, zone_id=context.zone_id
                )
                for check in checks:
                    if results.get(check, False):
                        if self._dir_visibility_cache is not None:
                            self._dir_visibility_cache.set_visible(
                                zone_id,
                                context.subject_type,
                                subject_id,
                                path,
                                True,
                                f"fallback_bulk:{check[2][1]}",
                            )
                        return True
            except Exception:
                logger.debug("has_access: rebac_check_bulk_sync failed, using individual checks")

        # Final fallback: individual checks with early exit
        for meta in all_descendants:
            descendant_access = self._rebac_service.rebac_check_sync(
                subject=subject_tuple,
                permission=rebac_permission,
                object=("file", meta.path),
                zone_id=zone_id,
            )
            if descendant_access:
                if self._dir_visibility_cache is not None:
                    self._dir_visibility_cache.set_visible(
                        zone_id,
                        context.subject_type,
                        subject_id,
                        path,
                        True,
                        f"fallback:{meta.path}",
                    )
                return True

        # No accessible descendants found - cache negative result
        if self._dir_visibility_cache is not None:
            self._dir_visibility_cache.set_visible(
                zone_id, context.subject_type, subject_id, path, False, "fallback:no_descendants"
            )
        return False

    def has_access_bulk(
        self,
        paths: list[str],
        permission: "Permission",
        context: "OperationContext",
    ) -> dict[str, bool]:
        """Check if user has access to any descendant for multiple paths in bulk.

        This is an optimization for list() operations that need to check many
        backend directories. Instead of calling ``has_access()`` for each
        directory (N separate bulk queries), this method batches all directories
        + all their descendants into ONE bulk query.

        Args:
            paths: List of directory paths to check
            permission: Permission to check (READ, WRITE, or EXECUTE)
            context: Operation context with user/agent identity

        Returns:
            Dict mapping each path to True (has access) or False (no access)

        Performance:
            - Before: N directories x 1 bulk query = N bulk queries
            - After: 1 bulk query for all directories + all descendants
            - 10x improvement for 10 backend directories
        """
        from nexus.contracts.types import Permission

        # Admin/system bypass
        if context.is_admin or context.is_system:
            return dict.fromkeys(paths, True)

        # Check if ReBAC bulk checking is available
        if not (
            self._rebac_manager is not None and hasattr(self._rebac_manager, "rebac_check_bulk")
        ):
            # Fallback to individual checks
            return {path: self.has_access(path, permission, context) for path in paths}

        # Validate subject_id
        if context.subject_id is None:
            return dict.fromkeys(paths, False)

        subject_tuple: tuple[str, str] = (context.subject_type, context.subject_id)

        # Map permission to ReBAC name
        permission_map = {
            Permission.READ: "read",
            Permission.WRITE: "write",
            Permission.EXECUTE: "execute",
        }
        rebac_permission = permission_map.get(permission, "read")

        # PHASE 1: Collect all descendants for all paths
        # OPTIMIZATION: Find common ancestor and query ONCE instead of N queries
        all_checks: list[tuple[tuple[str, str], str, tuple[str, str]]] = []
        path_to_descendants: dict[str, list[str]] = {}

        # Find common ancestor of all paths to minimize DB queries
        if len(paths) > 1:
            # Find the longest common prefix among all paths
            common_prefix = paths[0]
            for path in paths[1:]:
                # Find common prefix between current common_prefix and this path
                min_len = min(len(common_prefix), len(path))
                i = 0
                while i < min_len and common_prefix[i] == path[i]:
                    i += 1
                common_prefix = common_prefix[:i]

            # Trim to last / to get valid directory path
            if "/" in common_prefix:
                common_prefix = common_prefix[: common_prefix.rfind("/") + 1]
            else:
                common_prefix = "/"

            # Query common ancestor ONCE and cache all descendants
            logger.debug(
                f"has_access_bulk: Using common ancestor optimization - "
                f"querying '{common_prefix}' once for {len(paths)} directories"
            )
            try:
                all_descendants = self._metadata_store.list(common_prefix if common_prefix else "/")
                all_paths_set = {meta.path for meta in all_descendants}
                logger.debug(
                    f"has_access_bulk: Got {len(all_paths_set)} paths from common ancestor"
                )
            except Exception as e:
                logger.warning(
                    f"has_access_bulk: Failed to list common ancestor {common_prefix}: {e}"
                )
                all_paths_set = set()

            # Filter locally for each directory
            for path in paths:
                # Check direct access to the directory itself
                all_checks.append((subject_tuple, rebac_permission, ("file", path)))

                # Filter descendants from cached list
                prefix = path if path.endswith("/") else path + "/"
                if path == "/":
                    descendant_paths = list(all_paths_set)
                else:
                    descendant_paths = [p for p in all_paths_set if p.startswith(prefix)]

                path_to_descendants[path] = descendant_paths

                # Add checks for all descendants
                for desc_path in descendant_paths:
                    all_checks.append((subject_tuple, rebac_permission, ("file", desc_path)))
        else:
            # Single path - just query directly
            for path in paths:
                # Check direct access to the directory itself
                all_checks.append((subject_tuple, rebac_permission, ("file", path)))

                # Get all descendants
                prefix = path if path.endswith("/") else path + "/"
                if path == "/":
                    prefix = ""

                try:
                    descendants = self._metadata_store.list(prefix)
                    descendant_paths = [meta.path for meta in descendants]
                    path_to_descendants[path] = descendant_paths

                    # Add checks for all descendants
                    for desc_path in descendant_paths:
                        all_checks.append((subject_tuple, rebac_permission, ("file", desc_path)))
                except Exception as e:
                    logger.warning(f"has_access_bulk: Failed to list {path}: {e}")
                    path_to_descendants[path] = []

        logger.debug(
            f"has_access_bulk: Checking {len(all_checks)} paths for {len(paths)} directories"
        )

        # PHASE 2: Perform ONE bulk permission check for everything
        try:
            results = self._rebac_manager.rebac_check_bulk(
                all_checks, zone_id=context.zone_id or ROOT_ZONE_ID
            )
        except Exception as e:
            logger.warning(f"has_access_bulk: Bulk check failed, falling back: {e}")
            # Fallback to individual checks
            return {path: self.has_access(path, permission, context) for path in paths}

        # PHASE 3: Map results back to each directory
        result_map = {}
        for path in paths:
            # Check if user has access to directory itself
            direct_check = (subject_tuple, rebac_permission, ("file", path))
            if results.get(direct_check, False):
                result_map[path] = True
                continue

            # Check if user has access to any descendant
            has_access = False
            for desc_path in path_to_descendants.get(path, []):
                desc_check = (subject_tuple, rebac_permission, ("file", desc_path))
                if results.get(desc_check, False):
                    has_access = True
                    break

            result_map[path] = has_access

        logger.debug(
            f"has_access_bulk: {sum(result_map.values())}/{len(paths)} directories accessible"
        )
        return result_map
