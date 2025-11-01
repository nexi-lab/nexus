"""
Directory Hierarchy Manager (P0-3 Implementation)

Automatically creates parent relationship tuples for directory inheritance.
This enables filesystem-like permission inheritance where granting access
to a directory automatically grants access to all children.

Usage:
    from nexus.core.hierarchy_manager import HierarchyManager

    manager = HierarchyManager(rebac_manager, enable_inheritance=True)

    # Automatically creates parent tuples when writing files
    manager.ensure_parent_tuples("/workspace/sales/report.pdf", tenant_id="org_123")

    # Now granting access to directory works:
    # rebac.write(("user", "alice"), "owner", ("file", "/workspace/sales"))
    # alice can now access /workspace/sales/report.pdf via parent_owner relation
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager


class HierarchyManager:
    """Manages directory hierarchy relationships for permission inheritance (P0-3).

    Automatically creates parent relationship tuples when files/directories
    are created, enabling directory-level permission grants to propagate
    to all children.

    Behavior:
    - Creates ("file", child_path) --parent--> ("file", parent_path) tuples
    - Integrates with namespace's parent_owner and parent_viewer relations
    - Batches operations for efficiency
    - Idempotent: safe to call multiple times
    """

    def __init__(
        self,
        rebac_manager: EnhancedReBACManager,
        enable_inheritance: bool = True,
    ):
        """Initialize hierarchy manager.

        Args:
            rebac_manager: ReBAC manager for creating tuples
            enable_inheritance: Enable automatic parent tuple creation
        """
        self.rebac_manager = rebac_manager
        self.enable_inheritance = enable_inheritance

    def ensure_parent_tuples(
        self,
        path: str,
        tenant_id: str | None = None,
    ) -> int:
        """Ensure parent relationship tuples exist for path (P0-3).

        Creates parent tuples for the entire path hierarchy:
        /a/b/c.txt creates:
        - (file:/a/b/c.txt) --parent--> (file:/a/b)
        - (file:/a/b) --parent--> (file:/a)

        Args:
            path: File or directory path
            tenant_id: Tenant ID for tuple scoping

        Returns:
            Number of parent tuples created
        """
        if not self.enable_inheritance:
            return 0

        if not path or path == "/":
            return 0

        parts = path.strip("/").split("/")
        if len(parts) < 2:
            # Root-level file, no parent
            return 0

        created_count = 0

        # Create parent chain from leaf to root
        # Example: /a/b/c.txt
        # - /a/b/c.txt -> /a/b
        # - /a/b -> /a
        for i in range(len(parts), 1, -1):
            child_path = "/" + "/".join(parts[:i])
            parent_path = "/" + "/".join(parts[: i - 1])

            # Check if parent tuple already exists
            if self._has_parent_tuple(child_path, parent_path, tenant_id):
                # Parent tuple exists, assume rest of chain exists too
                break

            # Create parent tuple
            self._create_parent_tuple(child_path, parent_path, tenant_id)
            created_count += 1

        # If we created any parent tuples, we need to invalidate related caches
        # The cache invalidation in rebac_write only invalidates direct relationships,
        # but parent tuples affect descendant permissions through inheritance
        if created_count > 0:
            # Clear cache for the entire path hierarchy to ensure fresh permission checks
            self._invalidate_cache_for_path_hierarchy(path)

        return created_count

    def _has_parent_tuple(
        self,
        child_path: str,
        parent_path: str,
        tenant_id: str | None,
    ) -> bool:
        """Check if parent tuple already exists.

        Checks for: (child) --[parent]--> (parent)

        Args:
            child_path: Child file path
            parent_path: Parent directory path
            tenant_id: Tenant ID for tuple scoping

        Returns:
            True if parent tuple exists
        """
        # Use ReBAC manager to check for existing tuple
        # We need to query the database directly since rebac_check
        # would recursively check, not just look for direct tuple

        from nexus.core.rebac import Entity

        child_entity = Entity("file", child_path)
        parent_entity = Entity("file", parent_path)

        # BUGFIX: Check child -> parent direction (child is subject, parent is object)
        # This matches _create_parent_tuple which creates (child, "parent", parent)
        # BUGFIX: Pass tenant_id to ensure proper scoping and prevent duplicate tuples
        return self.rebac_manager._has_direct_relation(
            subject=child_entity,
            relation="parent",
            obj=parent_entity,
            tenant_id=tenant_id,
        )

    def _create_parent_tuple(
        self,
        child_path: str,
        parent_path: str,
        tenant_id: str | None,
    ) -> None:
        """Create parent relationship tuple.

        Creates: (child) --[parent]--> (parent)
        Semantic: "child's parent is parent_path"
        This enables parent permissions to flow down to children via tupleToUserset.

        Args:
            child_path: Child file path
            parent_path: Parent directory path
            tenant_id: Tenant ID
        """
        # Check if rebac_manager supports tenant_id parameter
        # (EnhancedReBACManager does, basic ReBACManager doesn't)
        if hasattr(self.rebac_manager, "rebac_write") and tenant_id:
            try:
                # Try tenant-aware write
                # IMPORTANT: child is SUBJECT, parent is OBJECT (child -> parent direction)
                # Semantic: "child_path's parent is parent_path"
                self.rebac_manager.rebac_write(
                    subject=("file", child_path),
                    relation="parent",
                    object=("file", parent_path),
                    tenant_id=tenant_id,
                )
            except TypeError:
                # Fallback for basic ReBACManager (no tenant_id support)
                self.rebac_manager.rebac_write(
                    subject=("file", child_path),
                    relation="parent",
                    object=("file", parent_path),
                )
        else:
            # Basic ReBACManager
            self.rebac_manager.rebac_write(
                subject=("file", child_path),
                relation="parent",
                object=("file", parent_path),
            )

    def _invalidate_cache_for_path_hierarchy(self, path: str) -> None:
        """Invalidate cache for path, all ancestor paths, AND all descendant paths.

        When parent tuples are created, cached permission checks for descendant
        paths may become invalid. This method clears cache for the entire hierarchy.

        Args:
            path: File or directory path
        """
        # Get connection to cache database
        conn = self.rebac_manager._get_connection()
        cursor = self.rebac_manager._create_cursor(conn)

        # Build list of all paths to invalidate (ancestors + exact + descendants via LIKE)
        # Ancestors: /a/b/c -> [/a/b/c, /a/b, /a]
        parts = path.strip("/").split("/")
        ancestor_paths = []
        for i in range(len(parts), 0, -1):
            ancestor_paths.append("/" + "/".join(parts[:i]))

        # Invalidate cache for all ancestor paths (exact matches)
        for ancestor_path in ancestor_paths:
            cursor.execute(
                self.rebac_manager._fix_sql_placeholders(
                    """
                    DELETE FROM rebac_check_cache
                    WHERE object_type = 'file' AND object_id = ?
                    """
                ),
                (ancestor_path,),
            )

        # CRITICAL FIX: Also invalidate cache for all DESCENDANT paths
        # When parent tuples are created for /a/b, all cached checks for /a/b/c, /a/b/c/d, etc.
        # must be invalidated because they can now inherit permissions from /a/b
        cursor.execute(
            self.rebac_manager._fix_sql_placeholders(
                """
                DELETE FROM rebac_check_cache
                WHERE object_type = 'file' AND object_id LIKE ?
                """
            ),
            (path + "/%",),
        )

        conn.commit()

    def remove_parent_tuples(
        self,
        path: str,
        tenant_id: str | None = None,
    ) -> int:
        """Remove parent relationship tuples for path.

        Used when deleting files/directories to clean up graph.

        Args:
            path: File or directory path
            tenant_id: Tenant ID

        Returns:
            Number of parent tuples removed
        """
        if not self.enable_inheritance:
            return 0

        removed_count = 0

        # Find all tuples where this path is the subject (child)
        # and relation is "parent"
        # This requires querying the database directly

        conn = self.rebac_manager._get_connection()
        cursor = self.rebac_manager._create_cursor(conn)

        if tenant_id:
            # Tenant-aware query
            cursor.execute(
                self.rebac_manager._fix_sql_placeholders(
                    """
                    SELECT tuple_id
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND relation = ?
                      AND tenant_id = ?
                    """
                ),
                ("file", path, "parent", tenant_id),
            )
        else:
            # Non-tenant query
            cursor.execute(
                self.rebac_manager._fix_sql_placeholders(
                    """
                    SELECT tuple_id
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND relation = ?
                    """
                ),
                ("file", path, "parent"),
            )

        tuple_ids = []
        for row in cursor.fetchall():
            tuple_ids.append(row["tuple_id"])

        # Delete each tuple
        for tuple_id in tuple_ids:
            self.rebac_manager.rebac_delete(tuple_id)
            removed_count += 1

        return removed_count

    def rebuild_hierarchy(
        self,
        paths: list[str],
        tenant_id: str | None = None,
    ) -> int:
        """Rebuild parent tuples for a list of paths (batch operation).

        Useful for:
        - Initial migration to enable directory inheritance
        - Fixing broken parent relationships

        Args:
            paths: List of file/directory paths
            tenant_id: Tenant ID for all paths

        Returns:
            Total number of parent tuples created
        """
        if not self.enable_inheritance:
            return 0

        total_created = 0

        for path in paths:
            created = self.ensure_parent_tuples(path, tenant_id)
            total_created += created

        return total_created

    def get_parent_path(self, path: str) -> str | None:
        """Get parent directory path.

        Args:
            path: File or directory path

        Returns:
            Parent path or None if root
        """
        if not path or path == "/":
            return None

        parts = path.strip("/").split("/")
        if len(parts) < 2:
            return "/"

        return "/" + "/".join(parts[:-1])

    def get_all_parents(self, path: str) -> list[str]:
        """Get all parent paths in hierarchy order.

        Args:
            path: File or directory path

        Returns:
            List of parent paths from immediate parent to root
            Example: /a/b/c.txt -> ["/a/b", "/a"]
        """
        parents = []
        current = path

        while True:
            parent = self.get_parent_path(current)
            if not parent or parent == "/":
                break
            parents.append(parent)
            current = parent

        return parents
