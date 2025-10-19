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
    """

    entry_type: ACLEntryType
    identifier: str | None
    permissions: set[ACLPermission]
    deny: bool = False

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
            String representation (e.g., 'user:alice:rw-')

        Examples:
            >>> entry = ACLEntry(ACLEntryType.USER, "alice", {ACLPermission.READ, ACLPermission.WRITE})
            >>> entry.to_string()
            'user:alice:rw-'
        """
        # Build permission string
        r = "r" if ACLPermission.READ in self.permissions else "-"
        w = "w" if ACLPermission.WRITE in self.permissions else "-"
        x = "x" if ACLPermission.EXECUTE in self.permissions else "-"
        perms = f"{r}{w}{x}"

        # Build entry string
        prefix = "deny:" if self.deny else ""
        if self.identifier:
            return f"{prefix}{self.entry_type.value}:{self.identifier}:{perms}"
        return f"{prefix}{self.entry_type.value}:{perms}"

    @classmethod
    def from_string(cls, entry_str: str) -> ACLEntry:
        """Parse ACL entry from string format.

        Args:
            entry_str: String representation (e.g., 'user:alice:rw-')

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
        """
        entry_str = entry_str.strip()

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

        return cls(entry_type=entry_type, identifier=identifier, permissions=permissions, deny=deny)

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
