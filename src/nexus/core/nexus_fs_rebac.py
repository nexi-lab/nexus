"""ReBAC (Relationship-Based Access Control) operations for NexusFS.

This module contains relationship-based permission operations:
- rebac_create: Create relationship tuple
- rebac_check: Check permission via relationships
- rebac_expand: Find all subjects with permission
- rebac_delete: Delete relationship tuple
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.rebac_manager import ReBACManager


class NexusFSReBACMixin:
    """Mixin providing ReBAC operations for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        _rebac_manager: ReBACManager

        def _validate_path(self, path: str) -> str: ...

    @rpc_expose(description="Create ReBAC relationship tuple")
    def rebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
    ) -> str:
        """Create a relationship tuple in ReBAC system.

        Args:
            subject: (subject_type, subject_id) tuple (e.g., ('agent', 'alice'))
            relation: Relation type (e.g., 'member-of', 'owner-of', 'viewer-of')
            object: (object_type, object_id) tuple (e.g., ('group', 'developers'))
            expires_at: Optional expiration datetime for temporary relationships

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

        # Validate tuples
        if not isinstance(subject, tuple) or len(subject) != 2:
            raise ValueError(f"subject must be (type, id) tuple, got {subject}")
        if not isinstance(object, tuple) or len(object) != 2:
            raise ValueError(f"object must be (type, id) tuple, got {object}")

        # Create relationship
        return self._rebac_manager.rebac_write(
            subject=subject, relation=relation, object=object, expires_at=expires_at
        )

    @rpc_expose(description="Check ReBAC permission")
    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
    ) -> bool:
        """Check if subject has permission on object via ReBAC.

        Uses graph traversal to check both direct relationships and
        inherited permissions through group membership and hierarchies.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write', 'owner')
            object: (object_type, object_id) tuple

        Returns:
            True if permission is granted, False otherwise

        Raises:
            ValueError: If subject or object tuples are invalid
            RuntimeError: If ReBAC is not available

        Examples:
            >>> # Check if alice can read file
            >>> nx.rebac_check(
            ...     subject=("agent", "alice"),
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt")
            ... )
            True

            >>> # Check if group owns workspace
            >>> nx.rebac_check(
            ...     subject=("group", "developers"),
            ...     permission="owner",
            ...     object=("workspace", "/workspace")
            ... )
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

        # Check permission
        return self._rebac_manager.rebac_check(
            subject=subject, permission=permission, object=object
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

        cursor = conn.cursor()
        cursor.execute(query, params)

        results = []
        for row in cursor.fetchall():
            # Handle both dict-like (SQLite) and tuple (PostgreSQL) access
            if hasattr(row, "keys"):
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
                    }
                )
            else:
                # PostgreSQL returns tuples
                results.append(
                    {
                        "tuple_id": row[0],
                        "subject_type": row[1],
                        "subject_id": row[2],
                        "relation": row[3],
                        "object_type": row[4],
                        "object_id": row[5],
                        "created_at": row[6],
                        "expires_at": row[7],
                    }
                )

        return results
