"""Path routing for mapping virtual paths to storage backends."""

import posixpath
from dataclasses import dataclass

from nexus.core.backend import StorageBackend


@dataclass
class MountConfig:
    """Mount configuration for path routing."""

    mount_point: str  # Virtual path prefix, e.g., "/workspace"
    backend: StorageBackend  # Backend instance
    priority: int = 0  # For tie-breaking (higher = preferred)
    readonly: bool = False


@dataclass
class RouteResult:
    """Result of path routing."""

    backend: StorageBackend
    backend_path: str  # Path relative to backend root
    mount_point: str  # Matched mount point
    readonly: bool


class PathNotMountedError(Exception):
    """Raised when no mount exists for path."""

    pass


class PathRouter:
    """
    Route virtual paths to storage backends using mount table.

    Design Principles:
    1. **Longest Prefix Match**: Like IP routing - most specific mount wins
    2. **Mount Priority**: Explicit priority for overlapping mounts
    3. **Simplified for Embedded**: In-memory mounts (no database needed)

    Example Mounts:
        /workspace  → LocalFS (/var/nexus/workspace)
        /shared     → LocalFS (/var/nexus/shared)
        /external   → Could be S3, GDrive, etc.
    """

    def __init__(self) -> None:
        """Initialize path router with empty mount table."""
        self._mounts: list[MountConfig] = []

    def add_mount(
        self,
        mount_point: str,
        backend: StorageBackend,
        priority: int = 0,
        readonly: bool = False,
    ) -> None:
        """
        Add a mount to the router.

        Args:
            mount_point: Virtual path prefix (must start with /)
            backend: Backend instance to use for this mount
            priority: Priority for overlapping mounts (higher = preferred)
            readonly: Whether mount is readonly

        Raises:
            ValueError: If mount_point is invalid
        """
        mount_point = self._normalize_path(mount_point)

        mount = MountConfig(
            mount_point=mount_point, backend=backend, priority=priority, readonly=readonly
        )

        self._mounts.append(mount)

        # Sort mounts by priority (DESC) then by prefix length (DESC)
        self._mounts.sort(key=lambda m: (m.priority, len(m.mount_point)), reverse=True)

    def route(self, virtual_path: str) -> RouteResult:
        """
        Route virtual path to backend.

        Algorithm:
        1. Normalize path (remove trailing slashes, collapse //)
        2. Find longest matching prefix
        3. Strip mount_point prefix to get backend-relative path
        4. Return RouteResult

        Example:
            Input: "/workspace/data/file.txt"
            Mounts: [("/workspace", localfs)]
            Match: "/workspace"
            Backend Path: "data/file.txt"

        Args:
            virtual_path: Virtual path to route

        Returns:
            RouteResult with backend and relative path

        Raises:
            PathNotMountedError: No mount found for path
        """
        # Normalize path
        virtual_path = self._normalize_path(virtual_path)

        # Find longest matching prefix
        matched_mount = self._match_longest_prefix(virtual_path)
        if not matched_mount:
            raise PathNotMountedError(f"No mount found for path: {virtual_path}")

        # Strip prefix
        backend_path = self._strip_mount_prefix(virtual_path, matched_mount.mount_point)

        return RouteResult(
            backend=matched_mount.backend,
            backend_path=backend_path,
            mount_point=matched_mount.mount_point,
            readonly=matched_mount.readonly,
        )

    def _match_longest_prefix(self, virtual_path: str) -> MountConfig | None:
        """
        Find mount with longest matching prefix.

        Note: mounts already sorted by (priority DESC, prefix_length DESC)
        so first match is the winner.

        Args:
            virtual_path: Normalized virtual path

        Returns:
            MountConfig if match found, None otherwise
        """
        for mount in self._mounts:
            # Exact match
            if virtual_path == mount.mount_point:
                return mount

            # Prefix match - check that mount_point is a directory boundary
            # For "/workspace", it should match "/workspace/..." but not "/workspace2/..."
            if mount.mount_point == "/":
                # Root mount matches everything
                return mount
            elif virtual_path.startswith(mount.mount_point + "/"):
                return mount

        return None

    def _strip_mount_prefix(self, virtual_path: str, mount_point: str) -> str:
        """
        Strip mount prefix to get backend-relative path.

        Examples:
            ("/workspace/data/file.txt", "/workspace") → "data/file.txt"
            ("/workspace", "/workspace") → ""
            ("/shared/docs/report.pdf", "/shared") → "docs/report.pdf"
            ("/workspace/data/file.txt", "/") → "workspace/data/file.txt"

        Args:
            virtual_path: Full virtual path
            mount_point: Mount point prefix

        Returns:
            Backend-relative path
        """
        if virtual_path == mount_point:
            return ""

        # Special case for root mount
        if mount_point == "/":
            return virtual_path.lstrip("/")

        # Remove mount_point prefix and leading slash
        relative = virtual_path[len(mount_point) :].lstrip("/")
        return relative

    def _normalize_path(self, path: str) -> str:
        """
        Normalize virtual path.

        Rules:
        - Must start with /
        - Collapse multiple slashes (// -> /)
        - Remove trailing slash (except root /)
        - Resolve . and .. (security)

        Args:
            path: Path to normalize

        Returns:
            Normalized path

        Raises:
            ValueError: If path is invalid
        """
        # Ensure absolute
        if not path.startswith("/"):
            raise ValueError(f"Path must be absolute: {path}")

        # Normalize using posixpath
        normalized = posixpath.normpath(path)

        # Security: Prevent path traversal outside root
        if not normalized.startswith("/"):
            raise ValueError(f"Path traversal detected: {path}")

        return normalized
