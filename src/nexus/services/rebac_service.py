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

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from nexus.core.exceptions import CircuitOpenError
from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

T = TypeVar("T")

if TYPE_CHECKING:
    from nexus.services.permissions.circuit_breaker import AsyncCircuitBreaker
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager


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
        rebac_manager: EnhancedReBACManager | None,
        enforce_permissions: bool = True,
        enable_audit_logging: bool = True,
        circuit_breaker: AsyncCircuitBreaker | None = None,
        file_reader: Callable | None = None,
    ):
        """Initialize ReBAC service.

        Args:
            rebac_manager: Enhanced ReBAC manager for relationship storage
            enforce_permissions: Whether to enforce permission checks
            enable_audit_logging: Whether to log permission grants/denials
            circuit_breaker: Optional circuit breaker for database resilience (Issue #726)
            file_reader: Optional callback ``(path) -> bytes|str`` for CSV column validation.
                         Provided by NexusFS at composition time.
        """
        self._rebac_manager = rebac_manager
        self._enforce_permissions = enforce_permissions
        self._enable_audit_logging = enable_audit_logging
        self._circuit_breaker = circuit_breaker
        self._file_reader = file_reader

        logger.info(
            "[ReBACService] Initialized with audit_logging=%s, circuit_breaker=%s",
            enable_audit_logging,
            "enabled" if circuit_breaker else "disabled",
        )

    # =========================================================================
    # Internal: Thread Pool Helper with Circuit Breaker (Issue #726)
    # =========================================================================

    async def _run_in_thread(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Execute sync ReBAC operation in thread pool with circuit breaker protection.

        Decision 14A: Checks circuit state BEFORE spawning a thread to avoid
        exhausting the thread pool during a DB outage.

        Args:
            fn: Synchronous callable to run in a thread.
            *args: Positional arguments forwarded to *fn*.
            **kwargs: Keyword arguments forwarded to *fn*.

        Returns:
            The return value of *fn*.

        Raises:
            RuntimeError: If ReBAC manager is not available.
            CircuitOpenError: If the circuit breaker is open.
        """
        if not self._rebac_manager:
            raise RuntimeError(
                "ReBAC manager is not available. Ensure ReBACService is properly initialized."
            )

        # Issue #702: Propagate OTel context to worker thread so spans are
        # children of the async caller's span.
        from nexus.services.permissions.rebac_tracing import propagate_otel_context

        fn_with_ctx = propagate_otel_context(fn)

        if self._circuit_breaker:
            return await self._circuit_breaker.call(asyncio.to_thread, fn_with_ctx, *args, **kwargs)
        return await asyncio.to_thread(fn_with_ctx, *args, **kwargs)

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

            # Create relationship tuple (manager guaranteed by _run_in_thread)
            assert self._rebac_manager is not None
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

        # Write operation — no cache fallback (Decision 3A)
        return await self._run_in_thread(_create_sync)

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
                from nexus.services.permissions.rebac_manager_enhanced import (
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
            # Manager guaranteed by _run_in_thread
            assert self._rebac_manager is not None
            result = self._rebac_manager.rebac_check(
                subject=subject,
                permission=permission,
                object=object,
                context=context,
                zone_id=effective_zone_id,
                consistency=consistency,
            )

            return result

        # Read operation — supports L1 cache fallback (Decision 3A)
        try:
            return await self._run_in_thread(_check_sync)
        except CircuitOpenError:
            if self._rebac_manager:
                cached = self._rebac_manager.get_cached_permission(
                    subject=subject, permission=permission, object=object, zone_id=zone_id
                )
                if cached is not None:
                    logger.warning(
                        "[ReBACService] Circuit open — serving cached permission for %s %s %s",
                        subject,
                        permission,
                        object,
                    )
                    return cached
            raise

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
            # Validate tuple
            if not isinstance(object, tuple) or len(object) != 2:
                raise ValueError(f"object must be (type, id) tuple, got {object}")

            # Expand permission (manager guaranteed by _run_in_thread)
            assert self._rebac_manager is not None
            return self._rebac_manager.rebac_expand(permission=permission, object=object)

        return await self._run_in_thread(_expand_sync)

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

            # Get explanation from manager (manager guaranteed by _run_in_thread)
            assert self._rebac_manager is not None
            return self._rebac_manager.rebac_explain(
                subject=subject,
                permission=permission,
                object=object,
                zone_id=effective_zone_id,
            )

        return await self._run_in_thread(_explain_sync)

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
            # Validate all checks
            for i, check in enumerate(checks):
                if not isinstance(check, tuple) or len(check) != 3:
                    raise ValueError(f"Check {i} must be (subject, permission, object) tuple")
                subj, perm, obj = check
                if not isinstance(subj, tuple) or len(subj) != 2:
                    raise ValueError(f"Check {i}: subject must be (type, id) tuple, got {subj}")
                if not isinstance(obj, tuple) or len(obj) != 2:
                    raise ValueError(f"Check {i}: object must be (type, id) tuple, got {obj}")

            # Perform batch check with Rust acceleration (manager guaranteed by _run_in_thread)
            assert self._rebac_manager is not None
            return self._rebac_manager.rebac_check_batch_fast(checks=checks)

        # Issue #702: Wrap batch check in a summary span
        import time as _time

        from nexus.services.permissions.rebac_tracing import (
            record_batch_result,
            start_batch_check_span,
        )

        batch_start = _time.perf_counter()
        with start_batch_check_span(batch_size=len(checks)):
            # Read operation — supports L1 cache fallback per-item (Decision 3A)
            try:
                batch_results = await self._run_in_thread(_check_batch_sync)
                batch_ms = (_time.perf_counter() - batch_start) * 1000
                allowed_count = sum(1 for r in batch_results if r)
                record_batch_result(
                    None,  # span set by context manager
                    allowed_count=allowed_count,
                    denied_count=len(batch_results) - allowed_count,
                    duration_ms=batch_ms,
                )
                return batch_results
            except CircuitOpenError:
                if self._rebac_manager:
                    results: list[bool] = []
                    all_cached = True
                    for check in checks:
                        subj, perm, obj = check
                        cached = self._rebac_manager.get_cached_permission(
                            subject=subj, permission=perm, object=obj
                        )
                        if cached is not None:
                            results.append(cached)
                        else:
                            all_cached = False
                            break
                    if all_cached:
                        logger.warning(
                            "[ReBACService] Circuit open — serving %d cached batch results",
                            len(results),
                        )
                        return results
                raise

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
            # Delete tuple - enhanced rebac_delete handles Tiger Cache invalidation
            # Manager guaranteed by _run_in_thread
            assert self._rebac_manager is not None
            result = self._rebac_manager.rebac_delete(tuple_id=tuple_id)

            if self._enable_audit_logging and result:
                logger.info("[ReBACService] Deleted tuple: %s", tuple_id)

            return result

        # Write operation — no cache fallback (Decision 3A)
        return await self._run_in_thread(_delete_sync)

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
            # Manager guaranteed by _run_in_thread
            assert self._rebac_manager is not None

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

        return await self._run_in_thread(_list_tuples_sync)

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
        raise NotImplementedError(
            "get_namespace() not yet implemented — see permissions/ modules for namespace operations"
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
            # NOTE: Use self._rebac_manager.rebac_check() directly (sync) instead of
            # self.rebac_check() (async). This method is called from sync contexts
            # (e.g., _create_sync in asyncio.to_thread), so we cannot await.
            # Using the async version without await would return a truthy coroutine,
            # silently bypassing the permission check.
            if not self._rebac_manager:
                raise RuntimeError(
                    "ReBAC manager is not available. Ensure ReBACService is properly initialized."
                )
            has_permission = self._rebac_manager.rebac_check(
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
                # Extract zone from resource path (format: /zone/{zone_id}/...)
                zone_id = None
                if resource_path.startswith("/zone/"):
                    parts = resource_path[6:].split("/", 1)  # Remove "/zone/" prefix
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

    # =========================================================================
    # Sync Public API: Called by NexusFS thin stubs (Issue #1519)
    # =========================================================================

    def _require_manager(self) -> Any:
        """Get the ReBAC manager, raising if not initialized."""
        mgr = self._rebac_manager
        if mgr is None:
            raise RuntimeError("ReBAC manager not available (record_store not configured)")
        return mgr

    def rebac_create_sync(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        zone_id: str | None = None,
        context: Any = None,
        column_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Synchronous rebac_create — full business logic."""
        mgr = self._require_manager()

        # Validate tuples
        if not isinstance(subject, tuple) or len(subject) not in (2, 3):
            raise ValueError(
                f"subject must be (type, id) or (type, id, relation) tuple, got {subject}"
            )
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        # Normalize trailing slashes on file paths
        if (
            object[0] == "file"
            and isinstance(object[1], str)
            and object[1].endswith("/")
            and object[1] != "/"
        ):
            object = (object[0], object[1].rstrip("/"))

        # Use zone_id from context if not explicitly provided
        effective_zone_id = zone_id
        if effective_zone_id is None and context:
            if isinstance(context, dict):
                effective_zone_id = context.get("zone")
            elif hasattr(context, "zone_id"):
                effective_zone_id = context.zone_id

        # SECURITY: Check execute permission before allowing permission management
        self._check_share_permission(resource=object, context=context)

        # Validate column_config for dynamic_viewer relation
        conditions = None
        if relation == "dynamic_viewer":
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

            if not isinstance(column_config, dict):
                raise ValueError("column_config must be a dictionary")

            hidden_columns = column_config.get("hidden_columns", [])
            aggregations = column_config.get("aggregations", {})
            visible_columns = column_config.get("visible_columns", [])

            if not isinstance(hidden_columns, list):
                raise ValueError("column_config.hidden_columns must be a list")
            if not isinstance(aggregations, dict):
                raise ValueError("column_config.aggregations must be a dictionary")
            if not isinstance(visible_columns, list):
                raise ValueError("column_config.visible_columns must be a list")

            # Validate columns against actual CSV file via callback
            file_path = object[1]
            if self._file_reader is not None:
                try:
                    raw = self._file_reader(file_path)
                    if raw is not None:
                        text_content: str = (
                            raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                        )
                        try:
                            import io

                            import pandas as pd

                            df = pd.read_csv(io.StringIO(text_content))
                            actual_columns = set(df.columns)
                            configured_columns = (
                                set(hidden_columns)
                                | set(aggregations.keys())
                                | set(visible_columns)
                            )
                            invalid_columns = configured_columns - actual_columns
                            if invalid_columns:
                                raise ValueError(
                                    f"Column config contains invalid columns: "
                                    f"{sorted(invalid_columns)}. "
                                    f"Available columns in CSV: {sorted(actual_columns)}"
                                )
                        except ValueError:
                            raise
                        except ImportError:
                            pass
                        except Exception as e:
                            logger.warning(
                                "Could not validate CSV columns for %s: %s. "
                                "Column config will be created without validation.",
                                file_path,
                                e,
                            )
                except ValueError:
                    raise
                except OSError as e:
                    logger.debug("Could not read file %s for column validation: %s", file_path, e)

            # Check that a column only appears in one category
            all_columns: set[str] = set()
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

            conditions = {"type": "dynamic_viewer", "column_config": column_config}
        elif column_config is not None:
            raise ValueError("column_config can only be provided when relation is 'dynamic_viewer'")

        result = mgr.rebac_write(
            subject=subject,
            relation=relation,
            object=object,
            expires_at=expires_at,
            zone_id=effective_zone_id,
            conditions=conditions,
        )

        if self._enable_audit_logging:
            logger.info(
                "[ReBACService] Created tuple: %s -[%s]-> %s (zone=%s, expires=%s)",
                subject,
                relation,
                object,
                effective_zone_id,
                expires_at,
            )

        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "consistency_token": result.consistency_token,
        }

    def _has_descendant_access_for_traverse(
        self,
        path: str,
        subject: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        """Check if user has READ access to any descendant of path."""
        from nexus.services.permissions.utils.zone import normalize_zone_id

        prefix = path if path.endswith("/") else path + "/"
        if path == "/":
            prefix = "/"

        try:
            mgr = self._require_manager()
            effective_zone = normalize_zone_id(zone_id)

            if hasattr(mgr, "_get_cached_zone_tuples"):
                tuples = mgr._get_cached_zone_tuples(effective_zone)
                if tuples is None:
                    tuples = mgr.get_zone_tuples(effective_zone)
            else:
                tuples = []

            for t in tuples:
                if t.get("subject_type") != subject[0] or t.get("subject_id") != subject[1]:
                    continue
                relation = t.get("relation", "")
                if relation not in (
                    "direct_viewer",
                    "direct_editor",
                    "direct_owner",
                    "viewer",
                    "editor",
                    "owner",
                ):
                    continue
                obj_type = t.get("object_type", "")
                obj_id = t.get("object_id", "")
                if obj_type == "file" and obj_id.startswith(prefix):
                    return True

            return False
        except (RuntimeError, ValueError):
            return False

    def rebac_check_sync(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: Any = None,
        zone_id: str | None = None,
    ) -> bool:
        """Synchronous rebac_check — full business logic with traverse fallback."""
        mgr = self._require_manager()

        if not isinstance(subject, tuple) or len(subject) != 2:
            raise ValueError(f"subject must be (type, id) tuple, got {subject}")
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        effective_zone_id = zone_id
        if effective_zone_id is None and context:
            if isinstance(context, dict):
                effective_zone_id = context.get("zone")
            elif hasattr(context, "zone_id"):
                effective_zone_id = context.zone_id

        result = mgr.rebac_check(
            subject=subject,
            permission=permission,
            object=object,
            context=context,
            zone_id=effective_zone_id,
        )

        # Unix-like TRAVERSE fallback
        if not result and permission == "traverse" and object[0] == "file":
            result = self._has_descendant_access_for_traverse(
                path=object[1],
                subject=subject,
                zone_id=effective_zone_id,
            )

        return bool(result)

    def rebac_expand_sync(
        self,
        permission: str,
        object: tuple[str, str],
    ) -> list[tuple[str, str]]:
        """Synchronous rebac_expand."""
        mgr = self._require_manager()
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")
        expanded: list[tuple[str, str]] = mgr.rebac_expand(permission=permission, object=object)
        return expanded

    def rebac_explain_sync(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Synchronous rebac_explain."""
        mgr = self._require_manager()
        if not isinstance(subject, tuple) or len(subject) != 2:
            raise ValueError(f"subject must be (type, id) tuple, got {subject}")
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        effective_zone_id = zone_id
        if effective_zone_id is None and context:
            if isinstance(context, dict):
                effective_zone_id = context.get("zone")
            elif hasattr(context, "zone_id"):
                effective_zone_id = context.zone_id

        result: dict[str, Any] = mgr.rebac_explain(
            subject=subject, permission=permission, object=object, zone_id=effective_zone_id
        )
        return result

    def rebac_check_batch_sync(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> list[bool]:
        """Synchronous rebac_check_batch."""
        mgr = self._require_manager()
        for i, check in enumerate(checks):
            if not isinstance(check, tuple) or len(check) != 3:
                raise ValueError(f"Check {i} must be (subject, permission, object) tuple")
            subj, _perm, obj = check
            if not isinstance(subj, tuple) or len(subj) != 2:
                raise ValueError(f"Check {i}: subject must be (type, id) tuple, got {subj}")
            if not isinstance(obj, tuple) or len(obj) != 2:
                raise ValueError(f"Check {i}: object must be (type, id) tuple, got {obj}")
        results: list[bool] = mgr.rebac_check_batch_fast(checks=checks)
        return results

    def rebac_delete_sync(self, tuple_id: str) -> bool:
        """Synchronous rebac_delete."""
        mgr = self._require_manager()
        result = mgr.rebac_delete(tuple_id=tuple_id)
        if self._enable_audit_logging and result:
            logger.info("[ReBACService] Deleted tuple: %s", tuple_id)
        return bool(result)

    def rebac_list_tuples_sync(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Synchronous rebac_list_tuples — raw SQL query."""
        mgr = self._require_manager()
        conn = mgr._get_connection()
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
                placeholders = ", ".join("?" * len(relation_in))
                query += f" AND relation IN ({placeholders})"
                params.extend(relation_in)

            if object:
                query += " AND object_type = ? AND object_id = ?"
                params.extend([object[0], object[1]])

            query = mgr._fix_sql_placeholders(query)
            cursor = mgr._create_cursor(conn)
            cursor.execute(query, params)

            results = []
            for row in cursor.fetchall():
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
            mgr._close_connection(conn)

    # =========================================================================
    # Sync: Namespace Management
    # =========================================================================

    def get_namespace_sync(self, object_type: str) -> dict[str, Any] | None:
        """Get namespace schema for an object type (sync)."""
        mgr = self._require_manager()
        ns = mgr.get_namespace(object_type)
        if ns is None:
            return None
        return {
            "namespace_id": ns.namespace_id,
            "object_type": ns.object_type,
            "config": ns.config,
            "created_at": ns.created_at.isoformat(),
            "updated_at": ns.updated_at.isoformat(),
        }

    def namespace_create_sync(self, object_type: str, config: dict[str, Any]) -> None:
        """Create or update a namespace configuration (sync)."""
        mgr = self._require_manager()
        if "relations" not in config or "permissions" not in config:
            raise ValueError("Namespace config must have 'relations' and 'permissions' keys")

        import uuid

        from nexus.core.rebac import NamespaceConfig

        ns = NamespaceConfig(
            namespace_id=str(uuid.uuid4()),
            object_type=object_type,
            config=config,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        mgr.create_namespace(ns)

    def namespace_list_sync(self) -> list[dict[str, Any]]:
        """List all registered namespace configurations (sync)."""
        import json as _json

        mgr = self._require_manager()
        conn = mgr._get_connection()
        try:
            cursor = mgr._create_cursor(conn)
            cursor.execute(
                mgr._fix_sql_placeholders(
                    "SELECT namespace_id, object_type, config, created_at, updated_at "
                    "FROM rebac_namespaces ORDER BY object_type"
                )
            )
            namespaces = []
            for row in cursor.fetchall():
                namespaces.append(
                    {
                        "namespace_id": row["namespace_id"],
                        "object_type": row["object_type"],
                        "config": _json.loads(row["config"]),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                )
            return namespaces
        finally:
            mgr._close_connection(conn)

    def namespace_delete_sync(self, object_type: str) -> bool:
        """Delete a namespace configuration (sync)."""
        mgr = self._require_manager()
        conn = mgr._get_connection()
        try:
            cursor = mgr._create_cursor(conn)
            cursor.execute(
                mgr._fix_sql_placeholders(
                    "SELECT namespace_id FROM rebac_namespaces WHERE object_type = ?"
                ),
                (object_type,),
            )
            if cursor.fetchone() is None:
                return False
            cursor.execute(
                mgr._fix_sql_placeholders("DELETE FROM rebac_namespaces WHERE object_type = ?"),
                (object_type,),
            )
            conn.commit()
            cache = getattr(mgr, "_cache", None)
            if cache is not None:
                cache.clear()
            return True
        finally:
            mgr._close_connection(conn)

    # =========================================================================
    # Sync: Privacy & Consent
    # =========================================================================

    def rebac_expand_with_privacy_sync(
        self,
        permission: str,
        object: tuple[str, str],
        respect_consent: bool = True,
        requester: tuple[str, str] | None = None,
    ) -> list[tuple[str, str]]:
        """Expand permissions with privacy filtering (sync)."""
        all_subjects = self.rebac_expand_sync(permission, object)
        if not respect_consent or not requester:
            return all_subjects

        mgr = self._require_manager()
        filtered = []
        for subj in all_subjects:
            can_discover = mgr.rebac_check(subject=requester, permission="discover", object=subj)
            if can_discover:
                filtered.append(subj)
        return filtered

    def grant_consent_sync(
        self,
        from_subject: tuple[str, str],
        to_subject: tuple[str, str],
        expires_at: datetime | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Grant consent for discovery (sync)."""
        return self.rebac_create_sync(
            subject=to_subject,
            relation="consent_granted",
            object=from_subject,
            expires_at=expires_at,
            zone_id=zone_id,
        )

    def revoke_consent_sync(
        self, from_subject: tuple[str, str], to_subject: tuple[str, str]
    ) -> bool:
        """Revoke previously granted consent (sync)."""
        tuples = self.rebac_list_tuples_sync(
            subject=to_subject, relation="consent_granted", object=from_subject
        )
        if tuples:
            return self.rebac_delete_sync(tuples[0]["tuple_id"])
        return False

    def make_public_sync(
        self, resource: tuple[str, str], zone_id: str | None = None
    ) -> dict[str, Any]:
        """Make a resource publicly discoverable (sync)."""
        return self.rebac_create_sync(
            subject=("*", "*"),
            relation="public_discoverable",
            object=resource,
            zone_id=zone_id,
        )

    def make_private_sync(self, resource: tuple[str, str]) -> bool:
        """Remove public discoverability from a resource (sync)."""
        tuples = self.rebac_list_tuples_sync(
            subject=("*", "*"), relation="public_discoverable", object=resource
        )
        if tuples:
            return self.rebac_delete_sync(tuples[0]["tuple_id"])
        return False

    # =========================================================================
    # Sync: Cross-Zone Sharing
    # =========================================================================

    def share_with_user_sync(
        self,
        resource: tuple[str, str],
        user_id: str,
        relation: str = "viewer",
        zone_id: str | None = None,
        user_zone_id: str | None = None,
        expires_at: datetime | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Share a resource with a specific user (sync)."""
        mgr = self._require_manager()

        self._check_share_permission(resource=resource, context=context)

        relation_map = {
            "viewer": "shared-viewer",
            "editor": "shared-editor",
            "owner": "shared-owner",
        }
        if relation not in relation_map:
            raise ValueError(f"relation must be 'viewer', 'editor', or 'owner', got '{relation}'")
        tuple_relation = relation_map[relation]

        expires_dt = None
        if expires_at is not None:
            if isinstance(expires_at, str):
                from datetime import datetime as dt

                expires_dt = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                expires_dt = expires_at

        result = mgr.rebac_write(
            subject=("user", user_id),
            relation=tuple_relation,
            object=resource,
            zone_id=zone_id,
            subject_zone_id=user_zone_id,
            expires_at=expires_dt,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "consistency_token": result.consistency_token,
        }

    def share_with_group_sync(
        self,
        resource: tuple[str, str],
        group_id: str,
        relation: str = "viewer",
        zone_id: str | None = None,
        group_zone_id: str | None = None,
        expires_at: datetime | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Share a resource with a group (sync)."""
        mgr = self._require_manager()

        self._check_share_permission(resource=resource, context=context)

        relation_map = {
            "viewer": "shared-viewer",
            "editor": "shared-editor",
            "owner": "shared-owner",
        }
        if relation not in relation_map:
            raise ValueError(f"relation must be 'viewer', 'editor', or 'owner', got '{relation}'")
        tuple_relation = relation_map[relation]

        expires_dt = None
        if expires_at is not None:
            if isinstance(expires_at, str):
                from datetime import datetime as dt

                expires_dt = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                expires_dt = expires_at

        result = mgr.rebac_write(
            subject=("group", group_id, "member"),
            relation=tuple_relation,
            object=resource,
            zone_id=zone_id,
            subject_zone_id=group_zone_id,
            expires_at=expires_dt,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "consistency_token": result.consistency_token,
        }

    def revoke_share_sync(
        self,
        resource: tuple[str, str],
        user_id: str,
    ) -> bool:
        """Revoke a share for a specific user on a resource (sync)."""
        tuples = self.rebac_list_tuples_sync(
            subject=("user", user_id),
            relation_in=["shared-viewer", "shared-editor", "shared-owner"],
            object=resource,
        )
        if tuples:
            return self.rebac_delete_sync(tuples[0]["tuple_id"])
        return False

    def revoke_share_by_id_sync(self, share_id: str) -> bool:
        """Revoke a share using its ID (sync)."""
        return self.rebac_delete_sync(share_id)

    def list_outgoing_shares_sync(
        self,
        resource: tuple[str, str] | None = None,
        zone_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        cursor: str | None = None,
        current_zone: str = "default",
    ) -> dict[str, Any]:
        """List outgoing shares with iterator caching (sync)."""
        mgr = self._require_manager()
        if zone_id is not None:
            current_zone = zone_id

        from nexus.services.permissions.rebac_iterator_cache import CursorExpiredError

        relation_to_level = {
            "shared-viewer": "viewer",
            "shared-editor": "editor",
            "shared-owner": "owner",
        }

        def _transform(tuples: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "share_id": t.get("tuple_id"),
                    "resource_type": t.get("object_type"),
                    "resource_id": t.get("object_id"),
                    "recipient_id": t.get("subject_id"),
                    "permission_level": relation_to_level.get(t.get("relation") or "", "viewer"),
                    "created_at": t.get("created_at"),
                    "expires_at": t.get("expires_at"),
                }
                for t in tuples
            ]

        def _compute() -> list[dict[str, Any]]:
            all_tuples = self.rebac_list_tuples_sync(
                relation_in=["shared-viewer", "shared-editor", "shared-owner"],
                object=resource,
            )
            return _transform(all_tuples)

        if cursor:
            try:
                items, next_cursor, total = mgr._iterator_cache.get_page(
                    cursor_id=cursor,
                    offset=offset,
                    limit=limit,
                )
                return {
                    "items": items,
                    "next_cursor": next_cursor,
                    "total_count": total,
                    "has_more": next_cursor is not None,
                }
            except CursorExpiredError:
                pass

        resource_str = f"{resource[0]}:{resource[1]}" if resource else "all"
        query_hash = f"outgoing:{current_zone}:{resource_str}"

        cursor_id, all_results, total = mgr._iterator_cache.get_or_create(
            query_hash=query_hash,
            zone_id=current_zone,
            compute_fn=_compute,
        )

        items = all_results[offset : offset + limit]
        has_more = offset + limit < total
        next_cursor_val = cursor_id if has_more else None

        return {
            "items": items,
            "next_cursor": next_cursor_val,
            "total_count": total,
            "has_more": has_more,
        }

    def list_incoming_shares_sync(
        self,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
        cursor: str | None = None,
        current_zone: str = "default",
    ) -> dict[str, Any]:
        """List incoming shares with iterator caching (sync)."""
        mgr = self._require_manager()

        from nexus.services.permissions.rebac_iterator_cache import CursorExpiredError

        relation_to_level = {
            "shared-viewer": "viewer",
            "shared-editor": "editor",
            "shared-owner": "owner",
        }

        def _transform(tuples: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "share_id": t.get("tuple_id"),
                    "resource_type": t.get("object_type"),
                    "resource_id": t.get("object_id"),
                    "owner_zone_id": t.get("zone_id"),
                    "permission_level": relation_to_level.get(t.get("relation") or "", "viewer"),
                    "created_at": t.get("created_at"),
                    "expires_at": t.get("expires_at"),
                }
                for t in tuples
            ]

        def _compute() -> list[dict[str, Any]]:
            all_tuples = self.rebac_list_tuples_sync(
                subject=("user", user_id),
                relation_in=["shared-viewer", "shared-editor", "shared-owner"],
            )
            return _transform(all_tuples)

        if cursor:
            try:
                items, next_cursor, total = mgr._iterator_cache.get_page(
                    cursor_id=cursor,
                    offset=offset,
                    limit=limit,
                )
                return {
                    "items": items,
                    "next_cursor": next_cursor,
                    "total_count": total,
                    "has_more": next_cursor is not None,
                }
            except CursorExpiredError:
                pass

        query_hash = f"incoming:{current_zone}:{user_id}"

        cursor_id, all_results, total = mgr._iterator_cache.get_or_create(
            query_hash=query_hash,
            zone_id=current_zone,
            compute_fn=_compute,
        )

        items = all_results[offset : offset + limit]
        has_more = offset + limit < total
        next_cursor_val = cursor_id if has_more else None

        return {
            "items": items,
            "next_cursor": next_cursor_val,
            "total_count": total,
            "has_more": has_more,
        }

    # =========================================================================
    # Sync: Dynamic Viewer
    # =========================================================================

    def get_dynamic_viewer_config_sync(
        self,
        subject: tuple[str, str],
        file_path: str,
    ) -> dict[str, Any] | None:
        """Get dynamic_viewer configuration for a subject and file (sync)."""
        import json as _json

        mgr = self._require_manager()

        tuples = self.rebac_list_tuples_sync(
            subject=subject, relation="dynamic_viewer", object=("file", file_path)
        )
        if not tuples:
            return None

        tuple_data = tuples[0]
        conn = mgr._get_connection()
        try:
            cursor = mgr._create_cursor(conn)
            cursor.execute(
                mgr._fix_sql_placeholders("SELECT conditions FROM rebac_tuples WHERE tuple_id = ?"),
                (tuple_data["tuple_id"],),
            )
            row = cursor.fetchone()
            if row and row["conditions"]:
                conditions = _json.loads(row["conditions"])
                if conditions.get("type") == "dynamic_viewer":
                    col_cfg: dict[str, Any] | None = conditions.get("column_config")
                    return col_cfg
        finally:
            mgr._close_connection(conn)
        return None

    def apply_dynamic_viewer_filter_sync(
        self,
        data: str,
        column_config: dict[str, Any],
        file_format: str = "csv",
    ) -> dict[str, Any]:
        """Apply column-level filtering and aggregations to CSV data (sync)."""
        if file_format != "csv":
            raise ValueError(f"Unsupported file format: {file_format}. Only 'csv' is supported.")

        try:
            import io

            import pandas as pd
        except ImportError as e:
            raise RuntimeError(
                "pandas is required for dynamic viewer filtering. Install with: pip install pandas"
            ) from e

        try:
            df = pd.read_csv(io.StringIO(data))
        except (ValueError, pd.errors.ParserError) as e:
            raise RuntimeError(f"Failed to parse CSV data: {e}") from e

        hidden_columns = column_config.get("hidden_columns", [])
        aggregations = column_config.get("aggregations", {})
        visible_columns = column_config.get("visible_columns", [])

        if not visible_columns:
            all_cols = set(df.columns)
            hidden_set = set(hidden_columns)
            agg_set = set(aggregations.keys())
            visible_columns = list(all_cols - hidden_set - agg_set)

        result_columns: list[tuple[str, Any]] = []
        aggregation_results: dict[str, dict[str, float | int | str]] = {}
        aggregated_column_names: list[str] = []
        columns_shown: list[str] = []

        for col in df.columns:
            if col in hidden_columns:
                continue
            elif col in aggregations:
                operation = aggregations[col]
                try:
                    if operation == "mean":
                        agg_value = float(df[col].mean())
                    elif operation == "sum":
                        agg_value = float(df[col].sum())
                    elif operation == "count":
                        agg_value = int(df[col].count())
                    elif operation == "min":
                        agg_value = float(df[col].min())
                    elif operation == "max":
                        agg_value = float(df[col].max())
                    elif operation == "std":
                        agg_value = float(df[col].std())
                    elif operation == "median":
                        agg_value = float(df[col].median())
                    else:
                        continue

                    if col not in aggregation_results:
                        aggregation_results[col] = {}
                    aggregation_results[col][operation] = agg_value

                    agg_col_name = f"{operation}({col})"
                    aggregated_column_names.append(agg_col_name)
                    agg_series = pd.Series([agg_value] * len(df), name=agg_col_name)
                    result_columns.append((agg_col_name, agg_series))
                except (ValueError, TypeError, KeyError) as e:
                    if col not in aggregation_results:
                        aggregation_results[col] = {}
                    aggregation_results[col][operation] = f"error: {str(e)}"
            elif col in visible_columns:
                result_columns.append((col, df[col]))
                columns_shown.append(col)

        result_df = pd.DataFrame(dict(result_columns)) if result_columns else pd.DataFrame()
        filtered_data = result_df.to_csv(index=False)

        return {
            "filtered_data": filtered_data,
            "aggregations": aggregation_results,
            "columns_shown": columns_shown,
            "aggregated_columns": aggregated_column_names,
        }

    # =========================================================================
    # Sync: Tiger Cache & Traverse
    # =========================================================================

    def grant_traverse_on_implicit_dirs_sync(
        self,
        zone_id: str | None = None,
        subject: tuple[str, str] | None = None,
    ) -> list[Any]:
        """Grant TRAVERSE permission on root-level implicit directories (sync)."""
        from sqlalchemy.exc import OperationalError

        from nexus.services.permissions.utils.zone import normalize_zone_id

        mgr = self._require_manager()
        if subject is None:
            subject = ("group", "authenticated")
        effective_zone_id = normalize_zone_id(zone_id)

        implicit_dirs = [
            "/",
            "/zones",
            "/sessions",
            "/skills",
            "/workspace",
            "/shared",
            "/system",
            "/archives",
            "/external",
        ]

        tuple_ids = []
        for dir_path in implicit_dirs:
            try:
                existing = self.rebac_list_tuples_sync(
                    subject=subject,
                    relation="traverser-of",
                    object=("file", dir_path),
                )
                if existing:
                    continue
                tuple_id = mgr.rebac_write(
                    subject=subject,
                    relation="traverser-of",
                    object=("file", dir_path),
                    zone_id=effective_zone_id,
                )
                tuple_ids.append(tuple_id)
            except (RuntimeError, ValueError, OperationalError) as e:
                logger.warning("Failed to grant TRAVERSE on %s: %s", dir_path, e)
        return tuple_ids

    def process_tiger_cache_queue_sync(self, batch_size: int = 100) -> int:
        """Process pending Tiger Cache update queue (sync)."""
        if not self._rebac_manager:
            return 0
        mgr = self._require_manager()
        if hasattr(mgr, "tiger_process_queue"):
            count: int = mgr.tiger_process_queue(batch_size=batch_size)
            return count
        return 0

    def warm_tiger_cache_sync(
        self,
        subjects: list[tuple[str, str]] | None = None,
        zone_id: str | None = None,
    ) -> int:
        """Warm the Tiger Cache by pre-computing permissions for subjects (sync)."""
        from sqlalchemy.exc import OperationalError

        from nexus.services.permissions.utils.zone import normalize_zone_id

        if not self._rebac_manager:
            return 0

        mgr = self._require_manager()
        effective_zone_id = normalize_zone_id(zone_id)
        entries_created = 0

        if subjects is None:
            try:
                tuples = self.rebac_list_tuples_sync()
                subjects_set: set[tuple[str, str]] = set()
                for t in tuples:
                    subject_type = t.get("subject_type")
                    subject_id = t.get("subject_id")
                    if subject_type and subject_id:
                        subjects_set.add((subject_type, subject_id))
                subjects = list(subjects_set)
            except (KeyError, TypeError, AttributeError):
                subjects = []

        for subj in subjects:
            if hasattr(mgr, "tiger_queue_update"):
                for permission in ["read", "write", "traverse"]:
                    mgr.tiger_queue_update(
                        subject=subj,
                        permission=permission,
                        resource_type="file",
                        zone_id=effective_zone_id,
                    )
                    entries_created += 1

        if hasattr(mgr, "tiger_process_queue"):
            try:
                mgr.tiger_process_queue(batch_size=5)
            except (RuntimeError, OperationalError) as e:
                logger.warning("[WARM-TIGER] Queue processing failed: %s", e)

        return entries_created
