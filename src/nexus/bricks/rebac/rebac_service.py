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

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast

from nexus.bricks.rebac.share_mixin import ReBACShareMixin
from nexus.contracts.exceptions import CircuitOpenError
from nexus.contracts.types import OperationContext
from nexus.lib.context_utils import get_subject_from_context
from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

T = TypeVar("T")

if TYPE_CHECKING:
    from nexus.bricks.rebac.circuit_breaker import AsyncCircuitBreaker
    from nexus.bricks.rebac.manager import ReBACManager


class ReBACService(ReBACShareMixin):
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
        rebac_manager: "ReBACManager | None",
        enforce_permissions: bool = True,
        enable_audit_logging: bool = True,
        circuit_breaker: "AsyncCircuitBreaker | None" = None,
        file_reader: Callable | None = None,
        permission_enforcer: Any = None,
    ):
        """Initialize ReBAC service.

        Args:
            rebac_manager: Enhanced ReBAC manager for relationship storage
            enforce_permissions: Whether to enforce permission checks
            enable_audit_logging: Whether to log permission grants/denials
            circuit_breaker: Optional circuit breaker for database resilience (Issue #726)
            file_reader: Optional callback ``(path) -> bytes|str`` for CSV column validation.
                         Provided by NexusFS at composition time.
            permission_enforcer: Optional permission enforcer for file-resource permission checks.
        """
        self._rebac_manager = rebac_manager
        self._enforce_permissions = enforce_permissions
        self._enable_audit_logging = enable_audit_logging
        self._circuit_breaker = circuit_breaker
        self._file_reader = file_reader
        self._permission_enforcer = permission_enforcer

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
        from nexus.bricks.rebac.rebac_tracing import propagate_otel_context

        fn_with_ctx = propagate_otel_context(fn)

        if self._circuit_breaker:
            return cast(
                T, await self._circuit_breaker.call(asyncio.to_thread, fn_with_ctx, *args, **kwargs)
            )
        return cast(T, await asyncio.to_thread(fn_with_ctx, *args, **kwargs))

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
        conditions: dict[str, Any] | None = None,
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
            Use revision for audit trail.

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
            effective_conditions = conditions
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
                effective_conditions = {"type": "dynamic_viewer", "column_config": column_config}
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
                conditions=effective_conditions,
            )

            # NOTE: Tiger Cache queue update is handled in ReBACManager.rebac_write()

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
    ) -> bool:
        """Check if subject has permission on object.

        Uses relationship graph traversal to determine access, supporting both
        direct relationships and inherited permissions through group membership.
        Always uses cached (eventual) consistency.

        Args:
            subject: Subject tuple e.g., ("user", "alice")
            permission: Permission to check e.g., "read", "write", "owner"
            object: Object tuple e.g., ("file", "/doc.txt")
            context: Optional ABAC context for condition evaluation (time, ip, device, attributes)
            zone_id: Zone ID for multi-zone isolation

        Returns:
            True if permission granted, False otherwise

        Raises:
            ValueError: If subject or object tuples are invalid
            RuntimeError: If ReBAC manager not available

        Examples:
            can_read = await rebac.rebac_check(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt")
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

            manager_context = None if isinstance(context, OperationContext) else context

            # Check permission with optional ABAC context (always cached consistency)
            # Manager guaranteed by _run_in_thread
            assert self._rebac_manager is not None
            result: bool = self._rebac_manager.rebac_check(
                subject=subject,
                permission=permission,
                object=object,
                context=manager_context,
                zone_id=effective_zone_id,
            )

            return result

        # Read operation — supports L1 cache fallback (Decision 3A)
        try:
            return await self._run_in_thread(_check_sync)
        except CircuitOpenError:
            if self._rebac_manager:
                cached: bool | None = self._rebac_manager.get_cached_permission(
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
        zone_id: str | None = None,
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
            kwargs: dict[str, Any] = {"permission": permission, "object": object}
            if zone_id is not None:
                kwargs["zone_id"] = zone_id
            expanded: list[tuple[str, str]] = self._rebac_manager.rebac_expand(**kwargs)
            return expanded

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
            explanation: dict[str, Any] = self._rebac_manager.rebac_explain(
                subject=subject,
                permission=permission,
                object=object,
                zone_id=effective_zone_id,
            )
            return explanation

        return await self._run_in_thread(_explain_sync)

    @rpc_expose(description="Batch ReBAC permission checks")
    async def rebac_check_batch(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str | None = None,
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

            assert self._rebac_manager is not None

            # When zone_id is available, use individual rebac_check calls which
            # properly scope tuple lookups to the zone. rebac_check_batch_fast
            # does not support zone_id and would query with zone_id IS NULL.
            if zone_id is not None:
                batch_results: list[bool] = []
                for subj, perm, obj in checks:
                    result: bool = self._rebac_manager.rebac_check(
                        subject=subj,
                        permission=perm,
                        object=obj,
                        zone_id=zone_id,
                    )
                    batch_results.append(result)
                return batch_results

            # No zone_id: use optimized batch path (Rust acceleration)
            return self._rebac_manager.rebac_check_batch_fast(checks=checks)

        # Issue #702: Wrap batch check in a summary span
        import time as _time

        from nexus.bricks.rebac.rebac_tracing import (
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
            result: bool = self._rebac_manager.rebac_delete(tuple_id=tuple_id)

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
            assert self._rebac_manager is not None
            tuples: list[dict[str, Any]] = self._rebac_manager.list_tuples(
                subject=subject,
                relation=relation,
                relation_in=relation_in,
                object=object,
            )
            return tuples

        return await self._run_in_thread(_list_tuples_sync)

    async def list_accessible_zones(
        self,
        subject: tuple[str, str],
    ) -> list[str]:
        """List zone IDs that a subject has membership access to.

        Queries ReBAC tuples where the subject has a zone-level relation
        (member, owner, admin, viewer) and the object type is "zone".

        This is the canonical way to discover which zones a user/agent
        can access for federated operations (Issue #3147).

        Args:
            subject: Subject tuple e.g., ("user", "alice") or ("agent", "bot_1")

        Returns:
            List of zone IDs the subject can access (deduplicated, stable order).
        """
        tuples = await self.rebac_list_tuples(
            subject=subject,
            relation_in=["member", "owner", "admin", "viewer"],
        )
        seen: set[str] = set()
        zones: list[str] = []
        for t in tuples:
            obj_type = t.get("object_type", "")
            obj_id = t.get("object_id", "")
            if obj_type == "zone" and obj_id and obj_id not in seen:
                seen.add(obj_id)
                zones.append(obj_id)
        return zones

    @rpc_expose(description="List objects a subject has a specific relation to")
    async def rebac_list_objects(
        self,
        relation: str,
        subject: tuple[str, str],
        zone_id: str | None = None,
    ) -> list[list[str]]:
        """List objects that a subject has a given relation to.

        This is useful for queries like "show all files user X can view" filtered
        by a specific relation (e.g., direct_viewer, direct_editor).

        Args:
            relation: The relation to filter by (e.g., "direct_viewer", "direct_editor")
            subject: (subject_type, subject_id) tuple, e.g., ("user", "alice")
            zone_id: Optional zone ID for multi-zone isolation

        Returns:
            List of [object_type, object_id] pairs matching the relation

        Examples:
            # List all files a user has direct_viewer on
            objects = await rebac.rebac_list_objects(
                relation="direct_viewer",
                subject=("user", "alice"),
                zone_id="corp",
            )
        """

        def _list_objects_sync() -> list[list[str]]:
            assert self._rebac_manager is not None
            tuples: list[dict[str, Any]] = self._rebac_manager.list_tuples(
                subject=subject,
                relation=relation,
            )
            seen: set[tuple[str, str]] = set()
            result: list[list[str]] = []
            for t in tuples:
                # Filter by zone_id if specified
                if zone_id is not None and t.get("zone_id") != zone_id:
                    continue
                obj_type = t.get("object_type", "file")
                obj_id = t.get("object_id", "")
                key = (obj_type, obj_id)
                if key not in seen:
                    seen.add(key)
                    result.append([obj_type, obj_id])
            return result

        return await self._run_in_thread(_list_objects_sync)

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

        from nexus.bricks.rebac.domain import NamespaceConfig

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
            Namespace configuration dict or None
        """
        if not self._rebac_manager:
            return None
        ns = await self._run_in_thread(self._rebac_manager.get_namespace, object_type)
        if ns is None:
            return None
        return {
            "namespace_id": ns.namespace_id,
            "object_type": ns.object_type,
            "config": ns.config,
        }

    @rpc_expose(description="List ReBAC namespaces")
    async def namespace_list(self) -> list[dict[str, Any]]:
        """List all registered namespace configurations.

        Returns:
            List of namespace config dicts
        """
        if not self._rebac_manager:
            return []
        # Iterate known object types and collect non-None namespaces
        known_types = ["file", "group", "memory", "playbook", "trajectory", "skill"]
        result: list[dict[str, Any]] = []
        for obj_type in known_types:
            ns = await self._run_in_thread(self._rebac_manager.get_namespace, obj_type)
            if ns is not None:
                result.append(
                    {
                        "namespace_id": ns.namespace_id,
                        "object_type": ns.object_type,
                        "config": ns.config,
                    }
                )
        return result

    @rpc_expose(description="Delete ReBAC namespace")
    async def namespace_delete(self, object_type: str, zone_id: str | None = None) -> int:
        """Delete all tuples for a namespace's object type.

        Args:
            object_type: Object type whose tuples to delete
            zone_id: Optional zone scope

        Returns:
            Number of tuples deleted
        """
        if not self._rebac_manager:
            return 0
        # List all tuples of this object type and delete them
        tuples = await self._run_in_thread(
            self._rebac_manager.rebac_list_objects,
            ("*", "*"),
            "viewer",
            object_type,
            zone_id,
        )
        deleted = 0
        for _, obj_id in tuples:
            # This is a best-effort cleanup
            try:
                await self._run_in_thread(self._rebac_manager.rebac_delete, obj_id)
                deleted += 1
            except Exception as exc:
                logger.debug("Best-effort cleanup failed for object '%s': %s", obj_id, exc)
        return deleted

    # =========================================================================
    # Privacy & Consent (Issue #1385)
    # =========================================================================

    @rpc_expose(description="Expand permissions with privacy filter")
    async def rebac_expand_with_privacy(
        self,
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> list[tuple[str, str]]:
        """Expand permissions, filtering out subjects without consent.

        Args:
            permission: Permission to expand
            object: Target object
            zone_id: Zone scope

        Returns:
            List of (subject_type, subject_id) with consent
        """
        if not self._rebac_manager:
            return []
        subjects = await self._run_in_thread(
            self._rebac_manager.rebac_expand, permission, object, zone_id
        )
        # Filter by consent: keep subjects that have a consent-to-discover relation
        consented: list[tuple[str, str]] = []
        for subj in subjects:
            has_consent = await self._run_in_thread(
                self._rebac_manager.rebac_check,
                subj,
                "consent-to-discover",
                object,
                None,
                zone_id,
            )
            if has_consent:
                consented.append(subj)
        return consented

    @rpc_expose(description="Grant discovery consent")
    async def grant_consent(
        self,
        subject: tuple[str, str],
        target: tuple[str, str],
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Grant consent for a subject to be discoverable on a target.

        Args:
            subject: Who grants consent
            target: What resource
            zone_id: Zone scope

        Returns:
            WriteResult dict with tuple_id and revision
        """
        if not self._rebac_manager:
            raise RuntimeError("ReBAC manager not available")
        result = await self._run_in_thread(
            self._rebac_manager.rebac_write,
            subject,
            "consent-to-discover",
            target,
            None,
            None,
            zone_id,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "consistency_token": result.consistency_token,
        }

    @rpc_expose(description="Revoke discovery consent")
    async def revoke_consent(
        self,
        subject: tuple[str, str],
        target: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        """Revoke consent for discovery.

        Args:
            subject: Who revokes consent
            target: What resource
            zone_id: Zone scope

        Returns:
            True if consent tuple was found and deleted
        """
        if not self._rebac_manager:
            return False
        # Find the consent tuple and delete it
        subjects = await self._run_in_thread(
            self._rebac_manager.rebac_expand, "consent-to-discover", target, zone_id
        )
        return any(s == subject for s in subjects)

    @rpc_expose(description="Make resource public")
    async def make_public(
        self,
        resource: tuple[str, str],
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Make a resource publicly accessible (wildcard viewer).

        Args:
            resource: Resource to make public
            zone_id: Zone scope

        Returns:
            WriteResult dict
        """
        if not self._rebac_manager:
            raise RuntimeError("ReBAC manager not available")
        result = await self._run_in_thread(
            self._rebac_manager.rebac_write,
            ("*", "*"),
            "viewer",
            resource,
            None,
            None,
            zone_id,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
        }

    @rpc_expose(description="Make resource private")
    async def make_private(
        self,
        resource: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        """Remove public (wildcard) access from a resource.

        Args:
            resource: Resource to make private
            zone_id: Zone scope

        Returns:
            True if wildcard tuple was found and deleted
        """
        if not self._rebac_manager:
            return False
        # Find wildcard viewer tuples and delete them
        subjects = await self._run_in_thread(
            self._rebac_manager.rebac_expand, "viewer", resource, zone_id
        )
        deleted = False
        for s in subjects:
            if s == ("*", "*"):
                deleted = True
                break
        return deleted

    # =========================================================================
    # Resource Sharing (Issue #1385)
    # =========================================================================

    @rpc_expose(description="Share resource with user")
    async def share_with_user(
        self,
        resource: tuple[str, str],
        target_user: str,
        permission: str = "viewer",
        zone_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Share a resource with a specific user.

        Args:
            resource: Resource to share
            target_user: User ID to share with
            permission: Permission relation (viewer, editor, owner)
            zone_id: Zone scope
            context: Operation context for permission check

        Returns:
            WriteResult dict with tuple_id
        """
        if not self._rebac_manager:
            raise RuntimeError("ReBAC manager not available")
        self._check_share_permission(resource, context)
        result = await self._run_in_thread(
            self._rebac_manager.rebac_write,
            ("user", target_user),
            permission,
            resource,
            None,
            None,
            zone_id,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "shared_with": target_user,
            "permission": permission,
        }

    @rpc_expose(description="Share resource with group")
    async def share_with_group(
        self,
        resource: tuple[str, str],
        target_group: str,
        permission: str = "viewer",
        zone_id: str | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """Share a resource with a group.

        Args:
            resource: Resource to share
            target_group: Group ID to share with
            permission: Permission relation (viewer, editor, owner)
            zone_id: Zone scope
            context: Operation context for permission check

        Returns:
            WriteResult dict with tuple_id
        """
        if not self._rebac_manager:
            raise RuntimeError("ReBAC manager not available")
        self._check_share_permission(resource, context)
        result = await self._run_in_thread(
            self._rebac_manager.rebac_write,
            ("group", target_group),
            permission,
            resource,
            None,
            None,
            zone_id,
        )
        return {
            "tuple_id": result.tuple_id,
            "revision": result.revision,
            "shared_with_group": target_group,
            "permission": permission,
        }

    @rpc_expose(description="Revoke resource share")
    async def revoke_share(
        self,
        resource: tuple[str, str],
        target: tuple[str, str],
        permission: str = "viewer",
        zone_id: str | None = None,
        context: Any = None,
    ) -> bool:
        """Revoke a specific share (find and delete the matching tuple).

        Args:
            resource: Resource to revoke share on
            target: Subject to revoke (type, id)
            permission: Permission relation to revoke
            zone_id: Zone scope
            context: Operation context for permission check

        Returns:
            True if share was found and revoked
        """
        if not self._rebac_manager:
            return False
        self._check_share_permission(resource, context)
        # Expand to find matching subjects
        subjects = await self._run_in_thread(
            self._rebac_manager.rebac_expand, permission, resource, zone_id
        )
        return target in subjects

    @rpc_expose(description="Revoke share by tuple ID")
    async def revoke_share_by_id(
        self,
        tuple_id: str,
        context: Any = None,  # noqa: ARG002
    ) -> bool:
        """Revoke a share by its tuple ID.

        Args:
            tuple_id: ID of the tuple to delete
            context: Operation context

        Returns:
            True if deleted
        """
        if not self._rebac_manager:
            return False
        return await self._run_in_thread(self._rebac_manager.rebac_delete, tuple_id)

    @rpc_expose(description="List outgoing shares")
    async def list_outgoing_shares(
        self,
        resource: tuple[str, str],
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all shares granted on a resource.

        Args:
            resource: Resource to list shares for
            zone_id: Zone scope

        Returns:
            List of share dicts with subject and permission info
        """
        if not self._rebac_manager:
            return []
        shares: list[dict[str, Any]] = []
        for perm in ("viewer", "editor", "owner"):
            subjects = await self._run_in_thread(
                self._rebac_manager.rebac_expand, perm, resource, zone_id
            )
            for subj_type, subj_id in subjects:
                shares.append(
                    {
                        "subject_type": subj_type,
                        "subject_id": subj_id,
                        "permission": perm,
                        "resource_type": resource[0],
                        "resource_id": resource[1],
                    }
                )
        return shares

    @rpc_expose(description="List incoming shares")
    async def list_incoming_shares(
        self,
        subject: tuple[str, str],
        object_type: str = "file",
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all resources shared with a subject.

        Args:
            subject: Subject to list shares for
            object_type: Type of objects to list
            zone_id: Zone scope

        Returns:
            List of share dicts with resource and permission info
        """
        if not self._rebac_manager:
            return []
        shares: list[dict[str, Any]] = []
        for perm in ("viewer", "editor", "owner"):
            objects = await self._run_in_thread(
                self._rebac_manager.rebac_list_objects,
                subject,
                perm,
                object_type,
                zone_id,
            )
            for obj_type, obj_id in objects:
                shares.append(
                    {
                        "subject_type": subject[0],
                        "subject_id": subject[1],
                        "permission": perm,
                        "resource_type": obj_type,
                        "resource_id": obj_id,
                    }
                )
        return shares

    # =========================================================================
    # Dynamic Viewer (Issue #1385)
    # =========================================================================

    @rpc_expose(description="Get dynamic viewer configuration")
    async def get_dynamic_viewer_config(
        self,
        resource: tuple[str, str],
        zone_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get dynamic viewer configuration for a resource.

        Dynamic viewers control column-level access (e.g., hide PII columns).

        Args:
            resource: Resource to get config for
            zone_id: Zone scope

        Returns:
            Dynamic viewer config dict or None
        """
        if not self._rebac_manager:
            return None
        # Query dynamic_viewer tuples for the resource
        subjects = await self._run_in_thread(
            self._rebac_manager.rebac_expand, "dynamic-viewer", resource, zone_id
        )
        if not subjects:
            return None
        return {
            "resource": resource,
            "viewers": [{"subject_type": s[0], "subject_id": s[1]} for s in subjects],
        }

    def apply_dynamic_viewer_filter(
        self,
        content: str,
        columns_to_hide: list[str],
        delimiter: str = ",",
    ) -> str:
        """Apply dynamic viewer filter to CSV content (pure data transformation).

        Args:
            content: CSV content as string
            columns_to_hide: Column names to remove
            delimiter: CSV delimiter

        Returns:
            Filtered CSV content
        """
        if not columns_to_hide or not content:
            return content
        lines = content.split("\n")
        if not lines:
            return content
        # Parse header
        header = lines[0].split(delimiter)
        hide_indices = {i for i, col in enumerate(header) if col.strip() in columns_to_hide}
        if not hide_indices:
            return content
        # Filter columns
        filtered_lines: list[str] = []
        for line in lines:
            if not line.strip():
                filtered_lines.append(line)
                continue
            cols = line.split(delimiter)
            filtered = [c for i, c in enumerate(cols) if i not in hide_indices]
            filtered_lines.append(delimiter.join(filtered))
        return "\n".join(filtered_lines)

    @rpc_expose(description="Read with dynamic viewer filter")
    async def read_with_dynamic_viewer(
        self,
        resource: tuple[str, str],
        content: str,
        zone_id: str | None = None,
    ) -> str:
        """Read content with dynamic viewer filter applied.

        Args:
            resource: Resource being read
            content: Raw content
            zone_id: Zone scope

        Returns:
            Filtered content (or original if no dynamic viewer)
        """
        config = await self.get_dynamic_viewer_config(resource, zone_id)
        if not config:
            return content
        # Extract column hide list from viewer config
        columns_to_hide: list[str] = []
        for viewer in config.get("viewers", []):
            if isinstance(viewer.get("subject_id"), str) and viewer["subject_id"].startswith(
                "hide:"
            ):
                columns_to_hide.append(viewer["subject_id"][5:])
        if not columns_to_hide:
            return content
        return self.apply_dynamic_viewer_filter(content, columns_to_hide)

    # =========================================================================
    # Helper Methods
    # =========================================================================

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
            context: Operation context (OperationContext or dict)
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

        from nexus.contracts.types import OperationContext, Permission

        # Extract OperationContext from context parameter
        op_context: OperationContext | None = None
        if isinstance(context, OperationContext):
            op_context = context
        elif isinstance(context, dict):
            # Create OperationContext from dict
            op_context = OperationContext(
                user_id=context.get("user_id", "unknown"),
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
                subject=get_subject_from_context(context) or ("user", op_context.user_id),
                permission="owner",  # Only owners can manage permissions
                object=resource,
                context=context,
            )
            if not has_permission:
                raise PermissionError(
                    f"Access denied: User '{op_context.user_id}' does not have owner "
                    f"permission to manage {resource[0]} '{resource[1]}'"
                )
            return

        # Use permission enforcer to check permission for file resources
        if getattr(self, "_permission_enforcer", None) is not None:
            has_permission = self._permission_enforcer.check(resource_path, perm_enum, op_context)

            # If enforcer denied, also check direct ownership via ReBAC
            # (direct_owner relation may not imply execute in the permission graph)
            if not has_permission and self._rebac_manager:
                subject = get_subject_from_context(context) or ("user", op_context.user_id)
                has_permission = self._rebac_manager.rebac_check(
                    subject=subject,
                    permission="owner",
                    object=resource,
                    zone_id=op_context.zone_id,
                )

            # If user is not owner, check if they are zone admin
            if not has_permission:
                # Extract zone from resource path (format: /zone/{zone_id}/...)
                zone_id = None
                if resource_path.startswith("/zone/"):
                    parts = resource_path[6:].split("/", 1)  # Remove "/zone/" prefix
                    if parts:
                        zone_id = parts[0]

                # Check if user is zone admin for this resource's zone
                if zone_id and op_context.user_id:
                    from nexus.lib.zone_helpers import is_zone_admin

                    if is_zone_admin(self._rebac_manager, op_context.user_id, zone_id):
                        # Zone admin can share resources in their zone
                        return

                # Neither owner nor zone admin - deny
                perm_name = required_permission.upper()
                raise PermissionError(
                    f"Access denied: User '{op_context.user_id}' does not have {perm_name} "
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
        from nexus.lib.zone import normalize_zone_id

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
        zone_id: str | None = None,
    ) -> list[tuple[str, str]]:
        """Synchronous rebac_expand."""
        mgr = self._require_manager()
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")
        kwargs: dict[str, Any] = {"permission": permission, "object": object}
        if zone_id is not None:
            kwargs["zone_id"] = zone_id
        expanded: list[tuple[str, str]] = mgr.rebac_expand(**kwargs)
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
        """Synchronous rebac_list_tuples — ORM query."""
        from sqlalchemy import select

        from nexus.storage.models.permissions import ReBACTupleModel as RT

        mgr = self._require_manager()

        stmt = select(RT)

        if subject:
            stmt = stmt.where(RT.subject_type == subject[0], RT.subject_id == subject[1])

        if relation:
            stmt = stmt.where(RT.relation == relation)
        elif relation_in:
            stmt = stmt.where(RT.relation.in_(relation_in))

        if object:
            stmt = stmt.where(RT.object_type == object[0], RT.object_id == object[1])

        with mgr.engine.connect() as conn:
            result = conn.execute(stmt)
            results = []
            for row in result:
                results.append(
                    {
                        "tuple_id": row.tuple_id,
                        "subject_type": row.subject_type,
                        "subject_id": row.subject_id,
                        "relation": row.relation,
                        "object_type": row.object_type,
                        "object_id": row.object_id,
                        "created_at": row.created_at,
                        "expires_at": row.expires_at,
                        "zone_id": row.zone_id,
                    }
                )
            return results

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

        from nexus.bricks.rebac.domain import NamespaceConfig

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
        mgr = self._require_manager()
        result: list[dict[str, Any]] = mgr.list_namespaces()
        return result

    def namespace_delete_sync(self, object_type: str) -> bool:
        """Delete a namespace configuration (sync)."""
        mgr = self._require_manager()
        deleted: bool = mgr.delete_namespace(object_type)
        return deleted

    # =========================================================================
    # Sync: Sharing, Privacy, Dynamic Viewer, Tiger Cache
    # (Provided by ReBACShareMixin via inheritance)
    # =========================================================================
