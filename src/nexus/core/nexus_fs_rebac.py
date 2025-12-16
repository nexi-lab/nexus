"""ReBAC (Relationship-Based Access Control) operations for NexusFS.

This module contains relationship-based permission operations:
- rebac_create: Create relationship tuple
- rebac_check: Check permission via relationships
- rebac_expand: Find all subjects with permission
- rebac_delete: Delete relationship tuple
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager


class NexusFSReBACMixin:
    """Mixin providing ReBAC operations for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        _rebac_manager: EnhancedReBACManager
        _enforce_permissions: bool

        def _validate_path(self, path: str) -> str: ...

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

    @rpc_expose(description="Create ReBAC relationship tuple")
    def rebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        tenant_id: str | None = None,
        context: Any = None,  # Accept OperationContext, EnhancedOperationContext, or dict
        column_config: dict[str, Any] | None = None,  # Column-level permissions for dynamic_viewer
    ) -> str:
        """Create a relationship tuple in ReBAC system.

        Args:
            subject: (subject_type, subject_id) tuple (e.g., ('agent', 'alice'))
            relation: Relation type (e.g., 'member-of', 'owner-of', 'viewer-of', 'dynamic_viewer')
            object: (object_type, object_id) tuple (e.g., ('group', 'developers'))
            expires_at: Optional expiration datetime for temporary relationships
            tenant_id: Optional tenant ID for multi-tenant isolation. If None, uses
                       tenant_id from operation context.
            context: Operation context (automatically provided by RPC server)
            column_config: Optional column-level permissions config for dynamic_viewer relation.
                          Only applies to CSV files.
                          Structure: {
                              "hidden_columns": ["password", "ssn"],  # Completely hide these columns
                              "aggregations": {"age": "mean", "salary": "sum"},  # Show aggregated values
                              "visible_columns": ["name", "email"]  # Show raw data (optional, auto-calculated if empty)
                          }
                          Note: A column can only appear in one category (hidden, aggregations, or visible)

        Returns:
            Tuple ID of created relationship

        Raises:
            ValueError: If subject or object tuples are invalid, or column_config is invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Alice is member of developers group
            >>> nx.rebac_create(
            ...     subject=("agent", "alice"),
            ...     relation="member-of",
            ...     object=("group", "developers")
            ... )
            'uuid-string'

            >>> # Developers group owns file
            >>> nx.rebac_create(
            ...     subject=("group", "developers"),
            ...     relation="owner-of",
            ...     object=("file", "/workspace/project.txt")
            ... )
            'uuid-string'

            >>> # Temporary viewer access (expires in 1 hour)
            >>> from datetime import timedelta
            >>> nx.rebac_create(
            ...     subject=("agent", "bob"),
            ...     relation="viewer-of",
            ...     object=("file", "/workspace/secret.txt"),
            ...     expires_at=datetime.now(UTC) + timedelta(hours=1)
            ... )
            'uuid-string'

            >>> # Dynamic viewer with column-level permissions for CSV files
            >>> nx.rebac_create(
            ...     subject=("agent", "alice"),
            ...     relation="dynamic_viewer",
            ...     object=("file", "/data/users.csv"),
            ...     column_config={
            ...         "hidden_columns": ["password", "ssn"],
            ...         "aggregations": {"age": "mean", "salary": "sum"},
            ...         "visible_columns": ["name", "email"]
            ...     }
            ... )
            'uuid-string'
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Validate tuples (support 2-tuple and 3-tuple for subject to support userset-as-subject)
        if not isinstance(subject, tuple) or len(subject) not in (2, 3):
            raise ValueError(
                f"subject must be (type, id) or (type, id, relation) tuple, got {subject}"
            )
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        # Use tenant_id from context if not explicitly provided
        effective_tenant_id = tenant_id
        if effective_tenant_id is None and context:
            # Handle both dict and OperationContext/EnhancedOperationContext
            if isinstance(context, dict):
                effective_tenant_id = context.get("tenant")
            elif hasattr(context, "tenant_id"):
                effective_tenant_id = context.tenant_id

        # SECURITY: Check execute permission before allowing permission management
        # Only owners (those with execute permission) can grant/manage permissions on resources
        # Exception: Allow permission management on non-file objects (groups, etc.) for now
        if object[0] == "file" and context:
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
                    tenant_id=context.get("tenant_id"),
                    is_admin=context.get("is_admin", False),
                    is_system=context.get("is_system", False),
                )

            # Check if caller has execute permission on the file object
            if (
                op_context
                and self._enforce_permissions
                and not op_context.is_admin
                and not op_context.is_system
            ):
                file_path = object[1]  # Extract file path from object tuple

                # Use permission enforcer to check execute permission
                if hasattr(self, "_permission_enforcer"):
                    has_execute = self._permission_enforcer.check(
                        file_path, Permission.EXECUTE, op_context
                    )
                    if not has_execute:
                        raise PermissionError(
                            f"Access denied: User '{op_context.user}' does not have EXECUTE "
                            f"permission to manage permissions on '{file_path}'"
                        )

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
                    "Provide configuration with hidden_columns, aggregations, and/or visible_columns."
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

            # Validate columns against actual CSV file
            file_path = object[1]
            if hasattr(self, "read") and hasattr(self, "exists"):
                try:
                    # Check if file exists
                    if self.exists(file_path):
                        # Read file to get actual columns
                        content = self.read(file_path)
                        if isinstance(content, bytes):
                            content = content.decode("utf-8")

                        try:
                            import io

                            import pandas as pd

                            df = pd.read_csv(io.StringIO(content))
                            actual_columns = set(df.columns)

                            # Collect all configured columns
                            configured_columns = (
                                set(hidden_columns)
                                | set(aggregations.keys())
                                | set(visible_columns)
                            )

                            # Check for invalid columns
                            invalid_columns = configured_columns - actual_columns
                            if invalid_columns:
                                raise ValueError(
                                    f"Column config contains invalid columns: {sorted(invalid_columns)}. "
                                    f"Available columns in CSV: {sorted(actual_columns)}"
                                )
                        except ValueError:
                            # Re-raise ValueError (validation error)
                            raise
                        except ImportError:
                            # pandas not available, skip validation
                            pass
                        except Exception as e:
                            # If CSV parsing fails (non-validation error), provide warning but allow creation
                            import logging

                            logger = logging.getLogger(__name__)
                            logger.warning(
                                f"Could not validate CSV columns for {file_path}: {e}. "
                                f"Column config will be created without validation."
                            )
                except ValueError:
                    # Re-raise validation errors
                    raise
                except Exception as e:
                    # If file read fails, skip validation (file might not exist yet)
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.debug(f"Could not read file {file_path} for column validation: {e}")

            # Check that a column only appears in one category
            all_columns = set()
            for col in hidden_columns:
                if col in all_columns:
                    raise ValueError(
                        f"Column '{col}' appears in multiple categories. "
                        f"Each column can only be in hidden_columns, aggregations, or visible_columns."
                    )
                all_columns.add(col)

            for col in aggregations:
                if col in all_columns:
                    raise ValueError(
                        f"Column '{col}' appears in multiple categories. "
                        f"Each column can only be in hidden_columns, aggregations, or visible_columns."
                    )
                all_columns.add(col)

            for col in visible_columns:
                if col in all_columns:
                    raise ValueError(
                        f"Column '{col}' appears in multiple categories. "
                        f"Each column can only be in hidden_columns, aggregations, or visible_columns."
                    )
                all_columns.add(col)

            # Validate aggregation operations (single value per column)
            valid_ops = {"mean", "sum", "min", "max", "std", "median", "count"}
            for col, op in aggregations.items():
                if not isinstance(op, str):
                    raise ValueError(
                        f"column_config.aggregations['{col}'] must be a string (one of: {', '.join(valid_ops)}). "
                        f"Got: {type(op).__name__}"
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
            raise ValueError("column_config can only be provided when relation is 'dynamic_viewer'")

        # Create relationship
        return self._rebac_manager.rebac_write(
            subject=subject,
            relation=relation,
            object=object,
            expires_at=expires_at,
            tenant_id=effective_tenant_id,
            conditions=conditions,
        )

    @rpc_expose(description="Check ReBAC permission")
    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: Any = None,  # Accept OperationContext, EnhancedOperationContext, or dict
        tenant_id: str | None = None,
    ) -> bool:
        """Check if subject has permission on object via ReBAC.

        Uses graph traversal to check both direct relationships and
        inherited permissions through group membership and hierarchies.

        Supports ABAC-style contextual conditions (time windows, IP allowlists, etc.).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write', 'owner')
            object: (object_type, object_id) tuple
            context: Optional ABAC context for condition evaluation (time, ip, device, attributes)
            tenant_id: Optional tenant ID for multi-tenant isolation (defaults to "default")

        Returns:
            True if permission is granted, False otherwise

        Raises:
            ValueError: If subject or object tuples are invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Basic check
            >>> nx.rebac_check(
            ...     subject=("agent", "alice"),
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt"),
            ...     tenant_id="org_acme"
            ... )
            True

            >>> # ABAC check with time window
            >>> nx.rebac_check(
            ...     subject=("agent", "contractor"),
            ...     permission="read",
            ...     object=("file", "/sensitive.txt"),
            ...     context={"time": "14:30", "ip": "10.0.1.5"},
            ...     tenant_id="org_acme"
            ... )
            True  # Allowed during business hours

            >>> # Check after hours
            >>> nx.rebac_check(
            ...     subject=("agent", "contractor"),
            ...     permission="read",
            ...     object=("file", "/sensitive.txt"),
            ...     context={"time": "20:00", "ip": "10.0.1.5"},
            ...     tenant_id="org_acme"
            ... )
            False  # Denied outside time window
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Validate tuples
        if not isinstance(subject, tuple) or len(subject) != 2:
            raise ValueError(f"subject must be (type, id) tuple, got {subject}")
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        # P0-4: Pass tenant_id for multi-tenant isolation
        # Use tenant_id from operation context if not explicitly provided
        effective_tenant_id = tenant_id
        if effective_tenant_id is None and context:
            # Handle both dict and OperationContext/EnhancedOperationContext
            if isinstance(context, dict):
                effective_tenant_id = context.get("tenant")
            elif hasattr(context, "tenant_id"):
                effective_tenant_id = context.tenant_id
        # BUGFIX: Don't default to "default" - let ReBAC manager handle None
        # This allows proper tenant isolation testing

        # Check permission with optional context
        return self._rebac_manager.rebac_check(
            subject=subject,
            permission=permission,
            object=object,
            context=context,
            tenant_id=effective_tenant_id,
        )

    @rpc_expose(description="Expand ReBAC permissions to find all subjects")
    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
    ) -> list[tuple[str, str]]:
        """Find all subjects that have a given permission on an object.

        Uses recursive graph expansion to find both direct and inherited permissions.

        Args:
            permission: Permission to check (e.g., 'read', 'write', 'owner')
            object: (object_type, object_id) tuple

        Returns:
            List of (subject_type, subject_id) tuples that have the permission

        Raises:
            ValueError: If object tuple is invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Who can read this file?
            >>> nx.rebac_expand(
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt")
            ... )
            [('agent', 'alice'), ('agent', 'bob'), ('group', 'developers')]

            >>> # Who owns this workspace?
            >>> nx.rebac_expand(
            ...     permission="owner",
            ...     object=("workspace", "/workspace")
            ... )
            [('group', 'admins')]
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Validate tuple
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        # Expand permission
        return self._rebac_manager.rebac_expand(permission=permission, object=object)

    @rpc_expose(description="Explain ReBAC permission check")
    def rebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        tenant_id: str | None = None,
        context: Any = None,  # Accept OperationContext, EnhancedOperationContext, or dict
    ) -> dict:
        """Explain why a subject has or doesn't have permission on an object.

        This debugging API traces through the permission graph to show exactly
        why a permission check succeeded or failed.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write', 'owner')
            object: (object_type, object_id) tuple
            tenant_id: Optional tenant ID for multi-tenant isolation. If None, uses
                       tenant_id from operation context.
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
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Why does alice have read permission?
            >>> explanation = nx.rebac_explain(
            ...     subject=("agent", "alice"),
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt"),
            ...     tenant_id="org_acme"
            ... )
            >>> print(explanation["reason"])
            'alice has 'read' on file:/workspace/doc.txt via parent inheritance'

            >>> # Why doesn't bob have write permission?
            >>> explanation = nx.rebac_explain(
            ...     subject=("agent", "bob"),
            ...     permission="write",
            ...     object=("workspace", "/workspace")
            ... )
            >>> print(explanation["result"])
            False
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Validate tuples
        if not isinstance(subject, tuple) or len(subject) != 2:
            raise ValueError(f"subject must be (type, id) tuple, got {subject}")
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        # Use tenant_id from context if not explicitly provided
        effective_tenant_id = tenant_id
        if effective_tenant_id is None and context:
            # Handle both dict and OperationContext/EnhancedOperationContext
            if isinstance(context, dict):
                effective_tenant_id = context.get("tenant")
            elif hasattr(context, "tenant_id"):
                effective_tenant_id = context.tenant_id

        # Get explanation
        return self._rebac_manager.rebac_explain(
            subject=subject, permission=permission, object=object, tenant_id=effective_tenant_id
        )

    @rpc_expose(description="Batch ReBAC permission checks")
    def rebac_check_batch(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> list[bool]:
        """Batch permission checks for efficiency.

        Performs multiple permission checks in a single call, using shared cache lookups
        and optimized database queries. More efficient than individual checks when checking
        multiple permissions.

        Args:
            checks: List of (subject, permission, object) tuples to check

        Returns:
            List of boolean results in the same order as input

        Raises:
            ValueError: If any check tuple is invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Check multiple permissions at once
            >>> results = nx.rebac_check_batch([
            ...     (("agent", "alice"), "read", ("file", "/workspace/doc1.txt")),
            ...     (("agent", "alice"), "read", ("file", "/workspace/doc2.txt")),
            ...     (("agent", "bob"), "write", ("file", "/workspace/doc3.txt")),
            ... ])
            >>> # Returns: [True, False, True]
            >>>
            >>> # Check if user has multiple permissions on same object
            >>> results = nx.rebac_check_batch([
            ...     (("agent", "alice"), "read", ("file", "/project")),
            ...     (("agent", "alice"), "write", ("file", "/project")),
            ...     (("agent", "alice"), "owner", ("file", "/project")),
            ... ])
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
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

    @rpc_expose(description="Delete ReBAC relationship tuple")
    def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a relationship tuple by ID.

        Args:
            tuple_id: ID of the tuple to delete (returned from rebac_create)

        Returns:
            True if tuple was deleted, False if not found

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> tuple_id = nx.rebac_create(
            ...     subject=("agent", "alice"),
            ...     relation="viewer-of",
            ...     object=("file", "/workspace/doc.txt")
            ... )
            >>> nx.rebac_delete(tuple_id)
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Delete tuple
        return self._rebac_manager.rebac_delete(tuple_id=tuple_id)

    @rpc_expose(description="List ReBAC relationship tuples")
    def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
    ) -> list[dict]:
        """List relationship tuples matching filters.

        Args:
            subject: Optional (subject_type, subject_id) filter
            relation: Optional relation type filter (mutually exclusive with relation_in)
            object: Optional (object_type, object_id) filter
            relation_in: Optional list of relation types to filter (mutually exclusive with relation)

        Returns:
            List of tuple dictionaries with keys:
                - tuple_id: Tuple ID
                - subject_type, subject_id: Subject
                - relation: Relation type
                - object_type, object_id: Object
                - created_at: Creation timestamp
                - expires_at: Optional expiration timestamp

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # List all relationships for alice
            >>> nx.rebac_list_tuples(subject=("agent", "alice"))
            [
                {
                    'tuple_id': 'uuid-1',
                    'subject_type': 'agent',
                    'subject_id': 'alice',
                    'relation': 'member-of',
                    'object_type': 'group',
                    'object_id': 'developers',
                    'created_at': datetime(...),
                    'expires_at': None
                }
            ]

            >>> # List tuples with multiple relation types (single query)
            >>> nx.rebac_list_tuples(
            ...     subject=("user", "alice"),
            ...     relation_in=["shared-viewer", "shared-editor", "shared-owner"]
            ... )
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Build query
        conn = self._rebac_manager._get_connection()
        query = "SELECT * FROM rebac_tuples WHERE 1=1"
        params: list = []

        if subject:
            query += " AND subject_type = ? AND subject_id = ?"
            params.extend([subject[0], subject[1]])

        if relation:
            query += " AND relation = ?"
            params.append(relation)
        elif relation_in:
            # N+1 FIX: Support multiple relations in a single query
            placeholders = ", ".join("?" * len(relation_in))
            query += f" AND relation IN ({placeholders})"
            params.extend(relation_in)

        if object:
            query += " AND object_type = ? AND object_id = ?"
            params.extend([object[0], object[1]])

        # Fix SQL placeholders for PostgreSQL
        query = self._rebac_manager._fix_sql_placeholders(query)

        cursor = self._rebac_manager._create_cursor(conn)
        cursor.execute(query, params)

        results = []
        for row in cursor.fetchall():
            # Both SQLite and PostgreSQL now return dict-like rows
            # Note: sqlite3.Row doesn't have .get() method, so use try/except for optional fields
            try:
                tenant_id = row["tenant_id"]
            except (KeyError, IndexError):
                tenant_id = None

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
                    "tenant_id": tenant_id,
                }
            )

        return results

    # =========================================================================
    # Public API Wrappers for Configuration (P1 - Should Do)
    # =========================================================================

    @rpc_expose(description="Set ReBAC configuration option")
    def set_rebac_option(self, key: str, value: Any) -> None:
        """Set a ReBAC configuration option.

        Provides public access to ReBAC configuration without using internal APIs.

        Args:
            key: Configuration key (e.g., "max_depth", "cache_ttl")
            value: Configuration value

        Raises:
            ValueError: If key is invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Set maximum graph traversal depth
            >>> nx.set_rebac_option("max_depth", 15)

            >>> # Set cache TTL
            >>> nx.set_rebac_option("cache_ttl", 600)
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
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
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Get current max depth
            >>> depth = nx.get_rebac_option("max_depth")
            >>> print(f"Max traversal depth: {depth}")
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
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

        Args:
            namespace: Namespace configuration dictionary with keys:
                - object_type: Type of objects this namespace applies to
                - config: Schema configuration (relations and permissions)

        Raises:
            RuntimeError: If ReBAC is not available
            ValueError: If namespace configuration is invalid

        Examples:
            >>> # Register file namespace with group inheritance
            >>> nx.register_namespace({
            ...     "object_type": "file",
            ...     "config": {
            ...         "relations": {
            ...             "viewer": {},
            ...             "editor": {}
            ...         },
            ...         "permissions": {
            ...             "read": ["viewer", "editor"],
            ...             "write": ["editor"]
            ...         }
            ...     }
            ... })
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Validate namespace structure
        if not isinstance(namespace, dict):
            raise ValueError("namespace must be a dictionary")
        if "object_type" not in namespace:
            raise ValueError("namespace must have 'object_type' key")
        if "config" not in namespace:
            raise ValueError("namespace must have 'config' key")

        # Import NamespaceConfig
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
    def get_namespace(self, object_type: str) -> dict[str, Any] | None:
        """Get namespace schema for an object type.

        Args:
            object_type: Type of objects (e.g., "file", "group")

        Returns:
            Namespace configuration dict or None if not found

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Get file namespace
            >>> ns = nx.get_namespace("file")
            >>> if ns:
            ...     print(f"Relations: {ns['config']['relations'].keys()}")
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        ns = self._rebac_manager.get_namespace(object_type)
        if ns is None:
            return None

        return {
            "namespace_id": ns.namespace_id,
            "object_type": ns.object_type,
            "config": ns.config,
            "created_at": ns.created_at.isoformat(),
            "updated_at": ns.updated_at.isoformat(),
        }

    @rpc_expose(description="Create or update ReBAC namespace")
    def namespace_create(self, object_type: str, config: dict[str, Any]) -> None:
        """Create or update a namespace configuration.

        Args:
            object_type: Type of objects this namespace applies to (e.g., "document", "project")
            config: Namespace configuration with "relations" and "permissions" keys

        Raises:
            RuntimeError: If ReBAC is not available
            ValueError: If configuration is invalid

        Examples:
            >>> # Create custom document namespace
            >>> nx.namespace_create("document", {
            ...     "relations": {
            ...         "owner": {},
            ...         "editor": {},
            ...         "viewer": {"union": ["editor", "owner"]}
            ...     },
            ...     "permissions": {
            ...         "read": ["viewer", "editor", "owner"],
            ...         "write": ["editor", "owner"]
            ...     }
            ... })
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Validate config structure
        if "relations" not in config or "permissions" not in config:
            raise ValueError("Namespace config must have 'relations' and 'permissions' keys")

        # Create namespace object
        import uuid
        from datetime import UTC, datetime

        from nexus.core.rebac import NamespaceConfig

        ns = NamespaceConfig(
            namespace_id=str(uuid.uuid4()),
            object_type=object_type,
            config=config,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        self._rebac_manager.create_namespace(ns)

    @rpc_expose(description="List all ReBAC namespaces")
    def namespace_list(self) -> list[dict[str, Any]]:
        """List all registered namespace configurations.

        Returns:
            List of namespace dictionaries with metadata and config

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # List all namespaces
            >>> namespaces = nx.namespace_list()
            >>> for ns in namespaces:
            ...     print(f"{ns['object_type']}: {list(ns['config']['relations'].keys())}")
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Get all namespaces by querying the database
        conn = self._rebac_manager._get_connection()
        cursor = self._rebac_manager._create_cursor(conn)

        cursor.execute(
            self._rebac_manager._fix_sql_placeholders(
                "SELECT namespace_id, object_type, config, created_at, updated_at FROM rebac_namespaces ORDER BY object_type"
            )
        )

        namespaces = []
        for row in cursor.fetchall():
            import json

            namespaces.append(
                {
                    "namespace_id": row["namespace_id"],
                    "object_type": row["object_type"],
                    "config": json.loads(row["config"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )

        return namespaces

    @rpc_expose(description="Delete ReBAC namespace")
    def namespace_delete(self, object_type: str) -> bool:
        """Delete a namespace configuration.

        Args:
            object_type: Type of objects to remove namespace for

        Returns:
            True if namespace was deleted, False if not found

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Delete custom namespace
            >>> nx.namespace_delete("document")
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        conn = self._rebac_manager._get_connection()
        cursor = self._rebac_manager._create_cursor(conn)

        # Check if exists
        cursor.execute(
            self._rebac_manager._fix_sql_placeholders(
                "SELECT namespace_id FROM rebac_namespaces WHERE object_type = ?"
            ),
            (object_type,),
        )

        if cursor.fetchone() is None:
            return False

        # Delete
        cursor.execute(
            self._rebac_manager._fix_sql_placeholders(
                "DELETE FROM rebac_namespaces WHERE object_type = ?"
            ),
            (object_type,),
        )

        conn.commit()

        # Invalidate cache if available
        if hasattr(self._rebac_manager, "_cache"):
            self._rebac_manager._cache.clear()

        return True

    # =========================================================================
    # Consent & Privacy Controls (Advanced Feature)
    # =========================================================================

    @rpc_expose(description="Expand ReBAC permissions with privacy filtering")
    def rebac_expand_with_privacy(
        self,
        permission: str,
        object: tuple[str, str],
        respect_consent: bool = True,
        requester: tuple[str, str] | None = None,
    ) -> list[tuple[str, str]]:
        """Find subjects with permission, optionally filtering by consent.

        This enables privacy-aware queries where subjects who haven't granted
        consent are filtered from results.

        Args:
            permission: Permission to check
            object: Object to expand on
            respect_consent: Filter results by consent/public_discoverable
            requester: Who is requesting (for consent checks)

        Returns:
            List of subjects, potentially filtered by privacy

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Standard expand (no privacy filtering)
            >>> viewers = nx.rebac_expand_with_privacy(
            ...     "view",
            ...     ("file", "/doc.txt"),
            ...     respect_consent=False
            ... )
            >>> # Returns all viewers

            >>> # Privacy-aware expand
            >>> viewers = nx.rebac_expand_with_privacy(
            ...     "view",
            ...     ("file", "/doc.txt"),
            ...     respect_consent=True,
            ...     requester=("user", "charlie")
            ... )
            >>> # Returns only users charlie can discover
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Get all subjects with permission
        all_subjects = self.rebac_expand(permission, object)

        if not respect_consent or not requester:
            return all_subjects

        # Filter by consent - only return subjects requester can discover
        filtered = []
        for subject in all_subjects:
            # Check if requester can discover this subject
            can_discover = self._rebac_manager.rebac_check(
                subject=requester, permission="discover", object=subject
            )
            if can_discover:
                filtered.append(subject)

        return filtered

    @rpc_expose(description="Grant consent for discovery")
    def grant_consent(
        self,
        from_subject: tuple[str, str],
        to_subject: tuple[str, str],
        expires_at: datetime | None = None,
        tenant_id: str | None = None,
    ) -> str:
        """Grant consent for one subject to discover another.

        Args:
            from_subject: Who is granting consent (e.g., profile, resource)
            to_subject: Who can now discover
            expires_at: Optional expiration
            tenant_id: Optional tenant ID

        Returns:
            Tuple ID

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Alice grants Bob consent to see her profile
            >>> from datetime import timedelta, UTC
            >>> nx.grant_consent(
            ...     from_subject=("profile", "alice"),
            ...     to_subject=("user", "bob"),
            ...     expires_at=datetime.now(UTC) + timedelta(days=30)
            ... )
            'uuid-string'

            >>> # Grant permanent consent
            >>> nx.grant_consent(
            ...     from_subject=("file", "/doc.txt"),
            ...     to_subject=("user", "charlie")
            ... )
            'uuid-string'
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        return self.rebac_create(
            subject=to_subject,
            relation="consent_granted",
            object=from_subject,
            expires_at=expires_at,
            tenant_id=tenant_id,
        )

    @rpc_expose(description="Revoke consent")
    def revoke_consent(self, from_subject: tuple[str, str], to_subject: tuple[str, str]) -> bool:
        """Revoke previously granted consent.

        Args:
            from_subject: Who is revoking
            to_subject: Who loses discovery access

        Returns:
            True if consent was revoked, False if no consent existed

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Revoke Bob's consent to see Alice's profile
            >>> nx.revoke_consent(
            ...     from_subject=("profile", "alice"),
            ...     to_subject=("user", "bob")
            ... )
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Find the consent tuple
        tuples = self.rebac_list_tuples(
            subject=to_subject, relation="consent_granted", object=from_subject
        )

        if tuples:
            return self.rebac_delete(tuples[0]["tuple_id"])
        return False

    @rpc_expose(description="Make resource publicly discoverable")
    def make_public(self, resource: tuple[str, str], tenant_id: str | None = None) -> str:
        """Make a resource publicly discoverable.

        Args:
            resource: Resource to make public
            tenant_id: Optional tenant ID

        Returns:
            Tuple ID

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Make alice's profile public
            >>> nx.make_public(("profile", "alice"))
            'uuid-string'

            >>> # Make file publicly discoverable
            >>> nx.make_public(("file", "/public/doc.txt"))
            'uuid-string'
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        return self.rebac_create(
            subject=("*", "*"),  # Wildcard = public
            relation="public_discoverable",
            object=resource,
            tenant_id=tenant_id,
        )

    @rpc_expose(description="Make resource private")
    def make_private(self, resource: tuple[str, str]) -> bool:
        """Remove public discoverability from a resource.

        Args:
            resource: Resource to make private

        Returns:
            True if made private, False if wasn't public

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Make alice's profile private
            >>> nx.make_private(("profile", "alice"))
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Find public tuple
        tuples = self.rebac_list_tuples(
            subject=("*", "*"), relation="public_discoverable", object=resource
        )

        if tuples:
            return self.rebac_delete(tuples[0]["tuple_id"])
        return False

    # =========================================================================
    # Cross-Tenant Sharing APIs
    # =========================================================================

    @rpc_expose(description="Share a resource with a specific user (same or different tenant)")
    def share_with_user(
        self,
        resource: tuple[str, str],
        user_id: str,
        relation: str = "viewer",
        tenant_id: str | None = None,
        user_tenant_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        """Share a resource with a specific user, regardless of tenant.

        This enables cross-tenant sharing - users from different organizations
        can be granted access to specific resources.

        Args:
            resource: Resource to share (e.g., ("file", "/path/to/doc.txt"))
            user_id: User to share with (e.g., "bob@partner-company.com")
            relation: Permission level - "viewer" (read) or "editor" (read/write)
            tenant_id: Resource owner's tenant ID (defaults to current tenant)
            user_tenant_id: Recipient user's tenant ID (for cross-tenant shares)
            expires_at: Optional expiration datetime for the share

        Returns:
            Share ID (tuple_id) that can be used to revoke the share

        Raises:
            RuntimeError: If ReBAC is not available
            ValueError: If relation is not "viewer" or "editor"

        Examples:
            >>> # Share file with user in same tenant
            >>> share_id = nx.share_with_user(
            ...     resource=("file", "/project/doc.txt"),
            ...     user_id="alice@mycompany.com",
            ...     relation="editor"
            ... )

            >>> # Share file with user in different tenant
            >>> share_id = nx.share_with_user(
            ...     resource=("file", "/project/doc.txt"),
            ...     user_id="bob@partner.com",
            ...     user_tenant_id="partner-tenant",
            ...     relation="viewer",
            ...     expires_at=datetime(2024, 12, 31)
            ... )
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Map user-facing relation to internal tuple relation
        # These shared-* relations are included in the viewer/editor/owner unions
        # for proper permission inheritance
        relation_map = {
            "viewer": "shared-viewer",
            "editor": "shared-editor",
            "owner": "shared-owner",
        }
        if relation not in relation_map:
            raise ValueError(f"relation must be 'viewer', 'editor', or 'owner', got '{relation}'")

        tuple_relation = relation_map[relation]

        # Parse expires_at if it's a string (from RPC)
        expires_dt = None
        if expires_at is not None:
            if isinstance(expires_at, str):
                from datetime import datetime as dt

                expires_dt = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                expires_dt = expires_at

        # Use shared-* relations which are allowed to cross tenant boundaries
        # Call underlying manager directly to support cross-tenant parameters
        return self._rebac_manager.rebac_write(
            subject=("user", user_id),
            relation=tuple_relation,
            object=resource,
            tenant_id=tenant_id,
            subject_tenant_id=user_tenant_id,
            expires_at=expires_dt,
        )

    @rpc_expose(description="Revoke a share by resource and user")
    def revoke_share(
        self,
        resource: tuple[str, str],
        user_id: str,
    ) -> bool:
        """Revoke a share for a specific user on a resource.

        Args:
            resource: Resource to unshare (e.g., ("file", "/path/to/doc.txt"))
            user_id: User to revoke access from

        Returns:
            True if share was revoked, False if no share existed

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> nx.revoke_share(
            ...     resource=("file", "/project/doc.txt"),
            ...     user_id="bob@partner.com"
            ... )
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Find the share tuple - use single query with relation_in (N+1 FIX)
        tuples = self.rebac_list_tuples(
            subject=("user", user_id),
            relation_in=["shared-viewer", "shared-editor", "shared-owner"],
            object=resource,
        )
        if tuples:
            return self.rebac_delete(tuples[0]["tuple_id"])
        return False

    @rpc_expose(description="Revoke a share by share ID")
    def revoke_share_by_id(self, share_id: str) -> bool:
        """Revoke a share using its ID.

        Args:
            share_id: The share ID returned by share_with_user()

        Returns:
            True if share was revoked, False if share didn't exist

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> share_id = nx.share_with_user(resource, user_id)
            >>> nx.revoke_share_by_id(share_id)
            True
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        return self.rebac_delete(share_id)

    @rpc_expose(description="List shares I've created (outgoing)")
    def list_outgoing_shares(
        self,
        resource: tuple[str, str] | None = None,
        tenant_id: str | None = None,  # noqa: ARG002 - Reserved for future tenant filtering
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List shares created by the current tenant (resources shared with others).

        Args:
            resource: Filter by specific resource (optional)
            tenant_id: Tenant ID to list shares for (defaults to current tenant)
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of share info dictionaries with keys:
            - share_id: Unique share identifier
            - resource_type: Type of shared resource
            - resource_id: ID of shared resource
            - recipient_id: User the resource is shared with
            - permission_level: "viewer" or "editor"
            - created_at: When the share was created
            - expires_at: When the share expires (if set)

        Examples:
            >>> # List all outgoing shares
            >>> shares = nx.list_outgoing_shares()
            >>> for share in shares:
            ...     print(f"{share['resource_id']} -> {share['recipient_id']}")

            >>> # List shares for a specific file
            >>> shares = nx.list_outgoing_shares(resource=("file", "/doc.txt"))
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Query for all shared-* relation tuples (N+1 FIX: single query with relation_in)
        all_tuples = self.rebac_list_tuples(
            relation_in=["shared-viewer", "shared-editor", "shared-owner"],
            object=resource,
        )

        # Map relation back to permission level
        relation_to_level = {
            "shared-viewer": "viewer",
            "shared-editor": "editor",
            "shared-owner": "owner",
        }

        # Transform to share info format
        shares = []
        for t in all_tuples[offset : offset + limit]:
            share_info = {
                "share_id": t.get("tuple_id"),
                "resource_type": t.get("object_type"),
                "resource_id": t.get("object_id"),
                "recipient_id": t.get("subject_id"),
                "permission_level": relation_to_level.get(t.get("relation") or "", "viewer"),
                "created_at": t.get("created_at"),
                "expires_at": t.get("expires_at"),
            }
            shares.append(share_info)

        return shares

    @rpc_expose(description="List shares I've received (incoming)")
    def list_incoming_shares(
        self,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List shares received by a user (resources shared with me).

        This includes cross-tenant shares from other organizations.

        Args:
            user_id: User ID to list incoming shares for
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of share info dictionaries with keys:
            - share_id: Unique share identifier
            - resource_type: Type of shared resource
            - resource_id: ID of shared resource
            - owner_tenant_id: Tenant that owns the resource
            - permission_level: "viewer" or "editor"
            - created_at: When the share was created
            - expires_at: When the share expires (if set)

        Examples:
            >>> # List all resources shared with me
            >>> shares = nx.list_incoming_shares(user_id="alice@mycompany.com")
            >>> for share in shares:
            ...     print(f"{share['resource_id']} from {share['owner_tenant_id']}")
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Query for all shared-* relation tuples where this user is the subject
        # This finds shares across all tenants (N+1 FIX: single query with relation_in)
        all_tuples = self.rebac_list_tuples(
            subject=("user", user_id),
            relation_in=["shared-viewer", "shared-editor", "shared-owner"],
        )

        # Map relation back to permission level
        relation_to_level = {
            "shared-viewer": "viewer",
            "shared-editor": "editor",
            "shared-owner": "owner",
        }

        # Transform to share info format
        shares = []
        for t in all_tuples[offset : offset + limit]:
            share_info = {
                "share_id": t.get("tuple_id"),
                "resource_type": t.get("object_type"),
                "resource_id": t.get("object_id"),
                "owner_tenant_id": t.get("tenant_id"),
                "permission_level": relation_to_level.get(t.get("relation") or "", "viewer"),
                "created_at": t.get("created_at"),
                "expires_at": t.get("expires_at"),
            }
            shares.append(share_info)

        return shares

    # =========================================================================
    # Dynamic Viewer - Column-level Permissions for Data Files
    # =========================================================================

    @rpc_expose(description="Get dynamic viewer configuration for a file")
    def get_dynamic_viewer_config(
        self,
        subject: tuple[str, str],
        file_path: str,
    ) -> dict[str, Any] | None:
        """Get the dynamic_viewer configuration for a subject and file.

        Args:
            subject: (subject_type, subject_id) tuple (e.g., ('agent', 'alice'))
            file_path: Path to the file

        Returns:
            Dictionary with column_config if dynamic_viewer relation exists, None otherwise

        Raises:
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Get alice's dynamic viewer config for users.csv
            >>> config = nx.get_dynamic_viewer_config(
            ...     subject=("agent", "alice"),
            ...     file_path="/data/users.csv"
            ... )
            >>> if config:
            ...     print(config["mode"])  # "whitelist" or "blacklist"
            ...     print(config["visible_columns"])  # ["name", "email"]
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Find dynamic_viewer tuples for this subject and file
        tuples = self.rebac_list_tuples(
            subject=subject, relation="dynamic_viewer", object=("file", file_path)
        )

        if not tuples:
            return None

        # Get the most recent tuple (in case there are multiple)
        tuple_data = tuples[0]

        # Parse conditions from the tuple
        import json

        conn = self._rebac_manager._get_connection()
        try:
            cursor = self._rebac_manager._create_cursor(conn)
            cursor.execute(
                self._rebac_manager._fix_sql_placeholders(
                    "SELECT conditions FROM rebac_tuples WHERE tuple_id = ?"
                ),
                (tuple_data["tuple_id"],),
            )
            row = cursor.fetchone()
            if row and row["conditions"]:
                conditions = json.loads(row["conditions"])
                if conditions.get("type") == "dynamic_viewer":
                    column_config = conditions.get("column_config")
                    return column_config if column_config is not None else None
        finally:
            conn.close()

        return None

    @rpc_expose(description="Apply dynamic viewer filter to CSV data")
    def apply_dynamic_viewer_filter(
        self,
        data: str,
        column_config: dict[str, Any],
        file_format: str = "csv",
    ) -> dict[str, Any]:
        """Apply column-level filtering and aggregations to CSV data.

        Args:
            data: Raw data content (CSV string)
            column_config: Column configuration dict with hidden_columns, aggregations, visible_columns
            file_format: Format of the data (currently only "csv" is supported)

        Returns:
            Dictionary with:
                - filtered_data: Filtered data as CSV string (visible columns + aggregated columns)
                - aggregations: Dictionary of computed aggregations
                - columns_shown: List of column names included in filtered data
                - aggregated_columns: List of aggregated column names with operation prefix

        Raises:
            ValueError: If file_format is not supported
            RuntimeError: If data parsing fails

        Examples:
            >>> # Apply filter to CSV data
            >>> result = nx.apply_dynamic_viewer_filter(
            ...     data="name,email,age,password\\nalice,a@ex.com,30,secret\\nbob,b@ex.com,25,pwd\\n",
            ...     column_config={
            ...         "hidden_columns": ["password"],
            ...         "aggregations": {"age": "mean"},
            ...         "visible_columns": ["name", "email"]
            ...     }
            ... )
            >>> print(result["filtered_data"])  # name,email,mean(age) with values
            >>> print(result["aggregations"])    # {"age": {"mean": 27.5}}
        """
        if file_format != "csv":
            raise ValueError(f"Unsupported file format: {file_format}. Only 'csv' is supported.")

        try:
            import io

            import pandas as pd
        except ImportError as e:
            raise RuntimeError(
                "pandas is required for dynamic viewer filtering. Install with: pip install pandas"
            ) from e

        # Parse CSV data
        try:
            df = pd.read_csv(io.StringIO(data))
        except Exception as e:
            raise RuntimeError(f"Failed to parse CSV data: {e}") from e

        # Get configuration
        hidden_columns = column_config.get("hidden_columns", [])
        aggregations = column_config.get("aggregations", {})
        visible_columns = column_config.get("visible_columns", [])

        # Auto-calculate visible_columns if empty
        # visible_columns = all columns - hidden_columns - aggregation columns
        if not visible_columns:
            all_cols = set(df.columns)
            hidden_set = set(hidden_columns)
            agg_set = set(aggregations.keys())
            visible_columns = list(all_cols - hidden_set - agg_set)

        # Build result dataframe in original column order
        # Iterate through original columns and add visible/aggregated columns in order
        result_columns = []  # List of (column_name, series) tuples
        aggregation_results: dict[str, dict[str, float | int | str]] = {}
        aggregated_column_names = []
        columns_shown = []

        for col in df.columns:
            if col in hidden_columns:
                # Skip hidden columns
                continue
            elif col in aggregations:
                # Add aggregated column at original position
                operation = aggregations[col]
                try:
                    # Compute aggregation
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
                        # Unknown operation, skip
                        continue

                    # Store aggregation result
                    if col not in aggregation_results:
                        aggregation_results[col] = {}
                    aggregation_results[col][operation] = agg_value

                    # Add aggregated column with formatted name
                    agg_col_name = f"{operation}({col})"
                    aggregated_column_names.append(agg_col_name)

                    # Create series with aggregated value repeated for all rows
                    agg_series = pd.Series([agg_value] * len(df), name=agg_col_name)
                    result_columns.append((agg_col_name, agg_series))

                except Exception as e:
                    # If aggregation fails, store error message
                    if col not in aggregation_results:
                        aggregation_results[col] = {}
                    aggregation_results[col][operation] = f"error: {str(e)}"
            elif col in visible_columns:
                # Add visible column at original position
                result_columns.append((col, df[col]))
                columns_shown.append(col)

        # Build result dataframe from ordered columns
        result_df = pd.DataFrame(dict(result_columns)) if result_columns else pd.DataFrame()

        # Convert result dataframe to CSV string
        filtered_data = result_df.to_csv(index=False)

        return {
            "filtered_data": filtered_data,
            "aggregations": aggregation_results,
            "columns_shown": columns_shown,
            "aggregated_columns": aggregated_column_names,
        }

    @rpc_expose(description="Read file with dynamic viewer permissions applied")
    def read_with_dynamic_viewer(
        self,
        file_path: str,
        subject: tuple[str, str],
        context: Any = None,
    ) -> dict[str, Any]:
        """Read a CSV file with dynamic_viewer permissions applied.

        This method checks if the subject has dynamic_viewer permissions on the file,
        and if so, applies the column-level filtering before returning the data.
        Only supports CSV files.

        Args:
            file_path: Path to the CSV file to read
            subject: (subject_type, subject_id) tuple
            context: Operation context (automatically provided by RPC server)

        Returns:
            Dictionary with:
                - content: Filtered file content (or full content if not dynamic viewer)
                - is_filtered: Boolean indicating if dynamic filtering was applied
                - config: The column config used (if filtered)
                - aggregations: Computed aggregations (if any)
                - columns_shown: List of visible columns (if filtered)
                - aggregated_columns: List of aggregated column names with operation prefix

        Raises:
            PermissionError: If subject has no read permission on file
            ValueError: If file is not a CSV file for dynamic_viewer
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Read CSV file with dynamic viewer permissions
            >>> result = nx.read_with_dynamic_viewer(
            ...     file_path="/data/users.csv",
            ...     subject=("agent", "alice")
            ... )
            >>> if result["is_filtered"]:
            ...     print("Filtered data:", result["content"])
            ...     print("Aggregations:", result["aggregations"])
            ...     print("Columns:", result["columns_shown"])
            ...     print("Aggregated:", result["aggregated_columns"])
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Check if this is a CSV file
        if not file_path.lower().endswith(".csv"):
            raise ValueError(
                f"read_with_dynamic_viewer only supports CSV files. "
                f"File '{file_path}' does not have .csv extension."
            )

        # Check if subject has read permission (either viewer or dynamic_viewer)
        has_read = self.rebac_check(
            subject=subject, permission="read", object=("file", file_path), context=context
        )

        if not has_read:
            raise PermissionError(f"Subject {subject} does not have read permission on {file_path}")

        # Get dynamic viewer config
        column_config = self.get_dynamic_viewer_config(subject=subject, file_path=file_path)

        # Read the file content WITHOUT dynamic_viewer filtering
        # We need the raw content to apply filtering here
        if (
            hasattr(self, "metadata")
            and hasattr(self, "router")
            and hasattr(self, "_get_routing_params")
        ):
            # NexusFS instance - read directly from backend to bypass filtering
            tenant_id, agent_id, is_admin = self._get_routing_params(context)
            route = self.router.route(
                file_path,
                tenant_id=tenant_id,
                agent_id=agent_id,
                is_admin=is_admin,
                check_write=False,
            )
            meta = self.metadata.get(file_path)
            if meta is None or meta.etag is None:
                raise RuntimeError(f"File not found: {file_path}")

            # Read raw content from backend
            content_bytes = route.backend.read_content(meta.etag, context=context)
            content = (
                content_bytes.decode("utf-8") if isinstance(content_bytes, bytes) else content_bytes
            )
        else:
            # Fallback: read from filesystem
            with open(file_path, encoding="utf-8") as f:
                content = f.read()

        # If no dynamic viewer config, return full content
        if not column_config:
            return {
                "content": content.encode("utf-8") if isinstance(content, str) else content,
                "is_filtered": False,
                "config": None,
                "aggregations": {},
                "columns_shown": [],
                "aggregated_columns": [],
            }

        # Apply dynamic viewer filtering to raw content
        result = self.apply_dynamic_viewer_filter(
            data=content,  # Raw unfiltered content
            column_config=column_config,
            file_format="csv",
        )

        return {
            "content": result["filtered_data"].encode("utf-8")
            if isinstance(result["filtered_data"], str)
            else result["filtered_data"],
            "is_filtered": True,
            "config": column_config,
            "aggregations": result["aggregations"],
            "columns_shown": result["columns_shown"],
            "aggregated_columns": result["aggregated_columns"],
        }
