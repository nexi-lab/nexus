"""Path routing for mapping virtual paths to storage backends."""

import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.backends.backend import Backend


@dataclass
class MountConfig:
    """Mount configuration for path routing."""

    mount_point: str  # Virtual path prefix, e.g., "/workspace"
    backend: "Backend"  # Backend instance
    priority: int = 0  # For tie-breaking (higher = preferred)
    readonly: bool = False


@dataclass
class RouteResult:
    """Result of path routing."""

    backend: "Backend"
    backend_path: str  # Path relative to backend root
    mount_point: str  # Matched mount point
    readonly: bool


@dataclass
class PathInfo:
    """Parsed path information with namespace and tenant details."""

    namespace: str  # e.g., "workspace", "shared", "external", "system", "archives"
    tenant_id: str | None  # Tenant identifier (if applicable)
    agent_id: str | None  # Agent identifier (for workspace namespace)
    relative_path: str  # Remaining path after namespace/tenant/agent


@dataclass
class NamespaceConfig:
    """Configuration for a namespace."""

    name: str  # Namespace name (e.g., "workspace", "shared")
    readonly: bool = False  # Whether namespace is read-only
    admin_only: bool = False  # Whether namespace requires admin access
    requires_tenant: bool = True  # Whether namespace requires tenant isolation


class PathNotMountedError(Exception):
    """Raised when no mount exists for path."""

    pass


class InvalidPathError(Exception):
    """Raised when path is invalid or contains security issues."""

    pass


class AccessDeniedError(Exception):
    """Raised when access to path is denied."""

    pass


class PathRouter:
    """
    Route virtual paths to storage backends using mount table.

    Design Principles:
    1. **Longest Prefix Match**: Like IP routing - most specific mount wins
    2. **Mount Priority**: Explicit priority for overlapping mounts
    3. **Namespace Awareness**: Understands /workspace, /shared, /external, etc.
    4. **Tenant Isolation**: Enforces access control based on tenant/agent identity

    Example Mounts:
        /workspace  → LocalFS (/var/nexus/workspace)
        /shared     → LocalFS (/var/nexus/shared)
        /external   → Could be S3, GDrive, etc.
    """

    def __init__(self) -> None:
        """Initialize path router with empty mount table and default namespaces."""
        self._mounts: list[MountConfig] = []
        self._namespaces: dict[str, NamespaceConfig] = {}

        # Register default namespaces
        self._register_default_namespaces()

    def add_mount(
        self,
        mount_point: str,
        backend: "Backend",
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

    def route(
        self,
        virtual_path: str,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        is_admin: bool = False,
        check_write: bool = False,
    ) -> RouteResult:
        """
        Route virtual path to backend with access control.

        Algorithm:
        1. Normalize path (remove trailing slashes, collapse //)
        2. Check access control (namespace permissions, tenant/agent isolation)
        3. Find longest matching prefix
        4. Strip mount_point prefix to get backend-relative path
        5. Return RouteResult

        Example:
            Input: "/workspace/acme/agent1/data/file.txt"
            Tenant: "acme", Agent: "agent1"
            Mounts: [("/workspace", localfs)]
            Match: "/workspace"
            Backend Path: "acme/agent1/data/file.txt"

        Args:
            virtual_path: Virtual path to route
            tenant_id: Current tenant identifier (for access control)
            agent_id: Current agent identifier (for workspace isolation)
            is_admin: Whether requester has admin privileges
            check_write: Whether to check write permissions

        Returns:
            RouteResult with backend and relative path

        Raises:
            PathNotMountedError: No mount found for path
            AccessDeniedError: Access denied based on namespace rules
            InvalidPathError: Path validation failed
        """
        # Normalize and validate path
        virtual_path = self.validate_path(virtual_path)

        # Parse path to extract namespace and tenant info
        path_info = self.parse_path(virtual_path)

        # Check access control
        self._check_access(path_info, tenant_id, agent_id, is_admin, check_write)

        # Find longest matching prefix
        matched_mount = self._match_longest_prefix(virtual_path)
        if not matched_mount:
            raise PathNotMountedError(f"No mount found for path: {virtual_path}")

        # Strip prefix
        backend_path = self._strip_mount_prefix(virtual_path, matched_mount.mount_point)

        # Determine if readonly (from mount or namespace)
        readonly = matched_mount.readonly
        if path_info.namespace in self._namespaces:
            ns_config = self._namespaces[path_info.namespace]
            readonly = readonly or ns_config.readonly

        return RouteResult(
            backend=matched_mount.backend,
            backend_path=backend_path,
            mount_point=matched_mount.mount_point,
            readonly=readonly,
        )

    def _check_access(
        self,
        path_info: PathInfo,
        tenant_id: str | None,
        agent_id: str | None,
        is_admin: bool,
        check_write: bool,
    ) -> None:
        """
        Check access control for a path.

        Args:
            path_info: Parsed path information
            tenant_id: Current tenant identifier
            agent_id: Current agent identifier
            is_admin: Whether requester has admin privileges
            check_write: Whether to check write permissions

        Raises:
            AccessDeniedError: If access is denied
        """
        # Get namespace config
        if path_info.namespace not in self._namespaces:
            # Unknown namespace - allow access
            return

        ns_config = self._namespaces[path_info.namespace]

        # Check admin-only namespaces
        if ns_config.admin_only and not is_admin:
            raise AccessDeniedError(f"Namespace '{path_info.namespace}' requires admin privileges")

        # Check write access to read-only namespaces
        if ns_config.readonly and check_write:
            raise AccessDeniedError(f"Namespace '{path_info.namespace}' is read-only")

        # Check tenant isolation (only if path contains tenant_id)
        if (
            ns_config.requires_tenant
            and path_info.tenant_id
            and not is_admin
            and tenant_id
            and path_info.tenant_id != tenant_id
        ):
            raise AccessDeniedError(
                f"Access denied: tenant '{tenant_id}' cannot access "
                f"tenant '{path_info.tenant_id}' resources"
            )

        # Check agent isolation for workspace namespace (only if path contains agent_id)
        if (
            path_info.namespace == "workspace"
            and path_info.agent_id
            and not is_admin
            and agent_id
            and path_info.agent_id != agent_id
        ):
            raise AccessDeniedError(
                f"Access denied: agent '{agent_id}' cannot access "
                f"agent '{path_info.agent_id}' workspace"
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

    def _register_default_namespaces(self) -> None:
        """Register default namespace configurations."""
        # Workspace - Agent scratch space (ephemeral, full access for agent)
        self.register_namespace(
            NamespaceConfig(
                name="workspace", readonly=False, admin_only=False, requires_tenant=True
            )
        )

        # Shared - Shared tenant data (persistent, tenant-wide access)
        self.register_namespace(
            NamespaceConfig(name="shared", readonly=False, admin_only=False, requires_tenant=True)
        )

        # External - Pass-through backends (no special restrictions)
        self.register_namespace(
            NamespaceConfig(
                name="external", readonly=False, admin_only=False, requires_tenant=False
            )
        )

        # System - System metadata (admin-only, immutable)
        self.register_namespace(
            NamespaceConfig(name="system", readonly=True, admin_only=True, requires_tenant=False)
        )

        # Archives - Cold storage (read-only)
        self.register_namespace(
            NamespaceConfig(name="archives", readonly=True, admin_only=False, requires_tenant=True)
        )

    def register_namespace(self, config: NamespaceConfig) -> None:
        """
        Register a namespace configuration.

        Args:
            config: Namespace configuration
        """
        self._namespaces[config.name] = config

    def validate_path(self, path: str) -> str:
        """
        Validate path format and check for security issues.

        Rules:
        - Must start with /
        - No null bytes or control characters
        - No path traversal (..)
        - Valid namespace (if configured)

        Args:
            path: Path to validate

        Returns:
            Normalized path

        Raises:
            InvalidPathError: If path is invalid or has security issues
        """
        # Ensure absolute path
        if not path.startswith("/"):
            raise InvalidPathError(f"Path must be absolute: {path}")

        # Check for null bytes
        if "\0" in path:
            raise InvalidPathError("Path contains null byte")

        # Check for control characters
        if any(ord(c) < 32 for c in path if c not in ("\t", "\n")):
            raise InvalidPathError("Path contains control characters")

        # Check for .. segments (path traversal attempts)
        if ".." in path:
            raise InvalidPathError(f"Path traversal detected: {path}")

        # Normalize first
        normalized = self._normalize_path(path)

        # After normalization, check for path traversal
        # If normalized path doesn't start with /, it escaped root
        if not normalized.startswith("/"):
            raise InvalidPathError(f"Path traversal detected: {path}")

        return normalized

    def parse_path(self, path: str) -> PathInfo:
        """
        Parse virtual path to extract namespace, tenant, and agent information.

        Supported formats:
        - /workspace/{tenant}/{agent}/{path}  → workspace namespace
        - /shared/{tenant}/{path}             → shared namespace
        - /external/{backend}/{path}          → external namespace
        - /system/{path}                      → system namespace
        - /archives/{tenant}/{path}           → archives namespace

        Args:
            path: Virtual path to parse (must be normalized)

        Returns:
            PathInfo with extracted components

        Raises:
            InvalidPathError: If path format is invalid
        """
        # Normalize path first
        path = self._normalize_path(path)

        # Split path into components
        parts = path.lstrip("/").split("/")

        if not parts or parts[0] == "":
            raise InvalidPathError("Cannot parse root path")

        namespace = parts[0]

        # Check if namespace is registered
        if namespace not in self._namespaces:
            # If no specific namespace, treat first component as namespace
            # This allows for dynamic namespaces
            return PathInfo(
                namespace=namespace,
                tenant_id=None,
                agent_id=None,
                relative_path="/".join(parts[1:]) if len(parts) > 1 else "",
            )

        # Get namespace config
        ns_config = self._namespaces[namespace]

        # Parse based on namespace type
        if namespace == "workspace":
            # Format: /workspace/{tenant}/{agent}/{path}
            # Allow partial paths for directory creation (e.g., /workspace, /workspace/tenant)
            if len(parts) >= 3:
                return PathInfo(
                    namespace=namespace,
                    tenant_id=parts[1],
                    agent_id=parts[2],
                    relative_path="/".join(parts[3:]) if len(parts) > 3 else "",
                )
            elif len(parts) == 2:
                # /workspace/{tenant} - has tenant but no agent
                return PathInfo(
                    namespace=namespace,
                    tenant_id=parts[1],
                    agent_id=None,
                    relative_path="",
                )
            else:
                # /workspace - just the namespace root
                return PathInfo(
                    namespace=namespace,
                    tenant_id=None,
                    agent_id=None,
                    relative_path="",
                )

        elif namespace in ("shared", "archives"):
            # Format: /shared/{tenant}/{path} or /archives/{tenant}/{path}
            # Allow partial paths for directory creation
            if len(parts) >= 2:
                return PathInfo(
                    namespace=namespace,
                    tenant_id=parts[1],
                    agent_id=None,
                    relative_path="/".join(parts[2:]) if len(parts) > 2 else "",
                )
            else:
                # Just the namespace root
                return PathInfo(
                    namespace=namespace,
                    tenant_id=None,
                    agent_id=None,
                    relative_path="",
                )

        elif namespace in ("external", "system"):
            # Format: /external/{path} or /system/{path}
            return PathInfo(
                namespace=namespace,
                tenant_id=None,
                agent_id=None,
                relative_path="/".join(parts[1:]) if len(parts) > 1 else "",
            )

        else:
            # Custom namespace - check config for tenant requirement
            if ns_config.requires_tenant:
                # Format: /{namespace}/{tenant}/{path}
                # Similar to shared/archives
                if len(parts) >= 2:
                    return PathInfo(
                        namespace=namespace,
                        tenant_id=parts[1],
                        agent_id=None,
                        relative_path="/".join(parts[2:]) if len(parts) > 2 else "",
                    )
                else:
                    # Just the namespace root
                    return PathInfo(
                        namespace=namespace,
                        tenant_id=None,
                        agent_id=None,
                        relative_path="",
                    )
            else:
                # No tenant isolation required
                return PathInfo(
                    namespace=namespace,
                    tenant_id=None,
                    agent_id=None,
                    relative_path="/".join(parts[1:]) if len(parts) > 1 else "",
                )

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
