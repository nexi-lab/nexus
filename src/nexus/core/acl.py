"""Access Control List (ACL) support for Nexus.

This module implements POSIX-style Access Control Lists (ACLs) for fine-grained
permission management beyond traditional UNIX permissions.

ACL Model:
    - ACL entries can be added to files to grant/deny specific permissions
    - Supports user and group entries
    - Permissions: read, write, execute
    - Entries are evaluated in order:
        1. Explicit deny entries
        2. Explicit allow entries
        3. Fall back to UNIX permissions

ACL Entry Format:
    user:<username>:rwx    - Grant user specific permissions
    group:<groupname>:r-x  - Grant group specific permissions
    deny:user:<username>   - Explicitly deny user access

Example:
    # Grant alice read+write
    user:alice:rw-

    # Grant developers group read+execute
    group:developers:r-x

    # Deny bob access
    deny:user:bob
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ACLEntryType(str, Enum):
    """Type of ACL entry."""

    USER = "user"
    GROUP = "group"
    MASK = "mask"  # Maximum permissions for non-owner
    OTHER = "other"  # Default permissions for others


class ACLPermission(str, Enum):
    """ACL permission types."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


@dataclass
class ACLEntry:
    """Represents a single ACL entry.

    Attributes:
        entry_type: Type of ACL entry (user/group/mask/other)
        identifier: User or group identifier (None for mask/other)
        permissions: Set of granted permissions
        deny: Whether this is a deny entry (default: False)
        is_default: Whether this is a default ACL entry for inheritance (default: False)
    """

    entry_type: ACLEntryType
    identifier: str | None
    permissions: set[ACLPermission]
    deny: bool = False
    is_default: bool = False

    def __post_init__(self) -> None:
        """Validate ACL entry."""
        if self.entry_type in (ACLEntryType.USER, ACLEntryType.GROUP):
            if not self.identifier:
                raise ValueError(f"{self.entry_type} entry requires an identifier")
        elif (
            self.entry_type in (ACLEntryType.MASK, ACLEntryType.OTHER)
            and self.identifier is not None
        ):
            raise ValueError(f"{self.entry_type} entry cannot have identifier")

        if not isinstance(self.permissions, set):
            self.permissions = set(self.permissions)

        # Validate permissions
        for perm in self.permissions:
            if not isinstance(perm, ACLPermission):
                raise ValueError(f"invalid permission: {perm}")

    def has_permission(self, permission: ACLPermission) -> bool:
        """Check if entry grants a specific permission.

        Args:
            permission: Permission to check

        Returns:
            True if permission is granted
        """
        return permission in self.permissions

    def to_string(self) -> str:
        """Convert ACL entry to string format.

        Returns:
            String representation (e.g., 'user:alice:rw-' or 'default:user:alice:rw-')

        Examples:
            >>> entry = ACLEntry(ACLEntryType.USER, "alice", {ACLPermission.READ, ACLPermission.WRITE})
            >>> entry.to_string()
            'user:alice:rw-'
            >>> entry = ACLEntry(ACLEntryType.USER, "alice", {ACLPermission.READ}, is_default=True)
            >>> entry.to_string()
            'default:user:alice:r--'
        """
        # Build permission string
        r = "r" if ACLPermission.READ in self.permissions else "-"
        w = "w" if ACLPermission.WRITE in self.permissions else "-"
        x = "x" if ACLPermission.EXECUTE in self.permissions else "-"
        perms = f"{r}{w}{x}"

        # Build entry string with default and deny prefixes
        prefix = ""
        if self.is_default:
            prefix = "default:"
        if self.deny:
            prefix += "deny:"

        if self.identifier:
            return f"{prefix}{self.entry_type.value}:{self.identifier}:{perms}"
        return f"{prefix}{self.entry_type.value}:{perms}"

    @classmethod
    def from_string(cls, entry_str: str) -> ACLEntry:
        """Parse ACL entry from string format.

        Args:
            entry_str: String representation (e.g., 'user:alice:rw-' or 'default:user:alice:rw-')

        Returns:
            ACLEntry instance

        Raises:
            ValueError: If entry string is invalid

        Examples:
            >>> entry = ACLEntry.from_string('user:alice:rw-')
            >>> entry.entry_type
            <ACLEntryType.USER: 'user'>
            >>> entry.identifier
            'alice'
            >>> entry = ACLEntry.from_string('default:user:alice:rw-')
            >>> entry.is_default
            True
        """
        entry_str = entry_str.strip()

        # Check for default prefix
        is_default = False
        if entry_str.startswith("default:"):
            is_default = True
            entry_str = entry_str[8:]

        # Check for deny prefix
        deny = False
        if entry_str.startswith("deny:"):
            deny = True
            entry_str = entry_str[5:]

        parts = entry_str.split(":")
        if len(parts) < 2:
            raise ValueError(f"invalid ACL entry: {entry_str}")

        # Parse entry type
        try:
            entry_type = ACLEntryType(parts[0])
        except ValueError:
            raise ValueError(f"invalid ACL entry type: {parts[0]}") from None

        # Parse identifier and permissions
        if entry_type in (ACLEntryType.USER, ACLEntryType.GROUP):
            if len(parts) != 3:
                raise ValueError(
                    f"expected format {entry_type.value}:<name>:<perms>, got {entry_str}"
                )
            identifier = parts[1]
            perms_str = parts[2]
        else:
            if len(parts) != 2:
                raise ValueError(f"expected format {entry_type.value}:<perms>, got {entry_str}")
            identifier = None
            perms_str = parts[1]

        # Parse permissions
        if len(perms_str) != 3:
            raise ValueError(f"permission string must be 3 characters, got {perms_str}")

        permissions: set[ACLPermission] = set()
        if perms_str[0] == "r":
            permissions.add(ACLPermission.READ)
        elif perms_str[0] != "-":
            raise ValueError(f"invalid read permission: {perms_str[0]}")

        if perms_str[1] == "w":
            permissions.add(ACLPermission.WRITE)
        elif perms_str[1] != "-":
            raise ValueError(f"invalid write permission: {perms_str[1]}")

        if perms_str[2] == "x":
            permissions.add(ACLPermission.EXECUTE)
        elif perms_str[2] != "-":
            raise ValueError(f"invalid execute permission: {perms_str[2]}")

        return cls(
            entry_type=entry_type,
            identifier=identifier,
            permissions=permissions,
            deny=deny,
            is_default=is_default,
        )

    def __repr__(self) -> str:
        return f"ACLEntry({self.to_string()!r})"

    def __str__(self) -> str:
        return self.to_string()


@dataclass
class ACL:
    """Access Control List for a file.

    An ACL is an ordered list of ACL entries that define fine-grained
    permissions for users and groups.

    Evaluation order:
        1. Explicit deny entries (highest priority)
        2. Explicit allow entries
        3. Fall back to UNIX permissions if no ACL match

    Attributes:
        entries: List of ACL entries (evaluated in order)
    """

    entries: list[ACLEntry]

    def __post_init__(self) -> None:
        """Validate ACL."""
        if not isinstance(self.entries, list):
            self.entries = list(self.entries)

        # Validate all entries
        for entry in self.entries:
            if not isinstance(entry, ACLEntry):
                raise TypeError(f"ACL entries must be ACLEntry, got {type(entry)}")

    def check_permission(
        self, user: str, groups: list[str], permission: ACLPermission
    ) -> bool | None:
        """Check if user/group has permission via ACL.

        Returns:
            True if explicitly allowed
            False if explicitly denied
            None if no ACL match (fall back to UNIX permissions)

        Args:
            user: User ID
            groups: List of group IDs
            permission: Permission to check

        Examples:
            >>> acl = ACL([
            ...     ACLEntry(ACLEntryType.USER, "alice", {ACLPermission.READ}),
            ...     ACLEntry(ACLEntryType.GROUP, "devs", {ACLPermission.READ, ACLPermission.WRITE})
            ... ])
            >>> acl.check_permission("alice", [], ACLPermission.READ)
            True
            >>> acl.check_permission("alice", [], ACLPermission.WRITE)
            None  # No match, fall back to UNIX permissions
        """
        # First pass: Check for explicit denies
        for entry in self.entries:
            if not entry.deny:
                continue

            # Check user deny
            if entry.entry_type == ACLEntryType.USER and entry.identifier == user:
                return False

            # Check group deny
            if entry.entry_type == ACLEntryType.GROUP and entry.identifier in groups:
                return False

        # Second pass: Check for explicit allows
        for entry in self.entries:
            if entry.deny:
                continue

            # Check user allow
            if (
                entry.entry_type == ACLEntryType.USER
                and entry.identifier == user
                and entry.has_permission(permission)
            ):
                return True

            # Check group allow
            if (
                entry.entry_type == ACLEntryType.GROUP
                and entry.identifier in groups
                and entry.has_permission(permission)
            ):
                return True

        # No match - fall back to UNIX permissions
        return None

    def add_entry(self, entry: ACLEntry) -> None:
        """Add an ACL entry.

        Args:
            entry: ACL entry to add
        """
        if not isinstance(entry, ACLEntry):
            raise TypeError(f"entry must be ACLEntry, got {type(entry)}")
        self.entries.append(entry)

    def remove_entry(self, entry_type: ACLEntryType, identifier: str | None = None) -> bool:
        """Remove ACL entry by type and identifier.

        Args:
            entry_type: Type of entry to remove
            identifier: Identifier (for user/group entries)

        Returns:
            True if entry was removed, False if not found
        """
        original_len = len(self.entries)
        self.entries = [
            e
            for e in self.entries
            if not (e.entry_type == entry_type and e.identifier == identifier)
        ]
        return len(self.entries) < original_len

    def get_entries(
        self, entry_type: ACLEntryType | None = None, identifier: str | None = None
    ) -> list[ACLEntry]:
        """Get ACL entries matching criteria.

        Args:
            entry_type: Filter by entry type (optional)
            identifier: Filter by identifier (optional)

        Returns:
            List of matching ACL entries
        """
        entries = self.entries

        if entry_type is not None:
            entries = [e for e in entries if e.entry_type == entry_type]

        if identifier is not None:
            entries = [e for e in entries if e.identifier == identifier]

        return entries

    def get_default_entries(self) -> list[ACLEntry]:
        """Get all default ACL entries (for inheritance).

        Returns:
            List of default ACL entries
        """
        return [e for e in self.entries if e.is_default]

    def apply_default_entries_to_child(self) -> ACL:
        """Create a new ACL for a child file/directory by inheriting default entries.

        This creates a new ACL where:
        - Default entries from parent become regular entries for the child
        - Default entries are preserved for directories (so they can be inherited further)
        - Regular (non-default) entries are NOT inherited

        Returns:
            New ACL with inherited entries

        Example:
            Parent directory has: default:user:alice:rwx
            Child file inherits:  user:alice:rwx
            Child directory inherits: user:alice:rwx AND default:user:alice:rwx
        """
        default_entries = self.get_default_entries()
        child_entries = []

        for entry in default_entries:
            # Create regular entry for child (not marked as default)
            child_entry = ACLEntry(
                entry_type=entry.entry_type,
                identifier=entry.identifier,
                permissions=entry.permissions.copy(),
                deny=entry.deny,
                is_default=False,  # Child gets regular entry
            )
            child_entries.append(child_entry)

        return ACL(entries=child_entries)

    def to_strings(self) -> list[str]:
        """Convert ACL to list of strings.

        Returns:
            List of ACL entry strings

        Examples:
            >>> acl = ACL([
            ...     ACLEntry(ACLEntryType.USER, "alice", {ACLPermission.READ}),
            ...     ACLEntry(ACLEntryType.GROUP, "devs", {ACLPermission.WRITE})
            ... ])
            >>> acl.to_strings()
            ['user:alice:r--', 'group:devs:-w-']
        """
        return [entry.to_string() for entry in self.entries]

    @classmethod
    def from_strings(cls, entries: Sequence[str]) -> ACL:
        """Parse ACL from list of strings.

        Args:
            entries: List of ACL entry strings

        Returns:
            ACL instance

        Raises:
            ValueError: If any entry string is invalid

        Examples:
            >>> acl = ACL.from_strings(['user:alice:r--', 'group:devs:-w-'])
            >>> len(acl.entries)
            2
        """
        parsed_entries = [ACLEntry.from_string(e) for e in entries]
        return cls(entries=parsed_entries)

    @classmethod
    def empty(cls) -> ACL:
        """Create an empty ACL.

        Returns:
            Empty ACL instance
        """
        return cls(entries=[])

    def __repr__(self) -> str:
        return f"ACL({self.to_strings()!r})"

    def __str__(self) -> str:
        return "\n".join(self.to_strings())


class ACLManager:
    """Manager for ACL operations.

    This class provides high-level operations for managing ACLs,
    including setting, getting, and checking permissions.
    """

    def __init__(self) -> None:
        """Initialize ACL manager."""
        pass

    def grant_user(
        self, acl: ACL, user: str, read: bool = False, write: bool = False, execute: bool = False
    ) -> None:
        """Grant permissions to a user.

        Args:
            acl: ACL to modify
            user: User ID
            read: Grant read permission
            write: Grant write permission
            execute: Grant execute permission
        """
        permissions: set[ACLPermission] = set()
        if read:
            permissions.add(ACLPermission.READ)
        if write:
            permissions.add(ACLPermission.WRITE)
        if execute:
            permissions.add(ACLPermission.EXECUTE)

        # Remove existing entry for this user
        acl.remove_entry(ACLEntryType.USER, user)

        # Add new entry
        if permissions:
            entry = ACLEntry(entry_type=ACLEntryType.USER, identifier=user, permissions=permissions)
            acl.add_entry(entry)

    def grant_group(
        self, acl: ACL, group: str, read: bool = False, write: bool = False, execute: bool = False
    ) -> None:
        """Grant permissions to a group.

        Args:
            acl: ACL to modify
            group: Group ID
            read: Grant read permission
            write: Grant write permission
            execute: Grant execute permission
        """
        permissions: set[ACLPermission] = set()
        if read:
            permissions.add(ACLPermission.READ)
        if write:
            permissions.add(ACLPermission.WRITE)
        if execute:
            permissions.add(ACLPermission.EXECUTE)

        # Remove existing entry for this group
        acl.remove_entry(ACLEntryType.GROUP, group)

        # Add new entry
        if permissions:
            entry = ACLEntry(
                entry_type=ACLEntryType.GROUP, identifier=group, permissions=permissions
            )
            acl.add_entry(entry)

    def revoke_user(self, acl: ACL, user: str) -> bool:
        """Revoke all permissions for a user.

        Args:
            acl: ACL to modify
            user: User ID

        Returns:
            True if entry was removed
        """
        return acl.remove_entry(ACLEntryType.USER, user)

    def revoke_group(self, acl: ACL, group: str) -> bool:
        """Revoke all permissions for a group.

        Args:
            acl: ACL to modify
            group: Group ID

        Returns:
            True if entry was removed
        """
        return acl.remove_entry(ACLEntryType.GROUP, group)

    def deny_user(self, acl: ACL, user: str) -> None:
        """Explicitly deny user access.

        Args:
            acl: ACL to modify
            user: User ID
        """
        # Remove any allow entries
        acl.remove_entry(ACLEntryType.USER, user)

        # Add deny entry
        entry = ACLEntry(
            entry_type=ACLEntryType.USER, identifier=user, permissions=set(), deny=True
        )
        acl.add_entry(entry)

    def deny_group(self, acl: ACL, group: str) -> None:
        """Explicitly deny group access.

        Args:
            acl: ACL to modify
            group: Group ID
        """
        # Remove any allow entries
        acl.remove_entry(ACLEntryType.GROUP, group)

        # Add deny entry
        entry = ACLEntry(
            entry_type=ACLEntryType.GROUP, identifier=group, permissions=set(), deny=True
        )
        acl.add_entry(entry)


class ACLStore:
    """Storage layer for ACL entries.

    This class bridges between the in-memory ACL representation and the
    database storage layer. It retrieves ACL entries from the database
    and converts them to ACL objects for permission checking.
    """

    def __init__(self, metadata_store: Any):
        """Initialize ACL store.

        Args:
            metadata_store: Metadata store with database connection
        """
        self.metadata_store = metadata_store

    def get_acl(self, path: str) -> ACL | None:
        """Get ACL for a file path.

        Args:
            path: Virtual file path

        Returns:
            ACL object or None if no ACL entries exist

        Example:
            >>> store = ACLStore(metadata_store)
            >>> acl = store.get_acl("/workspace/file.txt")
            >>> if acl:
            ...     result = acl.check_permission("alice", ["developers"], ACLPermission.READ)
        """
        # Get path_id using metadata store's method
        try:
            path_id = self.metadata_store.get_path_id(path)
        except (AttributeError, Exception):
            # If get_path_id method doesn't exist or fails, try legacy approach
            meta = self.metadata_store.get(path)
            if not meta:
                return None
            path_id = getattr(meta, "path_id", None)

        if not path_id:
            return None

        # Query ACL entries from database
        from nexus.storage.models import ACLEntryModel

        with self.metadata_store.SessionLocal() as session:
            acl_entries = (
                session.query(ACLEntryModel)
                .filter(ACLEntryModel.path_id == path_id)
                .order_by(ACLEntryModel.created_at)
                .all()
            )

            if not acl_entries:
                return None

            # Convert database models to ACL entries
            entries = []
            for db_entry in acl_entries:
                # Parse entry_type
                try:
                    entry_type = ACLEntryType(db_entry.entry_type)
                except ValueError:
                    continue  # Skip invalid entry types

                # Parse permissions from string (rwx format)
                permissions: set[ACLPermission] = set()
                if len(db_entry.permissions) >= 3:
                    if db_entry.permissions[0] == "r":
                        permissions.add(ACLPermission.READ)
                    if db_entry.permissions[1] == "w":
                        permissions.add(ACLPermission.WRITE)
                    if db_entry.permissions[2] == "x":
                        permissions.add(ACLPermission.EXECUTE)

                entry = ACLEntry(
                    entry_type=entry_type,
                    identifier=db_entry.identifier,
                    permissions=permissions,
                    deny=db_entry.deny,
                    is_default=db_entry.is_default,
                )
                entries.append(entry)

            return ACL(entries=entries)

    def check_permission(
        self, path: str, user: str, groups: list[str], permission: ACLPermission
    ) -> bool | None:
        """Check if user has permission via ACL.

        This is a convenience method that gets the ACL and checks permission in one call.

        Args:
            path: Virtual file path
            user: User ID
            groups: List of group IDs
            permission: Permission to check

        Returns:
            True if explicitly allowed
            False if explicitly denied
            None if no ACL entries exist or no match

        Example:
            >>> store = ACLStore(metadata_store)
            >>> result = store.check_permission("/workspace/file.txt", "alice", ["developers"], ACLPermission.READ)
            >>> if result is True:
            ...     print("Explicitly allowed")
            >>> elif result is False:
            ...     print("Explicitly denied")
            >>> else:
            ...     print("Fall back to UNIX permissions")
        """
        acl = self.get_acl(path)
        if not acl:
            return None
        return acl.check_permission(user, groups, permission)
