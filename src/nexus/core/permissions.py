"""UNIX-style file permissions for Nexus.

This module implements UNIX-style file permissions (owner, group, mode)
similar to POSIX filesystems.

Permission Model:
    - Owner: User ID (string) who owns the file
    - Group: Group ID (string) for group access
    - Mode: 9-bit permission mask (rwxrwxrwx)
        - Owner permissions (rwx): bits 6-8
        - Group permissions (rwx): bits 3-5
        - Other permissions (rwx): bits 0-2

Permission Bits:
    - Read (r): 4
    - Write (w): 2
    - Execute (x): 1

Example Modes:
    - 0o755 (rwxr-xr-x): Owner full, group/others read+execute
    - 0o644 (rw-r--r--): Owner read+write, group/others read-only
    - 0o700 (rwx------): Owner full, no access for others
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.acl import ACLStore
    from nexus.core.rebac_manager import ReBACManager


class Permission(IntFlag):
    """Permission bits for UNIX-style permissions."""

    NONE = 0
    EXECUTE = 1  # x
    WRITE = 2  # w
    READ = 4  # r
    ALL = 7  # rwx


class FileMode:
    """UNIX-style file mode (permission bits).

    Mode is a 9-bit integer representing permissions:
    - Bits 6-8: Owner permissions (rwx)
    - Bits 3-5: Group permissions (rwx)
    - Bits 0-2: Other permissions (rwx)

    Examples:
        >>> mode = FileMode(0o755)  # rwxr-xr-x
        >>> mode.owner_can_read()
        True
        >>> mode.group_can_write()
        False
        >>> mode.to_string()
        'rwxr-xr-x'
    """

    def __init__(self, mode: int = 0o644):
        """Initialize file mode.

        Args:
            mode: Permission mode (default: 0o644 - rw-r--r--)
        """
        if not 0 <= mode <= 0o777:
            raise ValueError(f"mode must be between 0o000 and 0o777, got {oct(mode)}")
        self._mode = mode

    @property
    def mode(self) -> int:
        """Get the raw mode integer."""
        return self._mode

    @property
    def owner_perms(self) -> Permission:
        """Get owner permissions."""
        return Permission((self._mode >> 6) & 0o7)

    @property
    def group_perms(self) -> Permission:
        """Get group permissions."""
        return Permission((self._mode >> 3) & 0o7)

    @property
    def other_perms(self) -> Permission:
        """Get other permissions."""
        return Permission(self._mode & 0o7)

    def owner_can_read(self) -> bool:
        """Check if owner has read permission."""
        return bool(self.owner_perms & Permission.READ)

    def owner_can_write(self) -> bool:
        """Check if owner has write permission."""
        return bool(self.owner_perms & Permission.WRITE)

    def owner_can_execute(self) -> bool:
        """Check if owner has execute permission."""
        return bool(self.owner_perms & Permission.EXECUTE)

    def group_can_read(self) -> bool:
        """Check if group has read permission."""
        return bool(self.group_perms & Permission.READ)

    def group_can_write(self) -> bool:
        """Check if group has write permission."""
        return bool(self.group_perms & Permission.WRITE)

    def group_can_execute(self) -> bool:
        """Check if group has execute permission."""
        return bool(self.group_perms & Permission.EXECUTE)

    def other_can_read(self) -> bool:
        """Check if others have read permission."""
        return bool(self.other_perms & Permission.READ)

    def other_can_write(self) -> bool:
        """Check if others have write permission."""
        return bool(self.other_perms & Permission.WRITE)

    def other_can_execute(self) -> bool:
        """Check if others have execute permission."""
        return bool(self.other_perms & Permission.EXECUTE)

    def to_string(self) -> str:
        """Convert mode to string representation (e.g., 'rwxr-xr-x').

        Returns:
            String representation of permissions
        """

        def perms_to_str(perms: Permission) -> str:
            r = "r" if perms & Permission.READ else "-"
            w = "w" if perms & Permission.WRITE else "-"
            x = "x" if perms & Permission.EXECUTE else "-"
            return f"{r}{w}{x}"

        return (
            perms_to_str(self.owner_perms)
            + perms_to_str(self.group_perms)
            + perms_to_str(self.other_perms)
        )

    @classmethod
    def from_string(cls, mode_str: str) -> FileMode:
        """Parse mode from string representation (e.g., 'rwxr-xr-x').

        Args:
            mode_str: String representation (must be 9 chars)

        Returns:
            FileMode instance

        Raises:
            ValueError: If mode_str is invalid
        """
        if len(mode_str) != 9:
            raise ValueError(f"mode string must be 9 characters, got {len(mode_str)}")

        def str_to_perms(s: str) -> int:
            if len(s) != 3:
                raise ValueError("permission string must be 3 characters")
            r = 4 if s[0] == "r" else 0
            w = 2 if s[1] == "w" else 0
            x = 1 if s[2] == "x" else 0
            return r + w + x

        owner = str_to_perms(mode_str[0:3])
        group = str_to_perms(mode_str[3:6])
        other = str_to_perms(mode_str[6:9])

        mode = (owner << 6) | (group << 3) | other
        return cls(mode)

    def __repr__(self) -> str:
        return f"FileMode({oct(self._mode)})"

    def __str__(self) -> str:
        return self.to_string()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FileMode):
            return NotImplemented
        return self._mode == other._mode


@dataclass
class FilePermissions:
    """Complete file permission information.

    Attributes:
        owner: Owner user ID (string)
        group: Group ID (string)
        mode: File mode (permission bits)
    """

    owner: str
    group: str
    mode: FileMode

    def __post_init__(self) -> None:
        """Validate permissions."""
        if not self.owner:
            raise ValueError("owner is required")
        if not self.group:
            raise ValueError("group is required")
        if not isinstance(self.mode, FileMode):
            raise TypeError(f"mode must be FileMode, got {type(self.mode)}")

    def can_read(self, user: str, groups: list[str]) -> bool:
        """Check if user can read file.

        Args:
            user: User ID
            groups: List of group IDs user belongs to

        Returns:
            True if user has read permission
        """
        if user == self.owner:
            return self.mode.owner_can_read()
        if self.group in groups:
            return self.mode.group_can_read()
        return self.mode.other_can_read()

    def can_write(self, user: str, groups: list[str]) -> bool:
        """Check if user can write file.

        Args:
            user: User ID
            groups: List of group IDs user belongs to

        Returns:
            True if user has write permission
        """
        if user == self.owner:
            return self.mode.owner_can_write()
        if self.group in groups:
            return self.mode.group_can_write()
        return self.mode.other_can_write()

    def can_execute(self, user: str, groups: list[str]) -> bool:
        """Check if user can execute file.

        Args:
            user: User ID
            groups: List of group IDs user belongs to

        Returns:
            True if user has execute permission
        """
        if user == self.owner:
            return self.mode.owner_can_execute()
        if self.group in groups:
            return self.mode.group_can_execute()
        return self.mode.other_can_execute()

    @classmethod
    def default(cls, owner: str, group: str | None = None) -> FilePermissions:
        """Create default permissions.

        Args:
            owner: Owner user ID
            group: Group ID (defaults to owner if not provided)

        Returns:
            FilePermissions with mode 0o644 (rw-r--r--)
        """
        return cls(owner=owner, group=group or owner, mode=FileMode(0o644))

    @classmethod
    def default_directory(cls, owner: str, group: str | None = None) -> FilePermissions:
        """Create default directory permissions.

        Args:
            owner: Owner user ID
            group: Group ID (defaults to owner if not provided)

        Returns:
            FilePermissions with mode 0o755 (rwxr-xr-x)
        """
        return cls(owner=owner, group=group or owner, mode=FileMode(0o755))


class PermissionChecker:
    """Helper class for checking file permissions.

    This class provides methods to check if a user has permission
    to perform operations on files based on UNIX-style permissions.
    """

    def __init__(self, default_owner: str = "root", default_group: str = "root"):
        """Initialize permission checker.

        Args:
            default_owner: Default owner for new files
            default_group: Default group for new files
        """
        self.default_owner = default_owner
        self.default_group = default_group

    def check_read(self, perms: FilePermissions | None, user: str, groups: list[str]) -> bool:
        """Check if user can read file.

        Args:
            perms: File permissions (None = no permissions set, allow all)
            user: User ID
            groups: List of group IDs

        Returns:
            True if user has read permission
        """
        if perms is None:
            # No permissions set - allow (for backward compatibility)
            return True
        return perms.can_read(user, groups)

    def check_write(self, perms: FilePermissions | None, user: str, groups: list[str]) -> bool:
        """Check if user can write file.

        Args:
            perms: File permissions (None = no permissions set, allow all)
            user: User ID
            groups: List of group IDs

        Returns:
            True if user has write permission
        """
        if perms is None:
            # No permissions set - allow (for backward compatibility)
            return True
        return perms.can_write(user, groups)

    def check_execute(self, perms: FilePermissions | None, user: str, groups: list[str]) -> bool:
        """Check if user can execute file.

        Args:
            perms: File permissions (None = no permissions set, allow all)
            user: User ID
            groups: List of group IDs

        Returns:
            True if user has execute permission
        """
        if perms is None:
            # No permissions set - allow (for backward compatibility)
            return True
        return perms.can_execute(user, groups)

    def create_default_permissions(
        self, owner: str | None = None, group: str | None = None, is_directory: bool = False
    ) -> FilePermissions:
        """Create default permissions for a new file.

        Args:
            owner: Owner ID (defaults to default_owner)
            group: Group ID (defaults to default_group)
            is_directory: Whether the file is a directory

        Returns:
            FilePermissions with appropriate defaults
        """
        owner = owner or self.default_owner
        group = group or self.default_group

        if is_directory:
            return FilePermissions.default_directory(owner, group)
        return FilePermissions.default(owner, group)


class PermissionInheritance:
    """Helper class for permission inheritance from parent directories.

    This class implements automatic permission assignment for new files and directories
    based on the permissions of their parent directory.

    Features:
    - Inherits owner, group, and mode from parent directory
    - Clears execute bits for files (preserves for directories)
    - Works with both UNIX-style permissions and ACL entries
    """

    def inherit_from_parent(
        self, parent_permissions: FilePermissions, is_directory: bool
    ) -> FilePermissions:
        """Inherit permissions from parent directory.

        Args:
            parent_permissions: Permissions of the parent directory
            is_directory: Whether the new file is a directory

        Returns:
            FilePermissions for the new file/directory

        Examples:
            >>> parent = FilePermissions("alice", "developers", FileMode(0o755))
            >>> inherit = PermissionInheritance()
            >>> # For a file: clears execute bits
            >>> child_file = inherit.inherit_from_parent(parent, is_directory=False)
            >>> child_file.mode.mode
            420  # 0o644 (rw-r--r--)
            >>> # For a directory: keeps all bits
            >>> child_dir = inherit.inherit_from_parent(parent, is_directory=True)
            >>> child_dir.mode.mode
            493  # 0o755 (rwxr-xr-x)
        """
        # Inherit owner and group
        owner = parent_permissions.owner
        group = parent_permissions.group

        # Inherit mode, but clear execute bits for files
        parent_mode = parent_permissions.mode.mode

        # Directories keep all permission bits, files clear execute bits (mask out 0o111)
        mode = parent_mode if is_directory else parent_mode & ~0o111

        return FilePermissions(owner=owner, group=group, mode=FileMode(mode))


def parse_mode(mode_str: str) -> int:
    """Parse mode from string (octal or symbolic).

    Supports both octal (e.g., '755', '0755', '0o755') and
    symbolic (e.g., 'rwxr-xr-x') formats.

    Args:
        mode_str: Mode string

    Returns:
        Mode as integer

    Raises:
        ValueError: If mode string is invalid

    Examples:
        >>> parse_mode('755')
        493
        >>> parse_mode('0o755')
        493
        >>> parse_mode('rwxr-xr-x')
        493
    """
    mode_str = mode_str.strip()

    # Try symbolic format first (9 chars)
    if len(mode_str) == 9 and all(c in "rwx-" for c in mode_str):
        return FileMode.from_string(mode_str).mode

    # Try octal format
    try:
        # Remove '0o' or '0' prefix if present
        if mode_str.startswith("0o") or mode_str.startswith("0O"):
            mode_str = mode_str[2:]
        elif mode_str.startswith("0") and len(mode_str) > 1:
            mode_str = mode_str[1:]

        mode = int(mode_str, 8)
        if not 0 <= mode <= 0o777:
            raise ValueError(f"mode must be between 0 and 0o777, got {oct(mode)}")
        return mode
    except ValueError as e:
        raise ValueError(
            f"invalid mode string: {mode_str!r} "
            "(must be octal like '755' or symbolic like 'rwxr-xr-x')"
        ) from e


@dataclass
class OperationContext:
    """Context for file operations with user/agent information.

    This class carries authentication and authorization context through
    all filesystem operations to enable permission checking.

    Attributes:
        user: Username or agent ID performing the operation
        groups: List of group IDs the user belongs to
        is_admin: Whether the user has admin privileges (bypasses all checks)
        is_system: Whether this is a system operation (bypasses all checks)

    Examples:
        >>> # Regular user context
        >>> ctx = OperationContext(user="alice", groups=["developers"])
        >>> # Admin context
        >>> ctx = OperationContext(user="admin", groups=["admins"], is_admin=True)
        >>> # System context (bypasses all checks)
        >>> ctx = OperationContext(user="system", groups=[], is_system=True)
    """

    user: str
    groups: list[str]
    is_admin: bool = False
    is_system: bool = False

    def __post_init__(self) -> None:
        """Validate context."""
        if not self.user:
            raise ValueError("user is required")
        if not isinstance(self.groups, list):
            raise TypeError(f"groups must be list, got {type(self.groups)}")


class PermissionEnforcer:
    """Multi-layer permission enforcement for Nexus filesystem.

    Implements permission checking using three layers in order:
    1. ReBAC (Relationship-Based Access Control) - Check graph relationships
    2. ACL (Access Control Lists) - Check explicit allow/deny entries
    3. UNIX Permissions - Check owner/group/other mode bits

    The enforcer short-circuits on first match:
    - If ReBAC grants permission, allow
    - If ACL denies explicitly, deny
    - If ACL allows explicitly, allow
    - Fall back to UNIX permissions

    This class integrates with the metadata store, ACL store, and ReBAC store
    to provide unified permission checking across all layers.
    """

    def __init__(
        self,
        metadata_store: Any = None,
        acl_store: ACLStore | None = None,
        rebac_manager: ReBACManager | None = None,
    ):
        """Initialize permission enforcer.

        Args:
            metadata_store: Metadata store for file permissions
            acl_store: ACL store for access control lists
            rebac_manager: ReBAC manager for relationship-based permissions
        """
        self.metadata_store = metadata_store
        self.acl_store: ACLStore | None = acl_store
        self.rebac_manager: ReBACManager | None = rebac_manager

    def check(
        self,
        path: str,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check if user has permission to perform operation on file.

        Multi-layer check order:
        1. Admin/system bypass
        2. ReBAC relationship check
        3. ACL explicit deny/allow
        4. UNIX permission check
        5. Default deny (if no permissions set)

        Args:
            path: Virtual file path
            permission: Permission to check (READ, WRITE, EXECUTE)
            context: Operation context with user/group information

        Returns:
            True if permission is granted, False otherwise

        Examples:
            >>> enforcer = PermissionEnforcer(metadata_store, acl_store, rebac_manager)
            >>> ctx = OperationContext(user="alice", groups=["developers"])
            >>> enforcer.check("/workspace/file.txt", Permission.READ, ctx)
            True
        """
        # 1. Admin/system bypass
        if context.is_admin or context.is_system:
            return True

        # 2. ReBAC check (relationship-based permissions)
        if self.rebac_manager and self._check_rebac(path, permission, context):
            return True

        # 3. ACL check (explicit deny takes priority)
        if self.acl_store:
            acl_result = self._check_acl(path, permission, context)
            if acl_result is not None:
                return acl_result  # Explicit allow or deny

        # 4. UNIX permissions check
        return self._check_unix(path, permission, context)

    def _check_rebac(
        self,
        path: str,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check ReBAC relationships for permission.

        Args:
            path: Virtual file path
            permission: Permission to check
            context: Operation context

        Returns:
            True if ReBAC grants permission, False otherwise
        """
        if not self.rebac_manager:
            return False

        # Map Permission flags to string permission names
        permission_name: str
        if permission & Permission.READ:
            permission_name = "read"
        elif permission & Permission.WRITE:
            permission_name = "write"
        elif permission & Permission.EXECUTE:
            permission_name = "execute"
        else:
            # Unknown permission
            return False

        # Get file metadata to find the file entity ID
        if not self.metadata_store:
            return False

        meta = self.metadata_store.get(path)
        if not meta:
            return False

        # Try to get path_id - it may not be available on all metadata stores
        # If path_id is not available, skip ReBAC check (fall through to ACL/UNIX)
        path_id = getattr(meta, "path_id", None)
        if not path_id:
            return False

        # Check ReBAC permission
        # Subject: (user_type, user_id) - we'll use "agent" as the type
        # Object: (file_type, file_id) - we'll use "file" as the type with path_id
        return self.rebac_manager.rebac_check(
            subject=("agent", context.user),
            permission=permission_name,
            object=("file", path_id),
        )

    def _check_acl(
        self,
        path: str,
        permission: Permission,
        context: OperationContext,
    ) -> bool | None:
        """Check ACL entries for permission.

        Args:
            path: Virtual file path
            permission: Permission to check
            context: Operation context

        Returns:
            True if ACL explicitly allows
            False if ACL explicitly denies
            None if no ACL match (fall through to UNIX permissions)
        """
        if not self.acl_store:
            return None

        # Map Permission flags to ACLPermission
        from nexus.core.acl import ACLPermission

        acl_permission: ACLPermission
        if permission & Permission.READ:
            acl_permission = ACLPermission.READ
        elif permission & Permission.WRITE:
            acl_permission = ACLPermission.WRITE
        elif permission & Permission.EXECUTE:
            acl_permission = ACLPermission.EXECUTE
        else:
            # Unknown permission - no ACL match
            return None

        # Check ACL using the ACL store
        return self.acl_store.check_permission(path, context.user, context.groups, acl_permission)

    def _check_unix(
        self,
        path: str,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check UNIX permissions for file access.

        Args:
            path: Virtual file path
            permission: Permission to check
            context: Operation context

        Returns:
            True if UNIX permissions grant access, False otherwise
        """
        if not self.metadata_store:
            # No metadata store - allow for backward compatibility
            return True

        # Get file metadata
        meta = self.metadata_store.get(path)
        if not meta:
            # File doesn't exist - deny
            return False

        # Check if permissions are set
        if meta.owner is None or meta.group is None or meta.mode is None:
            # No permissions set - allow for backward compatibility
            return True

        # Create FilePermissions from metadata
        try:
            file_perms = FilePermissions(
                owner=meta.owner,
                group=meta.group,
                mode=FileMode(meta.mode),
            )
        except Exception:
            # Invalid permissions - deny
            return False

        # Check permission based on type
        if permission & Permission.READ:
            return file_perms.can_read(context.user, context.groups)
        elif permission & Permission.WRITE:
            return file_perms.can_write(context.user, context.groups)
        elif permission & Permission.EXECUTE:
            return file_perms.can_execute(context.user, context.groups)
        else:
            # Unknown permission - deny
            return False

    def filter_list(
        self,
        paths: list[str],
        context: OperationContext,
    ) -> list[str]:
        """Filter list of paths by read permission.

        This is used by list() operations to only return files
        the user has permission to read.

        Args:
            paths: List of file paths to filter
            context: Operation context

        Returns:
            Filtered list of paths user can read

        Examples:
            >>> enforcer = PermissionEnforcer(metadata_store)
            >>> ctx = OperationContext(user="alice", groups=["developers"])
            >>> all_paths = ["/file1.txt", "/file2.txt", "/secret.txt"]
            >>> enforcer.filter_list(all_paths, ctx)
            ["/file1.txt", "/file2.txt"]  # /secret.txt filtered out
        """
        # Admin/system sees all files
        if context.is_admin or context.is_system:
            return paths

        # Filter paths by read permission
        filtered = []
        for path in paths:
            if self.check(path, Permission.READ, context):
                filtered.append(path)
        return filtered
