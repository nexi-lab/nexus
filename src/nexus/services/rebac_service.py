"""ReBAC Service - Extracted from NexusFSReBACMixin.

This service handles all Relationship-Based Access Control operations:
- Core ReBAC operations (create, check, expand, delete tuples)
- Batch permission checking
- Namespace management
- Privacy and consent management
- Resource sharing (user/group)
- Dynamic viewer permissions

Phase 2: Core Refactoring (Issue #988, Task 2.2)
Extracted from: nexus_fs_rebac.py (2,554 lines)

Security Note: This service handles all permission and access control logic.
Changes require security review before deployment.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager


class ReBACService:
    """Independent ReBAC service extracted from NexusFS.

    Handles all relationship-based access control operations following
    Google Zanzibar principles.

    Core Concepts:
        - Tuple: (subject, relation, object) relationship
        - Subject: Who (user, group, service)
        - Relation: What permission (owner, can-read, can-write, member)
        - Object: What resource (file, folder, workspace)

    Architecture:
        - No filesystem dependencies
        - Pure permission logic and relationship management
        - Dependency injection for ReBAC manager

    Security:
        - All operations require authentication
        - Permission checks before granting access
        - Audit logging for sensitive operations
        - Rate limiting on expensive operations

    Example:
        ```python
        rebac = ReBACService(rebac_manager=manager, enforce_permissions=True)

        # Create relationship
        tuple_id = await rebac.rebac_create(
            subject=("user", "alice"),
            relation="owner",
            object=("file", "/doc.txt")
        )

        # Check permission
        has_access = await rebac.rebac_check(
            subject=("user", "alice"),
            permission="can-read",
            object=("file", "/doc.txt")
        )

        # Share with user
        share_id = await rebac.share_with_user(
            resource=("file", "/doc.txt"),
            target_user="bob",
            permission="can-read",
            context=ctx
        )
        ```
    """

    def __init__(
        self,
        rebac_manager: EnhancedReBACManager,
        enforce_permissions: bool = True,
        enable_audit_logging: bool = True,
    ):
        """Initialize ReBAC service.

        Args:
            rebac_manager: Enhanced ReBAC manager for relationship storage
            enforce_permissions: Whether to enforce permission checks
            enable_audit_logging: Whether to log permission grants/denials
        """
        self._rebac_manager = rebac_manager
        self._enforce_permissions = enforce_permissions
        self._enable_audit_logging = enable_audit_logging

        logger.info("[ReBACService] Initialized with audit_logging=%s", enable_audit_logging)

    # =========================================================================
    # Public API: Core ReBAC Operations
    # =========================================================================

    @rpc_expose(description="Create ReBAC relationship tuple")
    async def rebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        zone_id: str | None = None,
        context: Any = None,
        column_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a ReBAC relationship tuple (Issue #1081).

        Args:
            subject: Subject tuple (type, id) e.g., ("user", "alice")
            relation: Relation name e.g., "owner", "can-read", "member", "dynamic_viewer"
            object: Object tuple (type, id) e.g., ("file", "/doc.txt")
            expires_at: Optional expiration datetime for temporary relationships
            zone_id: Zone ID for multi-zone isolation
            context: Operation context for permission checks
            column_config: Optional column-level permissions for dynamic_viewer relation.
                          Only applies to CSV files. Structure:
                          {
                              "hidden_columns": ["password", "ssn"],
                              "aggregations": {"age": "mean", "salary": "sum"},
                              "visible_columns": ["name", "email"]
                          }

        Returns:
            Dict with tuple_id, revision, and consistency_token (Issue #1081).
            Use revision with consistency_mode="at_least_as_fresh" for read-your-writes.

        Raises:
            PermissionError: If caller lacks permission to grant
            ValueError: If tuple format is invalid or column_config invalid
            RuntimeError: If ReBAC manager not available

        Examples:
            # Grant ownership
            id = await rebac.rebac_create(
                subject=("user", "alice"),
                relation="owner",
                object=("file", "/doc.txt")
            )

            # Add group membership
            id = await rebac.rebac_create(
                subject=("user", "bob"),
                relation="member",
                object=("group", "developers")
            )

            # Dynamic viewer with column-level permissions for CSV
            id = await rebac.rebac_create(
                subject=("user", "alice"),
                relation="dynamic_viewer",
                object=("file", "/data/users.csv"),
                column_config={
                    "hidden_columns": ["password", "ssn"],
                    "aggregations": {"age": "mean", "salary": "sum"},
                    "visible_columns": ["name", "email"]
                }
            )

        Security:
            - Requires "execute" permission on the resource to grant permissions
            - Logged for audit trail
        """

        def _create_sync() -> dict[str, Any]:
            """Synchronous implementation for thread pool execution."""
            if not self._rebac_manager:
                raise RuntimeError(
                    "ReBAC manager is not available. Ensure ReBACService is properly initialized."
                )

            # Validate tuples (support 2-tuple and 3-tuple for subject to support userset-as-subject)
            if not isinstance(subject, tuple) or len(subject) not in (2, 3):
                raise ValueError(
                    f"subject must be (type, id) or (type, id, relation) tuple, got {subject}"
                )
            if not isinstance(object, tuple) or len(object) != 2:
                raise ValueError(f"object must be (type, id) tuple, got {object}")

            # Use zone_id from context if not explicitly provided
            effective_zone_id = zone_id
            if effective_zone_id is None and context:
                # Handle both dict and OperationContext
                if isinstance(context, dict):
                    effective_zone_id = context.get("zone")
                elif hasattr(context, "zone_id"):
                    effective_zone_id = context.zone_id

            # SECURITY: Check execute permission before allowing permission management
            # Only owners (those with execute permission) can grant/manage permissions on resources
            self._check_share_permission(resource=object, context=context)

            # Validate column_config for dynamic_viewer relation
            conditions = None
            if relation == "dynamic_viewer":
                # Check if object is a CSV file
                if object[0] == "file" and not object[1].lower().endswith(".csv"):
                    raise ValueError(
                        f"dynamic_viewer relation only supports CSV files. "
                        f"File '{object[1]}' does not have .csv extension."
                    )

                if column_config is None:
                    raise ValueError(
                        "column_config is required when relation is 'dynamic_viewer'. "
                        "Provide configuration with hidden_columns, aggregations, "
                        "and/or visible_columns."
                    )

                # Validate column_config structure
                if not isinstance(column_config, dict):
                    raise ValueError("column_config must be a dictionary")

                # Get all column categories
                hidden_columns = column_config.get("hidden_columns", [])
                aggregations = column_config.get("aggregations", {})
                visible_columns = column_config.get("visible_columns", [])

                # Validate types
                if not isinstance(hidden_columns, list):
                    raise ValueError("column_config.hidden_columns must be a list")
                if not isinstance(aggregations, dict):
                    raise ValueError("column_config.aggregations must be a dictionary")
                if not isinstance(visible_columns, list):
                    raise ValueError("column_config.visible_columns must be a list")

                # TODO: Add filesystem integration for CSV column validation
                # This requires NexusFS dependency which will be added in composition phase
                # For now, skip the CSV file column validation

                # Check that a column only appears in one category
                all_columns = set()
                for col in hidden_columns:
                    if col in all_columns:
                        raise ValueError(
                            f"Column '{col}' appears in multiple categories. "
                            f"Each column can only be in hidden_columns, aggregations, "
                            f"or visible_columns."
                        )
                    all_columns.add(col)

                for col in aggregations:
                    if col in all_columns:
                        raise ValueError(
                            f"Column '{col}' appears in multiple categories. "
                            f"Each column can only be in hidden_columns, aggregations, "
                            f"or visible_columns."
                        )
                    all_columns.add(col)

                for col in visible_columns:
                    if col in all_columns:
                        raise ValueError(
                            f"Column '{col}' appears in multiple categories. "
                            f"Each column can only be in hidden_columns, aggregations, "
                            f"or visible_columns."
                        )
                    all_columns.add(col)

                # Validate aggregation operations
                valid_ops = {"mean", "sum", "min", "max", "std", "median", "count"}
                for col, op in aggregations.items():
                    if not isinstance(op, str):
                        raise ValueError(
                            f"column_config.aggregations['{col}'] must be a string "
                            f"(one of: {', '.join(valid_ops)}). Got: {type(op).__name__}"
                        )
                    if op not in valid_ops:
                        raise ValueError(
                            f"Invalid aggregation operation '{op}' for column '{col}'. "
                            f"Valid operations: {', '.join(sorted(valid_ops))}"
                        )

                # Store column_config as conditions
                conditions = {"type": "dynamic_viewer", "column_config": column_config}
            elif column_config is not None:
                # column_config provided but relation is not dynamic_viewer
                raise ValueError(
                    "column_config can only be provided when relation is 'dynamic_viewer'"
                )

            # Create relationship tuple
            result = self._rebac_manager.rebac_write(
                subject=subject,
                relation=relation,
                object=object,
                expires_at=expires_at,
                zone_id=effective_zone_id,
                conditions=conditions,
            )

            # NOTE: Tiger Cache queue update is handled in EnhancedReBACManager.rebac_write()

            if self._enable_audit_logging:
                logger.info(
                    "[ReBACService] Created tuple: %s -[%s]-> %s (zone=%s, expires=%s)",
                    subject,
                    relation,
                    object,
                    effective_zone_id,
                    expires_at,
                )

            # Issue #1081: Return dict with WriteResult fields for API serialization
            return {
                "tuple_id": result.tuple_id,
                "revision": result.revision,
                "consistency_token": result.consistency_token,
            }

        # Run in thread pool since _rebac_manager operations may block
        import asyncio

        return await asyncio.to_thread(_create_sync)

    @rpc_expose(description="Check ReBAC permission")
    async def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: Any = None,
        zone_id: str | None = None,
        consistency_mode: str | None = None,  # Issue #1081
        min_revision: int | None = None,  # Issue #1081
    ) -> bool:
        """Check if subject has permission on object (Issue #1081).

        Uses relationship graph traversal to determine access, supporting both
        direct relationships and inherited permissions through group membership.

        Supports ABAC-style contextual conditions (time windows, IP allowlists, etc.)
        and per-request consistency modes aligned with SpiceDB/Zanzibar.

        Args:
            subject: Subject tuple e.g., ("user", "alice")
            permission: Permission to check e.g., "read", "write", "owner"
            object: Object tuple e.g., ("file", "/doc.txt")
            context: Optional ABAC context for condition evaluation (time, ip, device, attributes)
            zone_id: Zone ID for multi-zone isolation
            consistency_mode: Per-request consistency mode (Issue #1081):
                - "minimize_latency" (default): Use cache for fastest response
                - "at_least_as_fresh": Cache must be >= min_revision
                - "fully_consistent": Bypass cache entirely
            min_revision: Minimum acceptable revision (required for at_least_as_fresh)

        Returns:
            True if permission granted, False otherwise

        Raises:
            ValueError: If subject or object tuples are invalid
            RuntimeError: If ReBAC manager not available

        Examples:
            # Check read access (default: minimize_latency)
            can_read = await rebac.rebac_check(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt")
            )

            # Check after a write with read-your-writes guarantee
            can_read = await rebac.rebac_check(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt"),
                consistency_mode="at_least_as_fresh",
                min_revision=123  # From previous write result
            )

            # Security audit: bypass all caches
            can_read = await rebac.rebac_check(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt"),
                consistency_mode="fully_consistent"
            )
        """

        def _check_sync() -> bool:
            """Synchronous implementation for thread pool execution."""
            if not self._rebac_manager:
                raise RuntimeError(
                    "ReBAC manager is not available. Ensure ReBACService is properly initialized."
                )

            # Validate tuples
            if not isinstance(subject, tuple) or len(subject) != 2:
                raise ValueError(f"subject must be (type, id) tuple, got {subject}")
            if not isinstance(object, tuple) or len(object) != 2:
                raise ValueError(f"object must be (type, id) tuple, got {object}")

            # Use zone_id from context if not explicitly provided
            effective_zone_id = zone_id
            if effective_zone_id is None and context:
                # Handle both dict and OperationContext
                if isinstance(context, dict):
                    effective_zone_id = context.get("zone")
                elif hasattr(context, "zone_id"):
                    effective_zone_id = context.zone_id

            # Issue #1081: Build consistency requirement from API params
            consistency = None
            if consistency_mode or min_revision is not None:
                from nexus.core.rebac_manager_enhanced import (
                    ConsistencyMode,
                    ConsistencyRequirement,
                )

                mode = ConsistencyMode.MINIMIZE_LATENCY
                if consistency_mode == "at_least_as_fresh":
                    mode = ConsistencyMode.AT_LEAST_AS_FRESH
                elif consistency_mode == "fully_consistent":
                    mode = ConsistencyMode.FULLY_CONSISTENT

                consistency = ConsistencyRequirement(mode=mode, min_revision=min_revision)

            # Check permission with optional ABAC context and consistency
            result = self._rebac_manager.rebac_check(
                subject=subject,
                permission=permission,
                object=object,
                context=context,
                zone_id=effective_zone_id,
                consistency=consistency,
            )

            # TODO: Unix-like TRAVERSE behavior fallback
            # If permission is "traverse" and object is a file path,
            # check if user has READ on any descendant (deferred for now)
            # This requires _has_descendant_access_for_traverse() helper

            return result

        # Run in thread pool since _rebac_manager operations may block
        import asyncio

        return await asyncio.to_thread(_check_sync)

    @rpc_expose(description="Expand ReBAC permissions to find all subjects")
    async def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        _zone_id: str | None = None,
        _limit: int = 100,
    ) -> list[tuple[str, str]]:
        """Find all subjects that have a permission on an object.

        Uses recursive graph expansion to find both direct and inherited permissions.

        Args:
            permission: Permission to check e.g., "read", "write", "owner"
            object: Object tuple e.g., ("file", "/doc.txt")
            zone_id: Zone ID for multi-zone isolation
            limit: Maximum results (not currently enforced by manager)

        Returns:
            List of subject tuples with the permission

        Raises:
            ValueError: If object tuple is invalid
            RuntimeError: If ReBAC manager not available

        Examples:
            # Find all users who can read a file
            readers = await rebac.rebac_expand(
                permission="read",
                object=("file", "/doc.txt")
            )
            # Returns: [("user", "alice"), ("user", "bob"), ...]

            # Who owns this workspace?
            owners = await rebac.rebac_expand(
                permission="owner",
                object=("workspace", "/workspace")
            )
        """

        def _expand_sync() -> list[tuple[str, str]]:
            """Synchronous implementation for thread pool execution."""
            if not self._rebac_manager:
                raise RuntimeError(
                    "ReBAC manager is not available. Ensure ReBACService is properly initialized."
                )

            # Validate tuple
            if not isinstance(object, tuple) or len(object) != 2:
                raise ValueError(f"object must be (type, id) tuple, got {object}")

            # Expand permission
            return self._rebac_manager.rebac_expand(permission=permission, object=object)

        # Run in thread pool since _rebac_manager operations may block
        import asyncio

        return await asyncio.to_thread(_expand_sync)

    @rpc_expose(description="Explain ReBAC permission check")
    async def rebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Explain why a subject has or doesn't have permission.

        This debugging API traces through the permission graph to show exactly
        why a permission check succeeded or failed.

        Args:
            subject: Subject tuple e.g., ("user", "alice")
            permission: Permission to explain e.g., "read", "write", "owner"
            object: Object tuple e.g., ("file", "/doc.txt")
            zone_id: Zone ID for multi-zone isolation
            context: Operation context (automatically provided by RPC server)

        Returns:
            Dictionary with:
            - result: bool - whether permission is granted
            - cached: bool - whether result came from cache
            - reason: str - human-readable explanation
            - paths: list[dict] - all checked paths through the graph
            - successful_path: dict | None - the path that granted access (if any)

        Raises:
            ValueError: If subject or object tuples are invalid
            RuntimeError: If ReBAC manager not available

        Examples:
            # Why does alice have read permission?
            explanation = await rebac.rebac_explain(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/workspace/doc.txt")
            )
            print(explanation["reason"])

            # Why doesn't bob have write permission?
            explanation = await rebac.rebac_explain(
                subject=("user", "bob"),
                permission="write",
                object=("workspace", "/workspace")
            )
            print(explanation["result"])  # False
        """

        def _explain_sync() -> dict[str, Any]:
            """Synchronous implementation for thread pool execution."""
            if not self._rebac_manager:
                raise RuntimeError(
                    "ReBAC manager is not available. Ensure ReBACService is properly initialized."
                )

            # Validate tuples
            if not isinstance(subject, tuple) or len(subject) != 2:
                raise ValueError(f"subject must be (type, id) tuple, got {subject}")
            if not isinstance(object, tuple) or len(object) != 2:
                raise ValueError(f"object must be (type, id) tuple, got {object}")

            # Use zone_id from context if not explicitly provided
            effective_zone_id = zone_id
            if effective_zone_id is None and context:
                # Handle both dict and OperationContext
                if isinstance(context, dict):
                    effective_zone_id = context.get("zone")
                elif hasattr(context, "zone_id"):
                    effective_zone_id = context.zone_id

            # Get explanation from manager
            return self._rebac_manager.rebac_explain(
                subject=subject,
                permission=permission,
                object=object,
                zone_id=effective_zone_id,
            )

        # Run in thread pool since _rebac_manager operations may block
        import asyncio

        return await asyncio.to_thread(_explain_sync)

    @rpc_expose(description="Batch ReBAC permission checks")
    async def rebac_check_batch(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        _zone_id: str | None = None,
    ) -> list[bool]:
        """Check multiple permissions in a single call for efficiency.

        Performs multiple permission checks with shared cache lookups and optimized
        database queries. More efficient than individual checks.

        Args:
            checks: List of (subject, permission, object) tuples
            zone_id: Zone ID (currently unused by manager)

        Returns:
            List of boolean results (same order as input)

        Raises:
            ValueError: If any check tuple is invalid
            RuntimeError: If ReBAC manager not available

        Examples:
            # Check multiple files at once
            results = await rebac.rebac_check_batch([
                (("user", "alice"), "read", ("file", "/a.txt")),
                (("user", "alice"), "read", ("file", "/b.txt")),
                (("user", "alice"), "write", ("file", "/c.txt")),
            ])
            # Returns: [True, True, False]

            # Check if user has multiple permissions on same object
            results = await rebac.rebac_check_batch([
                (("user", "alice"), "read", ("file", "/project")),
                (("user", "alice"), "write", ("file", "/project")),
                (("user", "alice"), "owner", ("file", "/project")),
            ])
        """

        def _check_batch_sync() -> list[bool]:
            """Synchronous implementation for thread pool execution."""
            if not self._rebac_manager:
                raise RuntimeError(
                    "ReBAC manager is not available. Ensure ReBACService is properly initialized."
                )

            # Validate all checks
            for i, check in enumerate(checks):
                if not isinstance(check, tuple) or len(check) != 3:
                    raise ValueError(f"Check {i} must be (subject, permission, object) tuple")
                subject, permission, obj = check
                if not isinstance(subject, tuple) or len(subject) != 2:
                    raise ValueError(f"Check {i}: subject must be (type, id) tuple, got {subject}")
                if not isinstance(obj, tuple) or len(obj) != 2:
                    raise ValueError(f"Check {i}: object must be (type, id) tuple, got {obj}")

            # Perform batch check with Rust acceleration
            return self._rebac_manager.rebac_check_batch_fast(checks=checks)

        # Run in thread pool since _rebac_manager operations may block
        import asyncio

        return await asyncio.to_thread(_check_batch_sync)

    @rpc_expose(description="Delete ReBAC relationship tuple")
    async def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a relationship tuple by ID.

        Args:
            tuple_id: UUID of tuple to delete (returned from rebac_create)

        Returns:
            True if deleted, False if not found

        Raises:
            RuntimeError: If ReBAC manager not available

        Examples:
            # Delete a relationship
            tuple_id = await rebac.rebac_create(
                subject=("user", "alice"),
                relation="viewer",
                object=("file", "/workspace/doc.txt")
            )
            success = await rebac.rebac_delete(tuple_id)

        Security:
            - Tiger Cache invalidation handled by manager
            - Logged for audit trail
        """

        def _delete_sync() -> bool:
            """Synchronous implementation for thread pool execution."""
            if not self._rebac_manager:
                raise RuntimeError(
                    "ReBAC manager is not available. Ensure ReBACService is properly initialized."
                )

            # Delete tuple - enhanced rebac_delete handles Tiger Cache invalidation
            result = self._rebac_manager.rebac_delete(tuple_id=tuple_id)

            if self._enable_audit_logging and result:
                logger.info("[ReBACService] Deleted tuple: %s", tuple_id)

            return result

        # Run in thread pool since _rebac_manager operations may block
        import asyncio

        return await asyncio.to_thread(_delete_sync)

    @rpc_expose(description="List ReBAC relationship tuples")
    async def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
        _zone_id: str | None = None,
        _limit: int = 100,
        _offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List relationship tuples with optional filters.

        Args:
            subject: Filter by subject (optional)
            relation: Filter by relation (optional, mutually exclusive with relation_in)
            object: Filter by object (optional)
            relation_in: Filter by multiple relations (optional, mutually exclusive with relation)
            zone_id: Zone ID for multi-zone isolation
            limit: Maximum results (not currently enforced)
            offset: Pagination offset (not currently enforced)

        Returns:
            List of tuple dicts with:
            - tuple_id: str
            - subject_type: str
            - subject_id: str
            - relation: str
            - object_type: str
            - object_id: str
            - created_at: datetime
            - expires_at: datetime | None
            - zone_id: str | None

        Raises:
            RuntimeError: If ReBAC manager not available

        Examples:
            # List all permissions for a user
            tuples = await rebac.rebac_list_tuples(
                subject=("user", "alice")
            )

            # List all owners of a file
            tuples = await rebac.rebac_list_tuples(
                relation="owner",
                object=("file", "/doc.txt")
            )

            # List tuples with multiple relation types (efficient single query)
            tuples = await rebac.rebac_list_tuples(
                subject=("user", "alice"),
                relation_in=["shared-viewer", "shared-editor", "shared-owner"]
            )
        """

        def _list_tuples_sync() -> list[dict[str, Any]]:
            """Synchronous implementation for thread pool execution."""
            if not self._rebac_manager:
                raise RuntimeError(
                    "ReBAC manager is not available. Ensure ReBACService is properly initialized."
                )

            # Build query dynamically with filters
            conn = self._rebac_manager._get_connection()
            try:
                query = "SELECT * FROM rebac_tuples WHERE 1=1"
                params: list = []

                if subject:
                    query += " AND subject_type = ? AND subject_id = ?"
                    params.extend([subject[0], subject[1]])

                if relation:
                    query += " AND relation = ?"
                    params.append(relation)
                elif relation_in:
                    # Support multiple relations in a single query (N+1 fix)
                    placeholders = ", ".join("?" * len(relation_in))
                    query += f" AND relation IN ({placeholders})"
                    params.extend(relation_in)

                if object:
                    query += " AND object_type = ? AND object_id = ?"
                    params.extend([object[0], object[1]])

                # Fix SQL placeholders for PostgreSQL if needed
                query = self._rebac_manager._fix_sql_placeholders(query)

                cursor = self._rebac_manager._create_cursor(conn)
                cursor.execute(query, params)

                results = []
                for row in cursor.fetchall():
                    # Both SQLite and PostgreSQL return dict-like rows
                    # Note: sqlite3.Row doesn't have .get() method, use try/except
                    try:
                        zone_id_val = row["zone_id"]
                    except (KeyError, IndexError):
                        zone_id_val = None

                    results.append(
                        {
                            "tuple_id": row["tuple_id"],
                            "subject_type": row["subject_type"],
                            "subject_id": row["subject_id"],
                            "relation": row["relation"],
                            "object_type": row["object_type"],
                            "object_id": row["object_id"],
                            "created_at": row["created_at"],
                            "expires_at": row["expires_at"],
                            "zone_id": zone_id_val,
                        }
                    )

                return results
            finally:
                self._rebac_manager._close_connection(conn)

        # Run in thread pool since database operations block
        import asyncio

        return await asyncio.to_thread(_list_tuples_sync)

    # =========================================================================
    # Public API: Configuration & Namespaces
    # =========================================================================

    @rpc_expose(description="Set ReBAC configuration option")
    def set_rebac_option(self, key: str, value: Any) -> None:
        """Set a ReBAC configuration option.

        Provides public access to ReBAC configuration without using internal APIs.

        Args:
            key: Configuration key (e.g., "max_depth", "cache_ttl")
            value: Configuration value

        Raises:
            ValueError: If key is invalid or value has wrong type
            RuntimeError: If ReBAC manager not available

        Examples:
            # Set maximum graph traversal depth
            rebac.set_rebac_option("max_depth", 15)

            # Set cache TTL
            rebac.set_rebac_option("cache_ttl", 600)
        """
        if not self._rebac_manager:
            raise RuntimeError(
                "ReBAC manager is not available. Ensure ReBACService is properly initialized."
            )

        if key == "max_depth":
            if not isinstance(value, int) or value < 1:
                raise ValueError("max_depth must be a positive integer")
            self._rebac_manager.max_depth = value
        elif key == "cache_ttl":
            if not isinstance(value, int) or value < 0:
                raise ValueError("cache_ttl must be a non-negative integer")
            self._rebac_manager.cache_ttl_seconds = value
        else:
            raise ValueError(f"Unknown ReBAC option: {key}. Valid options: max_depth, cache_ttl")

    @rpc_expose(description="Get ReBAC configuration option")
    def get_rebac_option(self, key: str) -> Any:
        """Get a ReBAC configuration option.

        Args:
            key: Configuration key (e.g., "max_depth", "cache_ttl")

        Returns:
            Current value of the configuration option

        Raises:
            ValueError: If key is invalid
            RuntimeError: If ReBAC manager not available

        Examples:
            # Get current max depth
            depth = rebac.get_rebac_option("max_depth")
            print(f"Max traversal depth: {depth}")
        """
        if not self._rebac_manager:
            raise RuntimeError(
                "ReBAC manager is not available. Ensure ReBACService is properly initialized."
            )

        if key == "max_depth":
            return self._rebac_manager.max_depth
        elif key == "cache_ttl":
            return self._rebac_manager.cache_ttl_seconds
        else:
            raise ValueError(f"Unknown ReBAC option: {key}. Valid options: max_depth, cache_ttl")

    @rpc_expose(description="Register ReBAC namespace schema")
    def register_namespace(self, namespace: dict[str, Any]) -> None:
        """Register a namespace schema for ReBAC.

        Provides public API to register namespace configurations without using internal APIs.
        Namespaces define the permission model for object types (e.g., files, workspaces).

        Args:
            namespace: Namespace configuration dictionary with keys:
                - object_type: Type of objects this namespace applies to
                - config: Schema configuration (relations and permissions)
                - namespace_id: Optional UUID (auto-generated if not provided)

        Raises:
            ValueError: If namespace configuration is invalid
            RuntimeError: If ReBAC manager not available

        Examples:
            # Register file namespace with group inheritance
            rebac.register_namespace({
                "object_type": "file",
                "config": {
                    "relations": {
                        "viewer": {},
                        "editor": {}
                    },
                    "permissions": {
                        "read": ["viewer", "editor"],
                        "write": ["editor"]
                    }
                }
            })
        """
        if not self._rebac_manager:
            raise RuntimeError(
                "ReBAC manager is not available. Ensure ReBACService is properly initialized."
            )

        # Validate namespace structure
        if not isinstance(namespace, dict):
            raise ValueError("namespace must be a dictionary")
        if "object_type" not in namespace:
            raise ValueError("namespace must have 'object_type' key")
        if "config" not in namespace:
            raise ValueError("namespace must have 'config' key")

        # Import dependencies
        import uuid

        from nexus.core.rebac import NamespaceConfig

        # Create NamespaceConfig object
        ns = NamespaceConfig(
            namespace_id=namespace.get("namespace_id", str(uuid.uuid4())),
            object_type=namespace["object_type"],
            config=namespace["config"],
        )

        # Register via manager
        self._rebac_manager.create_namespace(ns)

    @rpc_expose(description="Get ReBAC namespace schema")
    async def get_namespace(self, object_type: str) -> dict[str, Any] | None:
        """Get namespace schema for an object type.

        Args:
            object_type: Type of object (e.g., "file", "folder")

        Returns:
            Namespace configuration or None
        """
        # TODO: Extract get_namespace implementation
        raise NotImplementedError("get_namespace() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="Create or update ReBAC namespace")
    async def namespace_create(self, object_type: str, config: dict[str, Any]) -> None:
        """Create or update a namespace configuration."""
        # TODO: Extract namespace_create implementation
        raise NotImplementedError("namespace_create() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="List all ReBAC namespaces")
    async def namespace_list(self) -> list[dict[str, Any]]:
        """List all registered namespace configurations."""
        # TODO: Extract namespace_list implementation
        raise NotImplementedError("namespace_list() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="Delete ReBAC namespace")
    async def namespace_delete(self, object_type: str) -> bool:
        """Delete a namespace configuration."""
        # TODO: Extract namespace_delete implementation
        raise NotImplementedError("namespace_delete() not yet implemented - Phase 2 in progress")

    # =========================================================================
    # Public API: Privacy & Consent
    # =========================================================================

    @rpc_expose(description="Expand ReBAC permissions with privacy filtering")
    async def rebac_expand_with_privacy(
        self,
        permission: str,
        object: tuple[str, str],
        requesting_subject: tuple[str, str],
        zone_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        """Expand permissions with privacy filtering.

        Only returns subjects that have granted discovery consent.
        """
        # TODO: Extract rebac_expand_with_privacy implementation
        raise NotImplementedError(
            "rebac_expand_with_privacy() not yet implemented - Phase 2 in progress"
        )

    @rpc_expose(description="Grant consent for discovery")
    async def grant_consent(
        self,
        from_subject: tuple[str, str],
        to_subject: tuple[str, str],
        context: Any = None,
    ) -> str:
        """Grant consent for another subject to discover you in permission expansion."""
        # TODO: Extract grant_consent implementation
        raise NotImplementedError("grant_consent() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="Revoke consent")
    async def revoke_consent(
        self, from_subject: tuple[str, str], to_subject: tuple[str, str]
    ) -> bool:
        """Revoke previously granted consent."""
        # TODO: Extract revoke_consent implementation
        raise NotImplementedError("revoke_consent() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="Make resource publicly discoverable")
    async def make_public(self, resource: tuple[str, str], zone_id: str | None = None) -> str:
        """Make a resource publicly discoverable."""
        # TODO: Extract make_public implementation
        raise NotImplementedError("make_public() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="Make resource private")
    async def make_private(self, resource: tuple[str, str]) -> bool:
        """Remove public discoverability from a resource."""
        # TODO: Extract make_private implementation
        raise NotImplementedError("make_private() not yet implemented - Phase 2 in progress")

    # =========================================================================
    # Public API: Resource Sharing
    # =========================================================================

    @rpc_expose(description="Share a resource with a specific user (same or different zone)")
    async def share_with_user(
        self,
        resource: tuple[str, str],
        target_user: str,
        permission: str = "can-read",
        context: Any = None,
        target_zone_id: str | None = None,
        expiry: datetime | None = None,
        message: str | None = None,
    ) -> str:
        """Share a resource with a specific user.

        Security:
            - Requires "execute" permission on resource
            - Creates relationship tuple
            - Logs share for audit

        Args:
            resource: Resource tuple e.g., ("file", "/doc.txt")
            target_user: User ID to share with
            permission: Permission to grant (default: "can-read")
            context: Operation context
            target_zone_id: Target zone (for cross-zone sharing)
            expiry: Optional expiry datetime
            message: Optional message to recipient

        Returns:
            Share ID (tuple_id)
        """
        # TODO: Extract share_with_user implementation
        raise NotImplementedError("share_with_user() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="Share a resource with a group (all members get access)")
    async def share_with_group(
        self,
        resource: tuple[str, str],
        target_group: str,
        permission: str = "can-read",
        context: Any = None,
        expiry: datetime | None = None,
    ) -> str:
        """Share a resource with a group (all members get access)."""
        # TODO: Extract share_with_group implementation
        raise NotImplementedError("share_with_group() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="Revoke a share by resource and user")
    async def revoke_share(
        self,
        resource: tuple[str, str],
        target_user: str,
        context: Any = None,
    ) -> bool:
        """Revoke a share by resource and user."""
        # TODO: Extract revoke_share implementation
        raise NotImplementedError("revoke_share() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="Revoke a share by share ID")
    async def revoke_share_by_id(self, share_id: str) -> bool:
        """Revoke a share using its ID (tuple_id)."""
        # TODO: Extract revoke_share_by_id implementation
        raise NotImplementedError("revoke_share_by_id() not yet implemented - Phase 2 in progress")

    @rpc_expose(description="List shares I've created (outgoing)")
    async def list_outgoing_shares(
        self,
        resource: tuple[str, str] | None = None,
        context: Any = None,
    ) -> list[dict[str, Any]]:
        """List shares created by the caller (outgoing shares)."""
        # TODO: Extract list_outgoing_shares implementation
        raise NotImplementedError(
            "list_outgoing_shares() not yet implemented - Phase 2 in progress"
        )

    @rpc_expose(description="List shares I've received (incoming)")
    async def list_incoming_shares(
        self,
        user_id: str,
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List shares received by a user (incoming shares)."""
        # TODO: Extract list_incoming_shares implementation
        raise NotImplementedError(
            "list_incoming_shares() not yet implemented - Phase 2 in progress"
        )

    # =========================================================================
    # Public API: Dynamic Viewer Permissions
    # =========================================================================

    @rpc_expose(description="Get dynamic viewer configuration for a file")
    async def get_dynamic_viewer_config(
        self,
        subject: tuple[str, str],
        file_path: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Get dynamic viewer configuration (column visibility, filters) for a file."""
        # TODO: Extract get_dynamic_viewer_config implementation
        raise NotImplementedError(
            "get_dynamic_viewer_config() not yet implemented - Phase 2 in progress"
        )

    @rpc_expose(description="Apply dynamic viewer filter to CSV data")
    async def apply_dynamic_viewer_filter(
        self,
        data: str,
        columns_allowed: list[str],
        format: str = "csv",
    ) -> str:
        """Apply dynamic viewer filter to data (filter columns)."""
        # TODO: Extract apply_dynamic_viewer_filter implementation
        raise NotImplementedError(
            "apply_dynamic_viewer_filter() not yet implemented - Phase 2 in progress"
        )

    @rpc_expose(description="Read file with dynamic viewer permissions applied")
    async def read_with_dynamic_viewer(
        self,
        file_path: str,
        subject: tuple[str, str],
        context: Any = None,
    ) -> str | bytes:
        """Read file content with dynamic viewer permissions applied."""
        # TODO: Extract read_with_dynamic_viewer implementation
        raise NotImplementedError(
            "read_with_dynamic_viewer() not yet implemented - Phase 2 in progress"
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_subject_from_context(self, context: Any) -> tuple[str, str] | None:
        """Extract subject from operation context.

        Args:
            context: Operation context (OperationContext, EnhancedOperationContext, or dict)

        Returns:
            Subject tuple (type, id) or None if not found

        Examples:
            >>> context = {"subject": ("user", "alice")}
            >>> self._get_subject_from_context(context)
            ('user', 'alice')

            >>> context = OperationContext(user="alice", groups=[])
            >>> self._get_subject_from_context(context)
            ('user', 'alice')
        """
        if not context:
            return None

        # Handle dict format (used by RPC server and tests)
        if isinstance(context, dict):
            subject = context.get("subject")
            if subject and isinstance(subject, tuple) and len(subject) == 2:
                return (str(subject[0]), str(subject[1]))

            # Construct from subject_type + subject_id
            subject_type = context.get("subject_type", "user")
            subject_id = context.get("subject_id") or context.get("user")
            if subject_id:
                return (subject_type, subject_id)

            return None

        # Handle OperationContext format - use get_subject() method
        if hasattr(context, "get_subject") and callable(context.get_subject):
            result = context.get_subject()
            if result is not None:
                return (str(result[0]), str(result[1]))
            return None

        # Fallback: construct from attributes
        if hasattr(context, "subject_type") and hasattr(context, "subject_id"):
            subject_type = getattr(context, "subject_type", "user")
            subject_id = getattr(context, "subject_id", None) or getattr(context, "user", None)
            if subject_id:
                return (subject_type, subject_id)

        # Last resort: use user field
        if hasattr(context, "user") and context.user:
            return ("user", context.user)

        return None

    def _check_share_permission(
        self,
        resource: tuple[str, str],
        context: Any,
        required_permission: str = "execute",
    ) -> None:
        """Check if caller has permission to share/manage a resource.

        This helper centralizes the permission check logic used by rebac_create,
        share_with_user, and share_with_group to prevent code duplication.

        Args:
            resource: Resource tuple (object_type, object_id)
            context: Operation context (OperationContext, EnhancedOperationContext, or dict)
            required_permission: Permission level required (default: "execute" for ownership)

        Raises:
            PermissionError: If caller lacks required permission to manage the resource

        Examples:
            >>> self._check_share_permission(
            ...     resource=("file", "/path/doc.txt"),
            ...     context=operation_context
            ... )
        """
        if not context:
            return

        from nexus.core.permissions import OperationContext, Permission

        # Extract OperationContext from context parameter
        op_context: OperationContext | None = None
        if isinstance(context, OperationContext):
            op_context = context
        elif isinstance(context, dict):
            # Create OperationContext from dict
            op_context = OperationContext(
                user=context.get("user", "unknown"),
                groups=context.get("groups", []),
                zone_id=context.get("zone_id"),
                is_admin=context.get("is_admin", False),
                is_system=context.get("is_system", False),
            )

        # Skip permission check for admin and system contexts
        if not op_context or not self._enforce_permissions:
            return
        if op_context.is_admin or op_context.is_system:
            return

        # Check if caller has required permission on the resource
        # Map string permission to Permission enum
        permission_map = {
            "execute": Permission.EXECUTE,
            "write": Permission.WRITE,
            "read": Permission.READ,
        }
        perm_enum = permission_map.get(required_permission, Permission.EXECUTE)

        # For file resources, use the path directly
        if resource[0] == "file":
            resource_path = resource[1]
        else:
            # For non-file resources, we need to check ReBAC permissions
            # This ensures groups, workspaces, and other resources are also protected
            # Check if user has ownership (execute permission) via ReBAC
            has_permission = self.rebac_check(
                subject=self._get_subject_from_context(context) or ("user", op_context.user),
                permission="owner",  # Only owners can manage permissions
                object=resource,
                context=context,
            )
            if not has_permission:
                raise PermissionError(
                    f"Access denied: User '{op_context.user}' does not have owner "
                    f"permission to manage {resource[0]} '{resource[1]}'"
                )
            return

        # Use permission enforcer to check permission for file resources
        if hasattr(self, "_permission_enforcer"):
            has_permission = self._permission_enforcer.check(resource_path, perm_enum, op_context)

            # If user is not owner, check if they are zone admin
            if not has_permission:
                # Extract zone from resource path (format: /zone:{zone_id}/...)
                zone_id = None
                if resource_path.startswith("/zone:"):
                    parts = resource_path[6:].split("/", 1)  # Remove "/zone:" prefix
                    if parts:
                        zone_id = parts[0]

                # Check if user is zone admin for this resource's zone
                if zone_id and op_context.user:
                    from nexus.server.auth.user_helpers import is_zone_admin

                    if is_zone_admin(self._rebac_manager, op_context.user, zone_id):
                        # Zone admin can share resources in their zone
                        return

                # Neither owner nor zone admin - deny
                perm_name = required_permission.upper()
                raise PermissionError(
                    f"Access denied: User '{op_context.user}' does not have {perm_name} "
                    f"permission to manage permissions on '{resource_path}'. "
                    f"Only owners or zone admins can share resources."
                )


# =============================================================================
# Phase 2 Extraction Progress
# =============================================================================
#
# Status: Skeleton created ✅
#
# TODO (in order of priority):
# 1. [ ] Extract core ReBAC operations (rebac_create, rebac_check, etc.)
# 2. [ ] Extract batch operations (rebac_check_batch)
# 3. [ ] Extract namespace management
# 4. [ ] Extract privacy/consent operations
# 5. [ ] Extract sharing operations (share_with_user, share_with_group)
# 6. [ ] Extract dynamic viewer operations
# 7. [ ] Extract helper methods
# 8. [ ] Add unit tests for ReBACService
# 9. [ ] Security review (REQUIRED before merge)
# 10. [ ] Update NexusFS to use composition
# 11. [ ] Add backward compatibility shims with deprecation warnings
# 12. [ ] Update documentation and migration guide
#
# Lines extracted: 0 / 2,554 (0%)
# Files affected: 1 created, 0 modified
#
# SECURITY NOTE:
# This service handles all permission logic. All changes require:
# - Code review by 2+ developers
# - Security review
# - Penetration testing
# - Audit log verification
#
