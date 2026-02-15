"""ReBAC permission enforcement for Nexus (v0.6.0+).

This module implements pure ReBAC (Relationship-Based Access Control)
based on Google Zanzibar principles. All UNIX-style permission classes
have been removed as of v0.6.0.

Permission Model:
    - Subject: (type, id) tuple (e.g., ("user", "alice"), ("agent", "bot"))
    - Relation: Direct relations (direct_owner, direct_editor, direct_viewer)
    - Object: (type, id) tuple (e.g., ("file", "/path"), ("workspace", "ws1"))
    - Permission: Computed from relations (read, write, execute)

All permissions are now managed through ReBAC relationships.
Use rebac_create() to grant permissions instead of chmod/chown.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import IntFlag
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.read_set import ReadSet
    from nexus.services.permissions.hotspot_detector import HotspotDetector
    from nexus.services.permissions.namespace_manager import NamespaceManager
    from nexus.services.permissions.permission_boundary_cache import PermissionBoundaryCache
    from nexus.services.permissions.permissions_enhanced import AuditStore
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager

logger = logging.getLogger(__name__)


class Permission(IntFlag):
    """Permission flags for file operations.

    Note: These are still IntFlag for backward compatibility with
    bit operations, but they map to ReBAC permissions:
    - READ → "read" permission
    - WRITE → "write" permission
    - EXECUTE → "execute" permission
    - TRAVERSE → "traverse" permission (can stat/access by name, but not list contents)

    TRAVERSE is similar to Unix execute permission on directories - it allows
    accessing a path by name without the ability to list its contents.
    This enables O(1) permission checks for path traversal in FUSE operations.
    """

    NONE = 0
    EXECUTE = 1  # x
    WRITE = 2  # w
    READ = 4  # r
    TRAVERSE = 8  # t - can traverse/stat but not list (like Unix x on directories)
    ALL = 7  # rwx (does not include TRAVERSE by default)
    ALL_WITH_TRAVERSE = 15  # rwxt


@dataclass
class OperationContext:
    """Context for file operations with subject identity (v0.5.0).

    This class carries authentication and authorization context through
    all filesystem operations to enable permission checking.

    v0.5.0 ACE: Unified agent identity system
    - user_id: Human owner (always tracked)
    - agent_id: Agent identity (optional)
    - subject_type: "user" or "agent" (for authentication)
    - subject_id: Actual identity (user_id or agent_id)

    Agent lifecycle managed via API key TTL (no agent_type field needed).

    Subject-based identity supports:
    - user: Human users (alice, bob)
    - agent: AI agents (claude_001, gpt4_agent)
    - service: Backend services (backup_service, indexer)
    - session: Temporary sessions (session_abc123)

    Attributes:
        user: Subject ID performing the operation (LEGACY: use user_id)
        user_id: Human owner ID (v0.5.0: NEW, for explicit tracking)
        agent_id: Agent ID if operation is from agent (optional)
        subject_type: Type of subject (user, agent, service, session)
        subject_id: Unique identifier for the subject
        groups: List of group IDs the subject belongs to
        zone_id: Zone/organization ID for multi-zone isolation (optional)
        is_admin: Whether the subject has admin privileges
        is_system: Whether this is a system operation (bypasses all checks)
        admin_capabilities: Set of granted admin capabilities (P0-4)
        request_id: Unique ID for audit trail correlation (P0-4)
        backend_path: Backend-relative path for connector backends (optional)

    Examples:
        >>> # Human user context
        >>> ctx = OperationContext(
        ...     user="alice",
        ...     groups=["developers"],
        ...     zone_id="org_acme"
        ... )
        >>> # User-authenticated agent (uses user's auth)
        >>> ctx = OperationContext(
        ...     user="alice",
        ...     agent_id="notebook_xyz",
        ...     subject_type="user",  # Authenticates as user
        ...     groups=[]
        ... )
        >>> # Agent-authenticated (has own API key)
        >>> ctx = OperationContext(
        ...     user="alice",
        ...     agent_id="agent_data_analyst",
        ...     subject_type="agent",  # Authenticates as agent
        ...     subject_id="agent_data_analyst",
        ...     groups=[]
        ... )
    """

    user: str  # LEGACY: Kept for backward compatibility (maps to user_id)
    groups: list[str]
    zone_id: str | None = None
    agent_id: str | None = None  # Agent identity (optional)
    agent_generation: int | None = None  # Session generation counter (Issue #1240)
    is_admin: bool = False
    is_system: bool = False

    # v0.5.0 ACE: Unified agent identity
    user_id: str | None = None  # NEW: Human owner (auto-populated from user if None)

    # P0-2: Subject-based identity
    subject_type: str = "user"  # Default to "user" for backward compatibility
    subject_id: str | None = None  # If None, uses self.user

    # P0-4: Admin capabilities and audit trail
    admin_capabilities: set[str] = field(default_factory=set)  # Scoped admin capabilities
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # Audit trail correlation ID

    # Backend path for path-based connectors (GCS, S3, etc.)
    backend_path: str | None = None  # Backend-relative path for connector backends
    virtual_path: str | None = None  # Full virtual path with mount prefix (for cache keys)

    # Issue #1166: Read Set Tracking for Query Dependencies
    # When track_reads=True, operations automatically record what they read
    # to enable precise cache invalidation and efficient subscription updates
    read_set: ReadSet | None = None  # Read set for this operation (lazy-initialized)
    track_reads: bool = False  # Enable read tracking for this operation

    def __post_init__(self) -> None:
        """Validate context and apply defaults."""
        # v0.5.0: Auto-populate user_id from user if not provided
        if self.user_id is None:
            self.user_id = self.user

        # P0-2: If subject_id not provided, use user field for backward compatibility
        if self.subject_id is None:
            self.subject_id = self.user

        if not self.user:
            raise ValueError("user is required")
        if not isinstance(self.groups, list):
            raise TypeError(f"groups must be list, got {type(self.groups)}")

    def get_subject(self) -> tuple[str, str]:
        """Get subject as (type, id) tuple for ReBAC.

        Returns properly typed subject for permission checking.

        Returns:
            Tuple of (subject_type, subject_id)

        Example:
            >>> ctx = OperationContext(user="alice", groups=[])
            >>> ctx.get_subject()
            ('user', 'alice')
            >>> ctx = OperationContext(
            ...     user="alice",
            ...     agent_id="agent_data_analyst",
            ...     subject_type="agent",
            ...     subject_id="agent_data_analyst",
            ...     groups=[]
            ... )
            >>> ctx.get_subject()
            ('agent', 'agent_data_analyst')
        """
        return (self.subject_type, self.subject_id or self.user)

    def record_read(
        self,
        resource_type: str,
        resource_id: str,
        revision: int,
        access_type: str = "content",
    ) -> None:
        """Record a resource read for dependency tracking (Issue #1166).

        This method is called automatically by instrumented operations
        (read, list, stat) when track_reads=True.

        Args:
            resource_type: Type of resource (file, directory, metadata)
            resource_id: Path or identifier of the resource
            revision: Current revision of the resource
            access_type: Type of access (content, metadata, list, exists)

        Example:
            >>> ctx = OperationContext(user="alice", groups=[], track_reads=True)
            >>> ctx.enable_read_tracking("zone1")
            >>> ctx.record_read("file", "/inbox/a.txt", revision=10)
            >>> len(ctx.read_set)
            1
        """
        if not self.track_reads or self.read_set is None:
            return

        self.read_set.record_read(
            resource_type=resource_type,
            resource_id=resource_id,
            revision=revision,
            access_type=access_type,
        )

    def enable_read_tracking(self, zone_id: str | None = None) -> None:
        """Enable read tracking and initialize read set (Issue #1166).

        Call this before operations to track what resources are accessed.
        After the operation completes, the read_set can be registered
        with the ReadSetRegistry for subscription updates.

        Args:
            zone_id: Zone ID for the read set (defaults to self.zone_id)

        Example:
            >>> ctx = OperationContext(user="alice", groups=[], zone_id="org1")
            >>> ctx.enable_read_tracking()
            >>> # ... perform operations ...
            >>> registry.register(ctx.read_set)
        """
        from nexus.core.read_set import ReadSet

        self.track_reads = True
        self.read_set = ReadSet.create(zone_id=zone_id or self.zone_id or "default")

    def disable_read_tracking(self) -> None:
        """Disable read tracking.

        The read_set is preserved so it can still be registered/inspected.
        """
        self.track_reads = False


def check_stale_session(agent_registry: Any, context: OperationContext) -> None:
    """Check for stale agent sessions and raise if the session is outdated.

    Compares the agent_generation from the JWT token (stored in context) against
    the current generation in the agent registry (DB). A mismatch means a newer
    session has superseded this one.

    Issue #1240 / #1445: Shared helper used by both sync and async enforcers.

    Args:
        agent_registry: AgentRegistry instance (or None to skip check).
        context: Operation context with agent_generation from JWT claims.

    Raises:
        StaleSessionError: If the session generation is stale or the agent
            record no longer exists (deleted agent with valid JWT).
    """
    if (
        agent_registry is None
        or context.agent_generation is None
        or context.subject_type != "agent"
    ):
        return

    agent_id = context.agent_id or context.subject_id
    if not agent_id:
        logger.warning("[STALE-SESSION] No agent_id in context, skipping check")
        return

    current_record = agent_registry.get(agent_id)

    from nexus.core.exceptions import StaleSessionError

    # Issue #1445: Agent deleted but JWT still valid → stale session
    if current_record is None:
        raise StaleSessionError(
            agent_id,
            f"Agent '{agent_id}' no longer exists (session generation "
            f"{context.agent_generation} is stale)",
        )

    if current_record.generation != context.agent_generation:
        raise StaleSessionError(
            agent_id,
            f"Session generation {context.agent_generation} is stale "
            f"(current: {current_record.generation})",
        )


class PermissionEnforcer:
    """Pure ReBAC permission enforcement for Nexus filesystem (v0.6.0+).

    Implements permission checking using ReBAC (Relationship-Based Access Control)
    based on Google Zanzibar principles.

    Permission checks:
    1. Admin/system bypass - Scoped bypass with capabilities and audit logging (P0-4)
    2. ReBAC relationship check - Check permission graph for relationships

    P0-4 Features:
    - Scoped admin bypass (requires capabilities)
    - System bypass limited to /system paths (except read)
    - Audit logging for all bypasses
    - Kill-switch to disable bypasses
    - Path-based allowlist for admin bypass

    Migration from v0.5.x:
        - ACL and UNIX permissions have been removed
        - All permissions must be defined as ReBAC relationships
        - Use rebac_create() to grant permissions instead of chmod/setfacl
    """

    def __init__(
        self,
        metadata_store: Any = None,
        acl_store: Any | None = None,  # Deprecated, kept for backward compatibility
        rebac_manager: EnhancedReBACManager | None = None,
        entity_registry: Any = None,  # Entity registry (reserved for future use)
        router: Any = None,  # PathRouter for backend object type resolution
        # P0-4: Enhanced features
        allow_admin_bypass: bool = False,  # P0-4: Kill-switch DEFAULT OFF for production security
        allow_system_bypass: bool = True,  # P0-4: System bypass still enabled (for service operations)
        audit_store: AuditStore | None = None,  # P0-4: Audit logging
        admin_bypass_paths: list[str] | None = None,  # P0-4: Scoped bypass (allowlist)
        # Issue #922: Permission boundary cache for O(1) inheritance checks
        boundary_cache: PermissionBoundaryCache | None = None,
        enable_boundary_cache: bool = True,
        # Issue #921: Hotspot detection for proactive cache prefetching
        hotspot_detector: HotspotDetector | None = None,
        enable_hotspot_tracking: bool = True,
        # Issue #1239: Per-subject namespace visibility (Agent OS Phase 0)
        namespace_manager: NamespaceManager | None = None,
        # Issue #1240: Agent registry for stale-session detection (Agent OS Phase 1)
        agent_registry: Any = None,
    ):
        """Initialize permission enforcer.

        Args:
            metadata_store: Metadata store for file lookup (optional)
            acl_store: Deprecated, ignored (kept for backward compatibility)
            rebac_manager: ReBAC manager for relationship-based permissions
            entity_registry: Entity registry (reserved for future use)
            router: PathRouter for resolving backend object types (v0.5.0+)
            allow_admin_bypass: Enable admin bypass (DEFAULT: False for security)
            allow_system_bypass: Enable system bypass (for internal operations)
            audit_store: Audit store for bypass logging
            admin_bypass_paths: Optional path allowlist for admin bypass (e.g., ["/admin/*"])
            boundary_cache: Permission boundary cache for O(1) inheritance (Issue #922)
            enable_boundary_cache: Enable boundary caching (default: True)
            hotspot_detector: HotspotDetector for access pattern tracking (Issue #921)
            enable_hotspot_tracking: Enable hotspot tracking (default: True)
            namespace_manager: NamespaceManager for per-subject visibility (Issue #1239)
            agent_registry: AgentRegistry for stale-session detection (Issue #1240)
        """
        self.metadata_store = metadata_store
        self.rebac_manager: EnhancedReBACManager | None = rebac_manager
        self.entity_registry = entity_registry  # v0.5.0 ACE
        self.router = router  # For backend object type resolution

        # Issue #1239: Per-subject namespace visibility (Agent OS Phase 0)
        self.namespace_manager: NamespaceManager | None = namespace_manager

        # Issue #1240: Agent registry for stale-session detection (Agent OS Phase 1)
        self.agent_registry = agent_registry

        # P0-4: Enhanced features
        self.allow_admin_bypass = allow_admin_bypass
        self.allow_system_bypass = allow_system_bypass
        self.audit_store = audit_store
        self.admin_bypass_paths = admin_bypass_paths or []

        # Issue #899: Centralized cache coordinator for all permission caches
        from nexus.core.permission_cache import PermissionCacheCoordinator

        self._cache = PermissionCacheCoordinator(
            rebac_manager=rebac_manager,
            boundary_cache=boundary_cache,
            enable_boundary_cache=enable_boundary_cache,
            hotspot_detector=hotspot_detector,
            enable_hotspot_tracking=enable_hotspot_tracking,
        )

        # Backward-compatible properties that delegate to coordinator
        self._boundary_cache = self._cache.boundary_cache
        self._hotspot_detector = self._cache.hotspot_detector
        self._bitmap_completeness_cache = self._cache._bitmap_completeness_cache
        self._bitmap_completeness_ttl = self._cache._bitmap_completeness_ttl
        self._leopard_dir_index = self._cache._leopard_dir_index
        self._leopard_dir_ttl = self._cache._leopard_dir_ttl

        # Register boundary cache invalidation callback with rebac_manager
        if (
            self._boundary_cache
            and self.rebac_manager
            and hasattr(self.rebac_manager, "register_boundary_cache_invalidator")
        ):
            callback_id = f"permission_enforcer_{id(self)}"
            self.rebac_manager.register_boundary_cache_invalidator(
                callback_id,
                self._boundary_cache.invalidate_permission_change,
            )

        # Warn if ACL store is provided (deprecated)
        if acl_store is not None:
            import warnings

            warnings.warn(
                "acl_store parameter is deprecated and will be removed in v0.7.0. "
                "Use ReBAC for all permissions.",
                DeprecationWarning,
                stacklevel=2,
            )

    def invalidate_cache(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        zone_id: str | None = None,
    ) -> None:
        """Invalidate permission caches when permissions change.

        Should be called after write_tuple, delete_tuple, or bulk permission changes.

        Args:
            subject_type: If provided, only invalidate cache for this subject type
            subject_id: If provided, only invalidate cache for this subject
            zone_id: If provided, only invalidate cache for this zone
        """
        self._cache.invalidate(subject_type, subject_id, zone_id)
        logger.debug(
            f"[CACHE-INVALIDATE] Invalidated cache for "
            f"{(subject_type, subject_id, zone_id) if subject_type else 'all'}"
        )

    def has_accessible_descendants(
        self,
        prefix: str,
        context: OperationContext,
    ) -> bool:
        """Check if user has any accessible paths under the given prefix.

        Uses Tiger bitmap for O(1) lookup instead of scanning all files.
        This is used to determine if a directory should be shown when user
        has access to files within it (but not the directory itself).

        Args:
            prefix: Directory path prefix (e.g., "/skills/")
            context: Operation context with user information

        Returns:
            True if user can access any path starting with prefix
        """
        start = time.time()
        tiger_cache = getattr(self.rebac_manager, "_tiger_cache", None)
        if tiger_cache is None:
            logger.debug("[HAS-DESCENDANTS] No Tiger cache, returning True (fallback)")
            return True  # Fallback: assume accessible

        try:
            subject = context.get_subject()
            subject_type, subject_id = subject
            zone_id = context.zone_id

            # Get user's bitmap
            bitmap_bytes = tiger_cache.get_bitmap_bytes(
                subject_type=subject_type,
                subject_id=subject_id,
                permission="read",
                resource_type="file",
                zone_id=zone_id,
            )

            if bitmap_bytes is None:
                logger.debug(f"[HAS-DESCENDANTS] No bitmap for {subject_type}:{subject_id}")
                return True  # No bitmap = fallback to showing directory

            # Decode bitmap to get all allowed int IDs
            try:
                import nexus_fast

                allowed_ids = nexus_fast.decode_roaring_bitmap(bitmap_bytes)
            except (ImportError, AttributeError):
                # Fallback to Python roaring bitmap
                from pyroaring import BitMap

                bitmap = BitMap.deserialize(bitmap_bytes)
                allowed_ids = list(bitmap)

            if not allowed_ids:
                logger.debug(f"[HAS-DESCENDANTS] Empty bitmap for {subject_type}:{subject_id}")
                return False

            # Get paths for allowed IDs using reverse map (O(1) per ID)
            resource_map = tiger_cache._resource_map
            prefix_normalized = prefix.rstrip("/") + "/"

            # Check if any allowed path starts with prefix
            with resource_map._lock:
                for int_id in allowed_ids:
                    # O(1) reverse lookup: int_id -> (type, path)
                    resource_info = resource_map._int_to_uuid.get(int_id)
                    if resource_info and resource_info[0] == "file":
                        path = resource_info[1]
                        if path.startswith(prefix_normalized) or path == prefix.rstrip("/"):
                            elapsed = (time.time() - start) * 1000
                            logger.debug(
                                f"[HAS-DESCENDANTS] prefix={prefix}, FOUND {path} in {elapsed:.1f}ms"
                            )
                            return True

            elapsed = (time.time() - start) * 1000
            logger.debug(f"[HAS-DESCENDANTS] prefix={prefix}, NOT FOUND in {elapsed:.1f}ms")
            return False

        except Exception as e:
            logger.warning(f"[HAS-DESCENDANTS] Error: {e}, returning True (fallback)")
            return True  # Fallback: assume accessible

    def has_accessible_descendants_batch(
        self,
        prefixes: list[str],
        context: OperationContext,
    ) -> dict[str, bool]:
        """Check if user has accessible paths under multiple prefixes at once.

        Loads Tiger bitmap ONCE and scans all prefixes in a single pass.
        This avoids the N+1 pattern of calling has_accessible_descendants()
        individually per directory (Issue #1298).

        Args:
            prefixes: List of directory path prefixes (e.g., ["/skills/", "/docs/"])
            context: Operation context with user information

        Returns:
            Dict mapping each prefix to True/False
        """
        if not prefixes:
            return {}

        start = time.time()
        tiger_cache = getattr(self.rebac_manager, "_tiger_cache", None)
        if tiger_cache is None:
            logger.debug("[BATCH-OPT] No Tiger cache, returning all True (fallback)")
            return dict.fromkeys(prefixes, True)

        try:
            subject = context.get_subject()
            subject_type, subject_id = subject
            zone_id = context.zone_id

            # Get user's bitmap ONCE
            bitmap_bytes = tiger_cache.get_bitmap_bytes(
                subject_type=subject_type,
                subject_id=subject_id,
                permission="read",
                resource_type="file",
                zone_id=zone_id,
            )

            if bitmap_bytes is None:
                logger.debug(
                    f"[BATCH-OPT] No bitmap for {subject_type}:{subject_id}, "
                    f"returning all True for {len(prefixes)} prefixes"
                )
                return dict.fromkeys(prefixes, True)

            # Decode bitmap ONCE
            try:
                import nexus_fast

                allowed_ids = nexus_fast.decode_roaring_bitmap(bitmap_bytes)
            except (ImportError, AttributeError):
                from pyroaring import BitMap

                bitmap = BitMap.deserialize(bitmap_bytes)
                allowed_ids = list(bitmap)

            if not allowed_ids:
                logger.debug(f"[BATCH-OPT] Empty bitmap for {subject_type}:{subject_id}")
                return dict.fromkeys(prefixes, False)

            # Build set of all accessible file paths from allowed IDs (single scan)
            resource_map = tiger_cache._resource_map
            accessible_paths: list[str] = []
            with resource_map._lock:
                for int_id in allowed_ids:
                    resource_info = resource_map._int_to_uuid.get(int_id)
                    if resource_info and resource_info[0] == "file":
                        accessible_paths.append(resource_info[1])

            # Check each prefix against the accessible paths set
            results: dict[str, bool] = {}
            for prefix in prefixes:
                prefix_normalized = prefix.rstrip("/") + "/"
                prefix_exact = prefix.rstrip("/")
                found = False
                for path in accessible_paths:
                    if path.startswith(prefix_normalized) or path == prefix_exact:
                        found = True
                        break
                results[prefix] = found

            elapsed = (time.time() - start) * 1000
            found_count = sum(1 for v in results.values() if v)
            logger.debug(
                f"[BATCH-OPT] has_accessible_descendants_batch: "
                f"{found_count}/{len(prefixes)} accessible in {elapsed:.1f}ms "
                f"(bitmap: {len(allowed_ids)} IDs, paths: {len(accessible_paths)})"
            )
            return results

        except Exception as e:
            logger.warning(
                f"[BATCH-OPT] has_accessible_descendants_batch error: {e}, "
                f"returning all True for {len(prefixes)} prefixes (fallback)"
            )
            return dict.fromkeys(prefixes, True)

    def check(
        self,
        path: str,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check if user has permission to perform operation on file.

        Permission check with scoped admin/system bypass and audit logging (P0-4):
        1. System bypass (limited scope) - Read: any path, Write/Delete: /system/* only
        2. Admin bypass (capability-based) - Requires capabilities and optional path allowlist
        3. ReBAC relationship check - Check permission graph

        Args:
            path: Virtual file path
            permission: Permission to check (READ, WRITE, EXECUTE)
            context: Operation context with user/group information

        Returns:
            True if permission is granted, False otherwise

        Examples:
            >>> enforcer = PermissionEnforcer(metadata_store, rebac_manager=rebac)
            >>> ctx = OperationContext(user="alice", groups=["developers"])
            >>> enforcer.check("/workspace/file.txt", Permission.READ, ctx)
            True
        """
        logger.debug(
            f"[PermissionEnforcer.check] path={path}, perm={permission.name}, user={context.user}, is_admin={context.is_admin}, is_system={context.is_system}"
        )

        # Map Permission enum to string
        permission_str = self._permission_to_string(permission)

        # P0-4: System bypass (limited scope)
        if context.is_system:
            if not self.allow_system_bypass:
                self._log_bypass_denied(
                    context, path, permission_str, "system", "kill_switch_disabled"
                )
                raise PermissionError("System bypass disabled by configuration")

            if not self._is_allowed_system_operation(path, permission_str):
                self._log_bypass_denied(context, path, permission_str, "system", "scope_limit")
                raise PermissionError(f"System bypass not allowed for {path}")

            self._log_bypass(context, path, permission_str, "system", allowed=True)
            return True

        # P0-4: Admin bypass (capability-based + path-scoped)
        if context.is_admin:
            if not self.allow_admin_bypass:
                self._log_bypass_denied(
                    context, path, permission_str, "admin", "kill_switch_disabled"
                )
                # Fall through to ReBAC check instead of denying
                return self._check_rebac(path, permission, context)

            # P0-4: Check path-based allowlist (scoped bypass)
            if self.admin_bypass_paths and not self._path_matches_allowlist(
                path, self.admin_bypass_paths
            ):
                self._log_bypass_denied(
                    context, path, permission_str, "admin", "path_not_in_allowlist"
                )
                # Fall through to ReBAC check
                return self._check_rebac(path, permission, context)

            # Import AdminCapability here to avoid circular imports
            from nexus.services.permissions.permissions_enhanced import AdminCapability

            # P0-4: Zone boundary check (security fix for issue #819)
            # Extract zone from path (format: /zone/{zone_id}/...)
            path_zone_id = None
            if path.startswith("/zone/"):
                parts = path[6:].split("/", 1)  # Remove "/zone/" prefix
                if parts:
                    path_zone_id = parts[0]

            # Check if admin is attempting cross-zone access
            if (
                path_zone_id
                and context.zone_id
                and path_zone_id != context.zone_id
                and AdminCapability.MANAGE_ZONES not in context.admin_capabilities
            ):
                # Cross-zone access requires MANAGE_ZONES capability (system admin only)
                # Not system admin - deny cross-zone access
                self._log_bypass_denied(
                    context,
                    path,
                    permission_str,
                    "admin",
                    f"cross_zone_access_denied_path_zone={path_zone_id}_context_zone={context.zone_id}",
                )
                # Immediately raise PermissionError for cross-zone access violation
                raise PermissionError(
                    f"Access denied: Cross-zone access requires MANAGE_ZONES capability. "
                    f"Context zone: {context.zone_id}, Path zone: {path_zone_id}"
                )

            required_capability = AdminCapability.get_required_capability(path, permission_str)
            wildcard_capability = f"admin:{permission_str}:*"

            # Check if user has EITHER the path-specific capability OR the wildcard capability
            # Wildcard capability (admin:read:*) grants access to ALL paths
            has_capability = (
                required_capability in context.admin_capabilities
                or wildcard_capability in context.admin_capabilities
            )

            if not has_capability:
                self._log_bypass_denied(
                    context,
                    path,
                    permission_str,
                    "admin",
                    f"missing_capability_{required_capability}",
                )
                # Fall through to ReBAC check
                return self._check_rebac(path, permission, context)

            self._log_bypass(context, path, permission_str, "admin", allowed=True)
            return True

        # Issue #1239: Namespace visibility check (Agent OS Phase 0)
        # Unmounted paths are invisible (404 Not Found), not denied (403 Forbidden).
        # This runs AFTER admin/system bypass (admins see everything) and BEFORE
        # fine-grained ReBAC check (defense in depth).
        if self.namespace_manager is not None:
            subject = context.get_subject()
            if not self.namespace_manager.is_visible(subject, path, context.zone_id):
                from nexus.core.exceptions import NexusFileNotFoundError

                raise NexusFileNotFoundError(
                    path=path,
                    message="Path not found",  # Intentionally vague — path is invisible
                )

        # Issue #1240 / #1445: Stale-session detection (Agent OS Phase 1)
        check_stale_session(self.agent_registry, context)

        # Normal ReBAC check
        return self._check_rebac(path, permission, context)

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
        logger.debug(
            f"[_check_rebac] path={path}, permission={permission}, context.user={context.user}"
        )

        if not self.rebac_manager:
            # No ReBAC manager - deny by default
            # This ensures security: must explicitly configure ReBAC
            logger.debug("  -> DENY (no rebac_manager)")
            return False

        # Map Permission flags to string permission names
        permission_name = self._permission_to_string(permission)
        if permission_name == "unknown":
            logger.debug(f"  -> DENY (unknown permission: {permission})")
            return False

        # Get backend-specific object type for ReBAC check
        # This allows different backends (Postgres, Redis, etc.) to have different permission models
        object_type = "file"  # Default
        object_id = path  # Default - use virtual path for permission checks

        if self.router:
            try:
                # Route path to backend to get object type
                route = self.router.route(
                    path,
                    zone_id=context.zone_id,
                    is_admin=context.is_admin,
                    check_write=False,
                )
                # Ask backend for its object type
                object_type = route.backend.get_object_type(route.backend_path)

                # CRITICAL FIX: For file objects, use the VIRTUAL path for permission checks,
                # not the backend-relative path. ReBAC tuples are created with virtual paths
                # (e.g., /mnt/gcs/file.csv), but backend.get_object_id() returns backend-relative
                # paths (e.g., file.csv) which breaks permission inheritance for mounted backends.
                # Non-file backends (DB tables, Redis keys, etc.) can still override object_id.
                if object_type == "file":
                    # Use virtual path for file permission checks (mount-aware)
                    object_id = path
                    logger.debug(
                        f"[PermissionEnforcer] Using virtual path for file permission check: '{path}'"
                    )
                else:
                    # For non-file backends, use backend-provided object_id
                    object_id = route.backend.get_object_id(route.backend_path)
                    logger.debug(
                        f"[PermissionEnforcer] Using backend object_id for {object_type}: '{object_id}'"
                    )
            except Exception as e:
                # If routing fails, fall back to default "file" type with virtual path
                logger.warning(
                    f"[_check_rebac] Failed to route path for object type: {e}, using default 'file'"
                )

        # Check ReBAC permission using backend-provided object type
        # P0-4: Pass zone_id for multi-zone isolation
        zone_id = context.zone_id or "default"
        subject = context.get_subject()

        logger.debug(
            f"[_check_rebac] Calling rebac_check: subject={subject}, permission={permission_name}, object=('{object_type}', '{object_id}'), zone_id={zone_id}"
        )

        # Issue #921: Record access for hotspot detection (before cache/graph check)
        # This tracks access patterns to enable proactive cache prefetching
        if self._hotspot_detector:
            self._hotspot_detector.record_access(
                subject_type=subject[0],
                subject_id=subject[1],
                resource_type=object_type,
                permission=permission_name,
                zone_id=zone_id,
            )

        # NOTE: Removed implicit directory TRAVERSE optimization (was incorrectly granting
        # access to ALL authenticated users for ANY implicit directory, violating Unix semantics)
        # Correct behavior: user should only see a directory if they have access to at least
        # one file inside it. This is handled by _has_descendant_access in the listing code.

        # Issue #899: Adaptive batch vs sequential ancestor resolution.
        # For shallow paths (depth <= 2), sequential is fine (1-2 queries).
        # For deeper paths, batch all checks into one rebac_check_bulk() call.
        depth = object_id.count("/") if object_id else 0

        if depth <= 2 or permission_name not in ("read", "write", "traverse"):
            return self._check_rebac_sequential(
                subject, permission_name, object_type, object_id, zone_id
            )

        return self._check_rebac_batched(subject, permission_name, object_type, object_id, zone_id)

    def _check_rebac_sequential(
        self,
        subject: tuple[str, str],
        permission_name: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> bool:
        """Sequential permission check for shallow paths (depth <= 2).

        Performs direct check, TRAVERSE implication, boundary cache lookup,
        and parent walk one level at a time. Efficient when only 1-2 parents.
        """
        assert self.rebac_manager is not None  # guaranteed by _check_rebac guard

        # 1. Direct permission check
        result = self.rebac_manager.rebac_check(
            subject=subject,
            permission=permission_name,
            object=(object_type, object_id),
            zone_id=zone_id,
        )
        if result:
            return True

        # 2. TRAVERSE implied by READ/WRITE
        if permission_name == "traverse":
            for implied_perm in ("read", "write"):
                if self.rebac_manager.rebac_check(
                    subject=subject,
                    permission=implied_perm,
                    object=(object_type, object_id),
                    zone_id=zone_id,
                ):
                    logger.debug(
                        f"[_check_rebac] ALLOW TRAVERSE (has {implied_perm.upper()} permission)"
                    )
                    return True

        # 3. Parent directory inheritance (sequential walk)
        if permission_name in ("read", "write", "traverse") and object_id:
            subject_type, subject_id = subject

            # FAST PATH: Boundary cache (Issue #922)
            if self._boundary_cache:
                boundary = self._boundary_cache.get_boundary(
                    zone_id, subject_type, subject_id, permission_name, object_id
                )
                if boundary:
                    boundary_result = self.rebac_manager.rebac_check(
                        subject=subject,
                        permission=permission_name,
                        object=(object_type, boundary),
                        zone_id=zone_id,
                    )
                    if boundary_result:
                        logger.debug(
                            f"[_check_rebac] ALLOW (boundary cache hit: {object_id} → {boundary})"
                        )
                        return True
                    self._boundary_cache.invalidate_permission_change(
                        zone_id, subject_type, subject_id, permission_name, boundary
                    )

            # SLOW PATH: Walk up the directory tree
            parent_path = object_id
            while parent_path and parent_path != "/":
                parent_path = os.path.dirname(parent_path)
                if not parent_path or parent_path == object_id:
                    parent_path = "/"

                parent_result = self.rebac_manager.rebac_check(
                    subject=subject,
                    permission=permission_name,
                    object=(object_type, parent_path),
                    zone_id=zone_id,
                )
                if parent_result:
                    logger.debug(f"[_check_rebac] ALLOW (inherited from parent: {parent_path})")
                    if self._boundary_cache:
                        self._boundary_cache.set_boundary(
                            zone_id,
                            subject_type,
                            subject_id,
                            permission_name,
                            object_id,
                            parent_path,
                        )
                    return True

                if parent_path == "/":
                    break

        return False

    def _check_rebac_batched(
        self,
        subject: tuple[str, str],
        permission_name: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> bool:
        """Batch permission check for deep paths (depth > 2) (Issue #899).

        Collects all check variants (direct, implied TRAVERSE, all ancestors)
        and resolves them in a single rebac_check_bulk() call instead of O(D)
        sequential queries.
        """
        assert self.rebac_manager is not None  # guaranteed by _check_rebac guard

        subject_type, subject_id = subject

        # FAST PATH: Boundary cache (Issue #922)
        if self._boundary_cache:
            boundary = self._boundary_cache.get_boundary(
                zone_id, subject_type, subject_id, permission_name, object_id
            )
            if boundary:
                boundary_result = self.rebac_manager.rebac_check(
                    subject=subject,
                    permission=permission_name,
                    object=(object_type, boundary),
                    zone_id=zone_id,
                )
                if boundary_result:
                    logger.debug(
                        f"[_check_rebac_batched] ALLOW (boundary cache hit: "
                        f"{object_id} → {boundary})"
                    )
                    return True
                self._boundary_cache.invalidate_permission_change(
                    zone_id, subject_type, subject_id, permission_name, boundary
                )

        # Collect ALL checks for a single rebac_check_bulk() call
        from nexus.core.path_utils import get_ancestors

        ancestors = get_ancestors(object_id)
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]] = []

        # Direct permission on the object itself
        checks.append((subject, permission_name, (object_type, object_id)))

        # TRAVERSE implied by READ/WRITE on the object
        if permission_name == "traverse":
            checks.append((subject, "read", (object_type, object_id)))
            checks.append((subject, "write", (object_type, object_id)))

        # All ancestor checks for permission inheritance
        for ancestor in ancestors:
            if ancestor != object_id:
                checks.append((subject, permission_name, (object_type, ancestor)))
                # TRAVERSE implication also applies to ancestors
                if permission_name == "traverse":
                    checks.append((subject, "read", (object_type, ancestor)))
                    checks.append((subject, "write", (object_type, ancestor)))

        # ONE bulk call instead of O(D) sequential calls
        results = self.rebac_manager.rebac_check_bulk(checks, zone_id=zone_id)

        # Process results: direct > implied > inherited (priority order)
        for check in checks:
            if results.get(check, False):
                granting_path = check[2][1]  # object_id from the check tuple
                logger.debug(f"[_check_rebac_batched] ALLOW via {check[1]} on {granting_path}")
                # Update boundary cache if granted via an ancestor
                if self._boundary_cache and granting_path != object_id:
                    self._boundary_cache.set_boundary(
                        zone_id,
                        subject_type,
                        subject_id,
                        permission_name,
                        object_id,
                        granting_path,
                    )
                return True

        logger.debug(
            f"[_check_rebac_batched] DENY {permission_name} on {object_id} "
            f"(checked {len(checks)} tuples across {len(ancestors)} ancestors)"
        )
        return False

    def _is_allowed_system_operation(self, path: str, permission: str) -> bool:
        """Check if system bypass is allowed for this operation (P0-4).

        System bypass is limited to:
        - Read operations on any path (for auto-parse indexing)
        - Read, write, execute, delete operations on /system/* paths only

        Args:
            path: File path
            permission: Permission type

        Returns:
            True if system bypass is allowed
        """
        # Allow read operations on any path (for auto-parse and other system reads)
        if permission == "read":
            return True

        # For other operations, only allow /system paths
        # Use strict matching: /system/ or exactly /system (not /systemdata, etc.)
        if not (path.startswith("/system/") or path == "/system"):
            return False

        # Allow common operations on /system paths
        return permission in ["write", "execute", "delete"]

    def _log_bypass(
        self,
        context: OperationContext,
        path: str,
        permission: str,
        bypass_type: str,
        allowed: bool,
    ) -> None:
        """Log admin/system bypass to audit store (P0-4)."""
        if not self.audit_store:
            return

        from datetime import UTC, datetime

        from nexus.services.permissions.permissions_enhanced import AuditLogEntry

        entry = AuditLogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            request_id=getattr(context, "request_id", str(uuid.uuid4())),
            user=context.user,
            zone_id=context.zone_id,
            path=path,
            permission=permission,
            bypass_type=bypass_type,
            allowed=allowed,
            capabilities=sorted(getattr(context, "admin_capabilities", [])),
        )

        self.audit_store.log_bypass(entry)

    def _log_bypass_denied(
        self,
        context: OperationContext,
        path: str,
        permission: str,
        bypass_type: str,
        reason: str,
    ) -> None:
        """Log denied bypass attempt (P0-4)."""
        if not self.audit_store:
            return

        from datetime import UTC, datetime

        from nexus.services.permissions.permissions_enhanced import AuditLogEntry

        entry = AuditLogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            request_id=getattr(context, "request_id", str(uuid.uuid4())),
            user=context.user,
            zone_id=context.zone_id,
            path=path,
            permission=permission,
            bypass_type=bypass_type,
            allowed=False,
            capabilities=sorted(getattr(context, "admin_capabilities", [])),
            denial_reason=reason,
        )

        self.audit_store.log_bypass(entry)

    def _permission_to_string(self, permission: Permission) -> str:
        """Convert Permission enum to string."""
        if permission & Permission.READ:
            return "read"
        elif permission & Permission.WRITE:
            return "write"
        elif permission & Permission.EXECUTE:
            return "execute"
        elif permission & Permission.TRAVERSE:
            return "traverse"
        elif permission & Permission.NONE:
            return "none"
        else:
            return "unknown"

    def _path_matches_allowlist(self, path: str, allowlist: list[str]) -> bool:
        """Check if path matches any pattern in allowlist.

        P0-4: Scoped admin bypass - only allow admin bypass for specific paths

        Args:
            path: File path to check
            allowlist: List of path patterns (supports wildcards: /admin/*, /workspace/*)

        Returns:
            True if path matches any allowlist pattern
        """
        from nexus.core import glob_fast

        return glob_fast.glob_match(path, list(allowlist))

    def get_boundary_cache_stats(self) -> dict[str, Any] | None:
        """Get permission boundary cache statistics (Issue #922).

        Returns statistics about the boundary cache including hit rate,
        miss rate, and number of cached entries.

        Returns:
            Dictionary with cache statistics, or None if boundary cache is disabled

        Example:
            >>> enforcer = PermissionEnforcer(metadata_store, rebac_manager=rebac)
            >>> stats = enforcer.get_boundary_cache_stats()
            >>> print(f"Hit rate: {stats['hit_rate_percent']}%")
        """
        if self._boundary_cache is None:
            return None
        return self._boundary_cache.get_stats()

    def reset_boundary_cache_stats(self) -> None:
        """Reset permission boundary cache statistics (Issue #922)."""
        if self._boundary_cache is not None:
            self._boundary_cache.reset_stats()

    def clear_boundary_cache(self) -> None:
        """Clear all entries from the permission boundary cache (Issue #922)."""
        if self._boundary_cache is not None:
            self._boundary_cache.clear()

    @property
    def hotspot_detector(self) -> HotspotDetector | None:
        """Get the hotspot detector instance (Issue #921).

        Returns:
            HotspotDetector instance, or None if hotspot tracking is disabled
        """
        return self._hotspot_detector

    def get_hotspot_stats(self) -> dict[str, Any] | None:
        """Get hotspot detection statistics (Issue #921).

        Returns statistics about access pattern tracking including
        hot entries count, total accesses, and prefetch triggers.

        Returns:
            Dictionary with hotspot statistics, or None if disabled

        Example:
            >>> enforcer = PermissionEnforcer(metadata_store, rebac_manager=rebac)
            >>> stats = enforcer.get_hotspot_stats()
            >>> print(f"Hot entries: {stats['hot_entries_detected']}")
        """
        if self._hotspot_detector is None:
            return None
        return self._hotspot_detector.get_stats()

    def get_hot_entries(self, limit: int | None = 10) -> list[Any] | None:
        """Get current hot permission entries (Issue #921).

        Returns list of HotspotEntry objects representing frequently
        accessed permission paths.

        Args:
            limit: Maximum number of entries to return (default: 10)

        Returns:
            List of HotspotEntry objects, or None if disabled
        """
        if self._hotspot_detector is None:
            return None
        return self._hotspot_detector.get_hot_entries(limit=limit)

    def filter_list(
        self,
        paths: list[str],
        context: OperationContext,
    ) -> list[str]:
        """Filter list of paths by read permission (Issue #899 strategy chain).

        Uses a composable strategy chain for performance:
        1. Tiger bitmap (O(1) per path)
        2. Leopard directory index (cached dir grants)
        3. Hierarchy pre-filter (batch parent checks)
        4. Zone pre-filter (cross-zone elimination)
        5. Bulk ReBAC (final fallback)

        Args:
            paths: List of paths to filter
            context: Operation context

        Returns:
            Filtered list of paths user can read
        """
        # Admin/system bypass
        if (context.is_admin and self.allow_admin_bypass) or (
            context.is_system and self.allow_system_bypass
        ):
            return paths

        # Issue #1239 + #1244: Namespace pre-filter
        if self.namespace_manager is not None:
            subject = context.get_subject()
            paths = self.namespace_manager.filter_visible(subject, paths, context.zone_id)
            if not paths:
                return []

        # Use strategy chain if rebac_manager supports bulk checks
        if self.rebac_manager and hasattr(self.rebac_manager, "rebac_check_bulk"):
            overall_start = time.time()
            zone_id = context.zone_id or "default"
            subject = context.get_subject()

            logger.debug(
                f"[PERF-FILTER] filter_list START: {len(paths)} paths, "
                f"subject={subject}, zone={zone_id}"
            )

            from nexus.core.permission_filter_chain import FilterContext, run_filter_chain

            filter_ctx = FilterContext(
                paths=paths,
                subject=subject,
                zone_id=zone_id,
                context=context,
                cache=self._cache,
                rebac_manager=self.rebac_manager,
                router=self.router,
            )

            filtered = run_filter_chain(filter_ctx)

            overall_elapsed = time.time() - overall_start
            logger.info(
                f"[PERF-FILTER] filter_list DONE: {overall_elapsed:.3f}s total, "
                f"allowed {len(filtered)}/{len(paths)} paths"
            )
            return filtered

        # Fallback: Filter by ReBAC permissions individually
        result = []
        for path in paths:
            if self.check(path, Permission.READ, context):
                result.append(path)

        return result
