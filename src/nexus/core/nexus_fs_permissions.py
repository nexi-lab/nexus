"""Permission management operations for NexusFS.

This module contains file permission operations:
- chmod: Change file mode/permissions
- chown: Change file owner
- chgrp: Change file group
- grant_user/grant_group: ACL-based permissions
- deny_user: Explicit ACL deny
- get_acl: Retrieve ACL entries
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nexus.core.exceptions import NexusFileNotFoundError
from nexus.core.permissions import parse_mode
from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class NexusFSPermissionsMixin:
    """Mixin providing permission management operations for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        metadata: SQLAlchemyMetadataStore
        _default_context: OperationContext

        def _validate_path(self, path: str) -> str: ...

    @rpc_expose(description="Change file permissions")
    def chmod(
        self,
        path: str,
        mode: int | str,
        context: OperationContext | None = None,
    ) -> None:
        """Change file mode (permissions).

        Requires the user to be the owner of the file or an admin.

        Args:
            path: Virtual file path
            mode: Permission mode (int like 0o644 or string like '755')
            context: Optional operation context (defaults to self._default_context)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user is not owner and not admin
            ValueError: If mode is invalid

        Examples:
            >>> nx.chmod("/workspace/file.txt", 0o644)
            >>> nx.chmod("/workspace/file.txt", "755")
            >>> nx.chmod("/workspace/file.txt", "rwxr-xr-x")
        """
        path = self._validate_path(path)

        # Get file metadata
        file_meta = self.metadata.get(path)
        if not file_meta:
            raise NexusFileNotFoundError(path)

        # Get context (use default if not provided)
        ctx = context or self._default_context

        # Check if user is owner or admin
        # Must be owner to chmod (unless admin or system)
        if (
            not ctx.is_admin
            and not ctx.is_system
            and file_meta.owner
            and file_meta.owner != ctx.user
        ):
            raise PermissionError(
                f"Access denied: Only the owner ('{file_meta.owner}') or admin "
                f"can change permissions for '{path}'"
            )

        # Parse mode (handles int, octal string, or symbolic string)
        if isinstance(mode, str):
            mode_int = parse_mode(mode)
        elif isinstance(mode, int):
            mode_int = mode
        else:
            raise ValueError(f"mode must be int or str, got {type(mode)}")

        # Update mode
        file_meta.mode = mode_int
        self.metadata.put(file_meta)

        # Invalidate cache
        if self.metadata._cache_enabled and self.metadata._cache:
            self.metadata._cache.invalidate_path(path)

    @rpc_expose(description="Change file owner")
    def chown(
        self,
        path: str,
        owner: str,
        context: OperationContext | None = None,
    ) -> None:
        """Change file owner.

        Requires the user to be the current owner of the file or an admin.

        Args:
            path: Virtual file path
            owner: New owner username
            context: Optional operation context (defaults to self._default_context)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user is not owner and not admin

        Examples:
            >>> nx.chown("/workspace/file.txt", "alice")
        """
        path = self._validate_path(path)

        # Get file metadata
        file_meta = self.metadata.get(path)
        if not file_meta:
            raise NexusFileNotFoundError(path)

        # Get context (use default if not provided)
        ctx = context or self._default_context

        # Check if user is owner or admin
        # Must be owner to chown (unless admin or system)
        if (
            not ctx.is_admin
            and not ctx.is_system
            and file_meta.owner
            and file_meta.owner != ctx.user
        ):
            raise PermissionError(
                f"Access denied: Only the owner ('{file_meta.owner}') or admin "
                f"can change ownership for '{path}'"
            )

        # Update owner
        file_meta.owner = owner
        self.metadata.put(file_meta)

        # Invalidate cache
        if self.metadata._cache_enabled and self.metadata._cache:
            self.metadata._cache.invalidate_path(path)

    @rpc_expose(description="Change file group")
    def chgrp(
        self,
        path: str,
        group: str,
        context: OperationContext | None = None,
    ) -> None:
        """Change file group.

        Requires the user to be the owner of the file or an admin.

        Args:
            path: Virtual file path
            group: New group name
            context: Optional operation context (defaults to self._default_context)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user is not owner and not admin

        Examples:
            >>> nx.chgrp("/workspace/file.txt", "developers")
        """
        path = self._validate_path(path)

        # Get file metadata
        file_meta = self.metadata.get(path)
        if not file_meta:
            raise NexusFileNotFoundError(path)

        # Get context (use default if not provided)
        ctx = context or self._default_context

        # Check if user is owner or admin
        # Must be owner to chgrp (unless admin or system)
        if (
            not ctx.is_admin
            and not ctx.is_system
            and file_meta.owner
            and file_meta.owner != ctx.user
        ):
            raise PermissionError(
                f"Access denied: Only the owner ('{file_meta.owner}') or admin "
                f"can change group for '{path}'"
            )

        # Update group
        file_meta.group = group
        self.metadata.put(file_meta)

        # Invalidate cache
        if self.metadata._cache_enabled and self.metadata._cache:
            self.metadata._cache.invalidate_path(path)

    # ========================================================================
    # ACL (Access Control List) Methods
    # ========================================================================

    @rpc_expose(description="Grant permissions to a user via ACL")
    def grant_user(
        self,
        path: str,
        user: str,
        permissions: str,
        context: OperationContext | None = None,
    ) -> None:
        """Grant permissions to a user via ACL.

        Requires the user to be the owner of the file or an admin.

        Args:
            path: Virtual file path
            user: User identifier to grant permissions to
            permissions: Permission string in rwx format (e.g., 'rw-', 'r-x')
            context: Optional operation context (defaults to self._default_context)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user is not owner and not admin
            ValueError: If permissions string is invalid

        Examples:
            >>> nx.grant_user("/workspace/file.txt", user="alice", permissions="rw-")
            >>> nx.grant_user("/workspace/file.txt", user="bob", permissions="r--")
        """
        from sqlalchemy import delete

        from nexus.core.acl import ACLPermission
        from nexus.storage.models import ACLEntryModel

        path = self._validate_path(path)

        # Get file metadata
        file_meta = self.metadata.get(path)
        if not file_meta:
            raise NexusFileNotFoundError(path)

        # Get context (use default if not provided)
        ctx = context or self._default_context

        # Check if user is owner or admin
        if (
            not ctx.is_admin
            and not ctx.is_system
            and file_meta.owner
            and file_meta.owner != ctx.user
        ):
            raise PermissionError(
                f"Access denied: Only the owner ('{file_meta.owner}') or admin "
                f"can modify ACL for '{path}'"
            )

        # Parse permissions
        if len(permissions) != 3:
            raise ValueError(f"permissions must be 3 characters (rwx format), got '{permissions}'")

        acl_permissions: set[ACLPermission] = set()
        if permissions[0] == "r":
            acl_permissions.add(ACLPermission.READ)
        elif permissions[0] != "-":
            raise ValueError(f"invalid read permission: '{permissions[0]}'")

        if permissions[1] == "w":
            acl_permissions.add(ACLPermission.WRITE)
        elif permissions[1] != "-":
            raise ValueError(f"invalid write permission: '{permissions[1]}'")

        if permissions[2] == "x":
            acl_permissions.add(ACLPermission.EXECUTE)
        elif permissions[2] != "-":
            raise ValueError(f"invalid execute permission: '{permissions[2]}'")

        # Get path_id
        path_id = self.metadata.get_path_id(path)
        if not path_id:
            raise NexusFileNotFoundError(path)

        # Remove existing ACL entry for this user
        with self.metadata.SessionLocal() as session:
            stmt = delete(ACLEntryModel).where(
                ACLEntryModel.path_id == path_id,
                ACLEntryModel.entry_type == "user",
                ACLEntryModel.identifier == user,
            )
            session.execute(stmt)
            session.commit()

        # Add new ACL entry if permissions are non-empty
        if acl_permissions:
            with self.metadata.SessionLocal() as session:
                perms_str = permissions
                entry = ACLEntryModel(
                    path_id=path_id,
                    entry_type="user",
                    identifier=user,
                    permissions=perms_str,
                    deny=False,
                    is_default=False,
                    created_at=datetime.now(UTC),
                )
                session.add(entry)
                session.commit()

    @rpc_expose(description="Grant permissions to a group via ACL")
    def grant_group(
        self,
        path: str,
        group: str,
        permissions: str,
        context: OperationContext | None = None,
    ) -> None:
        """Grant permissions to a group via ACL.

        Requires the user to be the owner of the file or an admin.

        Args:
            path: Virtual file path
            group: Group identifier to grant permissions to
            permissions: Permission string in rwx format (e.g., 'rw-', 'r-x')
            context: Optional operation context (defaults to self._default_context)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user is not owner and not admin
            ValueError: If permissions string is invalid

        Examples:
            >>> nx.grant_group("/workspace/file.txt", group="developers", permissions="rw-")
            >>> nx.grant_group("/workspace/file.txt", group="viewers", permissions="r--")
        """
        from sqlalchemy import delete

        from nexus.core.acl import ACLPermission
        from nexus.storage.models import ACLEntryModel

        path = self._validate_path(path)

        # Get file metadata
        file_meta = self.metadata.get(path)
        if not file_meta:
            raise NexusFileNotFoundError(path)

        # Get context (use default if not provided)
        ctx = context or self._default_context

        # Check if user is owner or admin
        if (
            not ctx.is_admin
            and not ctx.is_system
            and file_meta.owner
            and file_meta.owner != ctx.user
        ):
            raise PermissionError(
                f"Access denied: Only the owner ('{file_meta.owner}') or admin "
                f"can modify ACL for '{path}'"
            )

        # Parse permissions
        if len(permissions) != 3:
            raise ValueError(f"permissions must be 3 characters (rwx format), got '{permissions}'")

        acl_permissions: set[ACLPermission] = set()
        if permissions[0] == "r":
            acl_permissions.add(ACLPermission.READ)
        elif permissions[0] != "-":
            raise ValueError(f"invalid read permission: '{permissions[0]}'")

        if permissions[1] == "w":
            acl_permissions.add(ACLPermission.WRITE)
        elif permissions[1] != "-":
            raise ValueError(f"invalid write permission: '{permissions[1]}'")

        if permissions[2] == "x":
            acl_permissions.add(ACLPermission.EXECUTE)
        elif permissions[2] != "-":
            raise ValueError(f"invalid execute permission: '{permissions[2]}'")

        # Get path_id
        path_id = self.metadata.get_path_id(path)
        if not path_id:
            raise NexusFileNotFoundError(path)

        # Remove existing ACL entry for this group
        with self.metadata.SessionLocal() as session:
            stmt = delete(ACLEntryModel).where(
                ACLEntryModel.path_id == path_id,
                ACLEntryModel.entry_type == "group",
                ACLEntryModel.identifier == group,
            )
            session.execute(stmt)
            session.commit()

        # Add new ACL entry if permissions are non-empty
        if acl_permissions:
            with self.metadata.SessionLocal() as session:
                perms_str = permissions
                entry = ACLEntryModel(
                    path_id=path_id,
                    entry_type="group",
                    identifier=group,
                    permissions=perms_str,
                    deny=False,
                    is_default=False,
                    created_at=datetime.now(UTC),
                )
                session.add(entry)
                session.commit()

    @rpc_expose(description="Explicitly deny user access via ACL")
    def deny_user(
        self,
        path: str,
        user: str,
        context: OperationContext | None = None,
    ) -> None:
        """Explicitly deny user access to file via ACL.

        Deny entries take precedence over all other permissions.
        Requires the user to be the owner of the file or an admin.

        Args:
            path: Virtual file path
            user: User identifier to deny access to
            context: Optional operation context (defaults to self._default_context)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user is not owner and not admin

        Examples:
            >>> nx.deny_user("/workspace/secret.txt", user="intern")
        """
        from sqlalchemy import delete

        from nexus.storage.models import ACLEntryModel

        path = self._validate_path(path)

        # Get file metadata
        file_meta = self.metadata.get(path)
        if not file_meta:
            raise NexusFileNotFoundError(path)

        # Get context (use default if not provided)
        ctx = context or self._default_context

        # Check if user is owner or admin
        if (
            not ctx.is_admin
            and not ctx.is_system
            and file_meta.owner
            and file_meta.owner != ctx.user
        ):
            raise PermissionError(
                f"Access denied: Only the owner ('{file_meta.owner}') or admin "
                f"can modify ACL for '{path}'"
            )

        # Get path_id
        path_id = self.metadata.get_path_id(path)
        if not path_id:
            raise NexusFileNotFoundError(path)

        # Remove existing ACL entry for this user
        with self.metadata.SessionLocal() as session:
            stmt = delete(ACLEntryModel).where(
                ACLEntryModel.path_id == path_id,
                ACLEntryModel.entry_type == "user",
                ACLEntryModel.identifier == user,
            )
            session.execute(stmt)
            session.commit()

        # Add deny entry
        with self.metadata.SessionLocal() as session:
            entry = ACLEntryModel(
                path_id=path_id,
                entry_type="user",
                identifier=user,
                permissions="---",  # No permissions
                deny=True,
                is_default=False,
                created_at=datetime.now(UTC),
            )
            session.add(entry)
            session.commit()

    @rpc_expose(description="Remove ACL entry for user or group")
    def revoke_acl(
        self,
        path: str,
        entry_type: str,
        identifier: str,
        context: OperationContext | None = None,
    ) -> None:
        """Remove ACL entry for user or group.

        Requires the user to be the owner of the file or an admin.

        Args:
            path: Virtual file path
            entry_type: Type of entry ('user' or 'group')
            identifier: User or group identifier
            context: Optional operation context (defaults to self._default_context)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user is not owner and not admin
            ValueError: If entry_type is invalid

        Examples:
            >>> nx.revoke_acl("/workspace/file.txt", "user", "alice")
            >>> nx.revoke_acl("/workspace/file.txt", "group", "developers")
        """
        from sqlalchemy import delete

        from nexus.storage.models import ACLEntryModel

        path = self._validate_path(path)

        if entry_type not in ("user", "group"):
            raise ValueError(f"entry_type must be 'user' or 'group', got '{entry_type}'")

        # Get file metadata
        file_meta = self.metadata.get(path)
        if not file_meta:
            raise NexusFileNotFoundError(path)

        # Get context (use default if not provided)
        ctx = context or self._default_context

        # Check if user is owner or admin
        if (
            not ctx.is_admin
            and not ctx.is_system
            and file_meta.owner
            and file_meta.owner != ctx.user
        ):
            raise PermissionError(
                f"Access denied: Only the owner ('{file_meta.owner}') or admin "
                f"can modify ACL for '{path}'"
            )

        # Get path_id
        path_id = self.metadata.get_path_id(path)
        if not path_id:
            raise NexusFileNotFoundError(path)

        # Remove ACL entry
        with self.metadata.SessionLocal() as session:
            stmt = delete(ACLEntryModel).where(
                ACLEntryModel.path_id == path_id,
                ACLEntryModel.entry_type == entry_type,
                ACLEntryModel.identifier == identifier,
            )
            session.execute(stmt)
            session.commit()

    @rpc_expose(description="Get ACL entries for a file")
    def get_acl(self, path: str) -> list[dict[str, str | bool | None]]:
        """Get ACL entries for a file.

        Args:
            path: Virtual file path

        Returns:
            List of ACL entry dictionaries with keys:
                - entry_type: 'user' or 'group'
                - identifier: User or group identifier
                - permissions: Permission string (e.g., 'rw-')
                - deny: True if this is a deny entry

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid

        Examples:
            >>> nx.get_acl("/workspace/file.txt")
            [
                {'entry_type': 'user', 'identifier': 'alice', 'permissions': 'rw-', 'deny': False},
                {'entry_type': 'user', 'identifier': 'bob', 'permissions': '---', 'deny': True}
            ]
        """
        from sqlalchemy import select

        from nexus.storage.models import ACLEntryModel

        path = self._validate_path(path)

        # Get file metadata
        file_meta = self.metadata.get(path)
        if not file_meta:
            raise NexusFileNotFoundError(path)

        # Get path_id
        path_id = self.metadata.get_path_id(path)
        if not path_id:
            return []

        # Query ACL entries
        with self.metadata.SessionLocal() as session:
            stmt = select(ACLEntryModel).where(ACLEntryModel.path_id == path_id)
            entries = session.scalars(stmt).all()

            result = []
            for entry in entries:
                result.append(
                    {
                        "entry_type": entry.entry_type,
                        "identifier": entry.identifier,
                        "permissions": entry.permissions,
                        "deny": entry.deny,
                    }
                )

            return result
