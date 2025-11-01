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

    @rpc_expose(description="Create ReBAC relationship tuple")
    def rebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        tenant_id: str | None = None,
        context: Any = None,  # Accept OperationContext, EnhancedOperationContext, or dict
    ) -> str:
        """Create a relationship tuple in ReBAC system.

        Args:
            subject: (subject_type, subject_id) tuple (e.g., ('agent', 'alice'))
            relation: Relation type (e.g., 'member-of', 'owner-of', 'viewer-of')
            object: (object_type, object_id) tuple (e.g., ('group', 'developers'))
            expires_at: Optional expiration datetime for temporary relationships
            tenant_id: Optional tenant ID for multi-tenant isolation. If None, uses
                       tenant_id from operation context.
            context: Operation context (automatically provided by RPC server)

        Returns:
            Tuple ID of created relationship

        Raises:
            ValueError: If subject or object tuples are invalid
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

        # Create relationship
        return self._rebac_manager.rebac_write(
            subject=subject,
            relation=relation,
            object=object,
            expires_at=expires_at,
            tenant_id=effective_tenant_id,
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

        # Perform batch check
        return self._rebac_manager.rebac_check_batch(checks=checks)

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
    ) -> list[dict]:
        """List relationship tuples matching filters.

        Args:
            subject: Optional (subject_type, subject_id) filter
            relation: Optional relation type filter
            object: Optional (object_type, object_id) filter

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
        """
        if not hasattr(self, "_rebac_manager"):
            raise RuntimeError(
                "ReBAC is not available. Ensure NexusFS is initialized in embedded mode."
            )

        # Build query
        conn = self._rebac_manager._get_connection()
        query = "SELECT * FROM rebac_tuples WHERE 1=1"
        params = []

        if subject:
            query += " AND subject_type = ? AND subject_id = ?"
            params.extend([subject[0], subject[1]])

        if relation:
            query += " AND relation = ?"
            params.append(relation)

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
