"""Permission management operations for NexusFS.

This module contains file permission operations:
- chmod: Change file mode/permissions
- chown: Change file owner
- chgrp: Change file group
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.core.exceptions import NexusFileNotFoundError
from nexus.core.permissions import parse_mode

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
