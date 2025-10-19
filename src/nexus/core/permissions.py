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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


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
