"""Default permission policies per namespace.

This module implements automatic permission assignment for new files
based on their location in the filesystem.

Policy Structure:
    - Namespace Pattern: Glob pattern (e.g., /workspace/*, /shared/*)
    - Default Owner: Owner ID (supports ${agent_id}, ${tenant_id})
    - Default Group: Group ID (supports ${agent_id}, ${tenant_id})
    - Default Mode: Permission bits (e.g., 0o644)
    - Priority: Higher priority policies take precedence

Policy Matching:
    - Policies are matched by namespace pattern
    - Most specific pattern wins (by priority)
    - System-wide policies apply to all tenants
    - Tenant-specific policies override system-wide

Variable Substitution:
    - ${agent_id}: Replaced with agent ID from context
    - ${tenant_id}: Replaced with tenant ID from context
    - ${user_id}: Replaced with user ID from context
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class PermissionPolicy:
    """Permission policy for a namespace.

    Attributes:
        policy_id: Unique policy identifier
        namespace_pattern: Path pattern (e.g., /workspace/*, /shared/*)
        tenant_id: Tenant ID (None = system-wide)
        default_owner: Default owner (supports ${agent_id}, ${tenant_id})
        default_group: Default group (supports ${agent_id}, ${tenant_id})
        default_mode: Default permission bits (e.g., 0o644)
        priority: Priority for pattern matching (higher = more specific)
    """

    policy_id: str
    namespace_pattern: str
    tenant_id: str | None
    default_owner: str
    default_group: str
    default_mode: int
    priority: int = 0

    def matches(self, path: str) -> bool:
        """Check if this policy matches a path.

        Args:
            path: Virtual path to check

        Returns:
            True if policy matches the path
        """
        # Use fnmatch for glob-style pattern matching
        return fnmatch.fnmatch(path, self.namespace_pattern)

    def apply(
        self,
        context: dict[str, str] | None = None,
        is_directory: bool = False,  # noqa: ARG002
    ) -> tuple[str, str, int]:
        """Apply policy with variable substitution.

        Args:
            context: Variable context (agent_id, tenant_id, user_id)
            is_directory: Whether the file is a directory

        Returns:
            Tuple of (owner, group, mode) with variables substituted

        Example:
            >>> policy = PermissionPolicy(
            ...     policy_id="p1",
            ...     namespace_pattern="/workspace/*",
            ...     tenant_id=None,
            ...     default_owner="${agent_id}",
            ...     default_group="agents",
            ...     default_mode=0o644
            ... )
            >>> policy.apply({"agent_id": "alice"})
            ('alice', 'agents', 420)
        """
        context = context or {}

        # Substitute variables in owner
        owner = self._substitute_variables(self.default_owner, context)

        # Substitute variables in group
        group = self._substitute_variables(self.default_group, context)

        # Use the policy's default mode
        mode = self.default_mode

        return owner, group, mode

    def _substitute_variables(self, value: str, context: dict[str, str]) -> str:
        """Substitute ${var} placeholders with context values.

        Args:
            value: String with placeholders
            context: Variable context

        Returns:
            String with variables substituted

        Example:
            >>> policy = PermissionPolicy(...)
            >>> policy._substitute_variables("${agent_id}", {"agent_id": "alice"})
            'alice'
        """
        # Pattern for ${variable} syntax
        pattern = r"\$\{(\w+)\}"

        def replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            return context.get(var_name, match.group(0))  # Keep original if not found

        return re.sub(pattern, replace, value)


class PolicyMatcher:
    """Matches paths to policies and applies them.

    This class handles policy selection based on:
    - Path pattern matching
    - Tenant scoping
    - Priority ordering
    """

    def __init__(self, policies: list[PermissionPolicy] | None = None):
        """Initialize policy matcher.

        Args:
            policies: List of policies (can be empty)
        """
        self.policies = policies or []

    def add_policy(self, policy: PermissionPolicy) -> None:
        """Add a policy to the matcher.

        Args:
            policy: Policy to add
        """
        self.policies.append(policy)

    def find_matching_policy(
        self, path: str, tenant_id: str | None = None
    ) -> PermissionPolicy | None:
        """Find the best matching policy for a path.

        Policy selection logic:
        1. Filter policies by tenant (tenant-specific + system-wide)
        2. Filter policies that match the path pattern
        3. Sort by priority (higher first)
        4. Return highest priority policy

        Args:
            path: Virtual path
            tenant_id: Tenant ID (None = system-wide)

        Returns:
            Best matching policy, or None if no match
        """
        # Filter applicable policies
        applicable = [
            p
            for p in self.policies
            if (p.tenant_id == tenant_id or p.tenant_id is None)  # Tenant or system-wide
            and p.matches(path)  # Matches pattern
        ]

        if not applicable:
            return None

        # Sort by priority (higher first), then prefer tenant-specific
        applicable.sort(key=lambda p: (p.priority, p.tenant_id is not None), reverse=True)

        return applicable[0]

    def apply_policy(
        self,
        path: str,
        tenant_id: str | None = None,
        context: dict[str, str] | None = None,
        is_directory: bool = False,
    ) -> tuple[str, str, int] | None:
        """Apply the best matching policy for a path.

        Args:
            path: Virtual path
            tenant_id: Tenant ID
            context: Variable context (agent_id, tenant_id, user_id)
            is_directory: Whether the file is a directory

        Returns:
            Tuple of (owner, group, mode) or None if no policy matches
        """
        policy = self.find_matching_policy(path, tenant_id)
        if policy is None:
            return None

        return policy.apply(context, is_directory)


def create_default_policies() -> list[PermissionPolicy]:
    """Create default permission policies for standard namespaces.

    Returns:
        List of default policies

    Default Policies:
        /workspace/*: owner=${agent_id}, group=agents, mode=0o644
        /shared/*: owner=root, group=${tenant_id}, mode=0o664
        /archives/*: owner=root, group=${tenant_id}, mode=0o444
        /system/*: owner=root, group=root, mode=0o600
    """
    import uuid

    return [
        # Workspace: Agent-owned files, read-only for others
        PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/workspace/*",
            tenant_id=None,  # System-wide
            default_owner="${agent_id}",
            default_group="agents",
            default_mode=0o644,  # rw-r--r--
            priority=10,
        ),
        # Shared: Tenant-shared files, read-write for group
        PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/shared/*",
            tenant_id=None,  # System-wide
            default_owner="root",
            default_group="${tenant_id}",
            default_mode=0o664,  # rw-rw-r--
            priority=10,
        ),
        # Archives: Read-only for everyone
        PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/archives/*",
            tenant_id=None,  # System-wide
            default_owner="root",
            default_group="${tenant_id}",
            default_mode=0o444,  # r--r--r--
            priority=10,
        ),
        # System: Admin-only
        PermissionPolicy(
            policy_id=str(uuid.uuid4()),
            namespace_pattern="/system/*",
            tenant_id=None,  # System-wide
            default_owner="root",
            default_group="root",
            default_mode=0o600,  # rw-------
            priority=10,
        ),
    ]
