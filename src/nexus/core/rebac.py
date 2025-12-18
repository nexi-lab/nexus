"""Relationship-Based Access Control (ReBAC) for Nexus.

This module implements Zanzibar-style relationship-based authorization,
enabling fine-grained permissions based on relationships between entities.

ReBAC Model:
    - Tuples: (subject, relation, object) representing relationships
    - Namespaces: Configuration for permission expansion per object type
    - Check API: Fast permission checks with graph traversal
    - Expand API: Find all subjects with a given permission

Relationship Types:
    - member-of: Agent is member of group/team
    - owner-of: Subject owns object (full permissions)
    - viewer-of: Subject can view object (read-only)
    - editor-of: Subject can edit object (read/write)
    - parent-of: Hierarchical relationship (e.g., folder → file)
    - shared-viewer: Cross-tenant read access
    - shared-editor: Cross-tenant read/write access
    - shared-owner: Cross-tenant full access

Example:
    # Direct relationship
    ("agent", alice_id) member-of ("group", eng_team_id)
    ("group", eng_team_id) owner-of ("file", file_id)

    # Check permission (with graph traversal)
    rebac_check(
        subject=("agent", alice_id),
        permission="read",
        object=("file", file_id)
    )  # Returns True (alice is member of eng_team, which owns file)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# Wildcard subject for public access
WILDCARD_SUBJECT = ("*", "*")

# Relations that are allowed to cross tenant boundaries
# These relations can link subjects and objects from different tenants
CROSS_TENANT_ALLOWED_RELATIONS = frozenset(
    {
        "shared-viewer",  # Read access via cross-tenant share
        "shared-editor",  # Read + Write access via cross-tenant share
        "shared-owner",  # Full access via cross-tenant share
    }
)


class RelationType(StrEnum):
    """Standard relationship types in ReBAC."""

    MEMBER_OF = "member-of"
    OWNER_OF = "owner-of"
    VIEWER_OF = "viewer-of"
    EDITOR_OF = "editor-of"
    PARENT_OF = "parent-of"
    # Cross-tenant sharing relations
    SHARED_VIEWER = "shared-viewer"
    SHARED_EDITOR = "shared-editor"
    SHARED_OWNER = "shared-owner"


class EntityType(StrEnum):
    """Types of entities in ReBAC system.

    Note: These are predefined constants for common entity types.
    The Entity dataclass accepts any string for entity_type to allow flexibility.

    Usage:
    - EntityRegistry: Enforces strict types (tenant, user, agent) for identity hierarchy
    - ReBAC: Accepts any string, including these predefined types for permission tuples
    """

    AGENT = "agent"
    USER = "user"
    GROUP = "group"
    FILE = "file"
    WORKSPACE = "workspace"
    TENANT = "tenant"
    PLAYBOOK = "playbook"  # v0.5.0 ACE
    TRAJECTORY = "trajectory"  # v0.5.0 ACE
    SKILL = "skill"  # v0.5.0 Skills System


@dataclass
class Entity:
    """Represents an entity in the ReBAC system.

    Attributes:
        entity_type: Type of entity (agent, group, file, etc.)
        entity_id: Unique identifier for the entity
    """

    entity_type: str
    entity_id: str

    def __post_init__(self) -> None:
        """Validate entity."""
        if not self.entity_type:
            raise ValueError("entity_type is required")
        if not self.entity_id:
            raise ValueError("entity_id is required")

    def to_tuple(self) -> tuple[str, str]:
        """Convert to (type, id) tuple."""
        return (self.entity_type, self.entity_id)

    @classmethod
    def from_tuple(cls, tup: tuple[str, str]) -> Entity:
        """Create entity from (type, id) tuple."""
        return cls(entity_type=tup[0], entity_id=tup[1])

    def __str__(self) -> str:
        return f"{self.entity_type}:{self.entity_id}"


@dataclass
class ReBACTuple:
    """Represents a relationship tuple in the ReBAC system.

    Format: (subject, relation, object)
    Example: (agent:alice, member-of, group:engineering)

    Attributes:
        tuple_id: Unique identifier for the tuple
        subject: Subject entity (who has the relationship)
        relation: Type of relationship
        object: Object entity (what the subject relates to)
        subject_relation: Optional indirect relation (for tupleToUserset)
        created_at: When the tuple was created
        expires_at: Optional expiration time for temporary access
        conditions: Optional JSON conditions for the relationship
    """

    tuple_id: str
    subject: Entity
    relation: str
    object: Entity
    subject_relation: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    conditions: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate tuple."""
        if not self.tuple_id:
            self.tuple_id = str(uuid.uuid4())
        if not isinstance(self.subject, Entity):
            raise TypeError(f"subject must be Entity, got {type(self.subject)}")
        if not isinstance(self.object, Entity):
            raise TypeError(f"object must be Entity, got {type(self.object)}")
        if not self.relation:
            raise ValueError("relation is required")

    def is_expired(self) -> bool:
        """Check if tuple has expired."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Convert tuple to dictionary."""
        return {
            "tuple_id": self.tuple_id,
            "subject_type": self.subject.entity_type,
            "subject_id": self.subject.entity_id,
            "subject_relation": self.subject_relation,
            "relation": self.relation,
            "object_type": self.object.entity_type,
            "object_id": self.object.entity_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "conditions": json.dumps(self.conditions) if self.conditions else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReBACTuple:
        """Create tuple from dictionary."""
        return cls(
            tuple_id=data["tuple_id"],
            subject=Entity(data["subject_type"], data["subject_id"]),
            relation=data["relation"],
            object=Entity(data["object_type"], data["object_id"]),
            subject_relation=data.get("subject_relation"),
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=(
                datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None
            ),
            conditions=json.loads(data["conditions"]) if data.get("conditions") else None,
        )

    def __str__(self) -> str:
        s = f"{self.subject} {self.relation} {self.object}"
        if self.subject_relation:
            s += f" (via {self.subject_relation})"
        if self.expires_at:
            s += f" (expires: {self.expires_at})"
        return s


@dataclass
class NamespaceConfig:
    """Configuration for a namespace (object type) in ReBAC.

    Defines how permissions are computed for a specific object type
    using Zanzibar-style rewrite rules.

    Attributes:
        namespace_id: Unique identifier
        object_type: Type of object (file, workspace, etc.)
        config: Permission expansion configuration
        created_at: When the config was created
        updated_at: When the config was last updated

    Example config:
        {
            "relations": {
                "owner": {
                    "union": ["direct_owner", "parent_owner"]
                },
                "direct_owner": {},
                "parent_owner": {
                    "tupleToUserset": {
                        "tupleset": "parent",
                        "computedUserset": "owner"
                    }
                },
                "viewer": {
                    "union": ["owner", "direct_viewer"]
                }
            }
        }
    """

    namespace_id: str
    object_type: str
    config: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Validate namespace config."""
        if not self.namespace_id:
            self.namespace_id = str(uuid.uuid4())
        if not self.object_type:
            raise ValueError("object_type is required")
        if not isinstance(self.config, dict):
            raise TypeError(f"config must be dict, got {type(self.config)}")

    def get_relation_config(self, relation: str) -> dict[str, Any] | None:
        """Get configuration for a specific relation.

        Args:
            relation: Relation name

        Returns:
            Relation config or None if not found
        """
        relations = self.config.get("relations", {})
        result = relations.get(relation)
        return result if result is None else dict(result)

    def has_union(self, relation: str) -> bool:
        """Check if relation is defined as a union."""
        rel_config = self.get_relation_config(relation)
        return rel_config is not None and "union" in rel_config

    def get_union_relations(self, relation: str) -> list[str]:
        """Get the list of relations in a union."""
        rel_config = self.get_relation_config(relation)
        if rel_config and "union" in rel_config:
            union_list = rel_config["union"]
            return list(union_list) if isinstance(union_list, list) else []
        return []

    def has_tuple_to_userset(self, relation: str) -> bool:
        """Check if relation uses tupleToUserset expansion."""
        rel_config = self.get_relation_config(relation)
        return rel_config is not None and "tupleToUserset" in rel_config

    def get_tuple_to_userset(self, relation: str) -> dict[str, str] | None:
        """Get tupleToUserset configuration."""
        rel_config = self.get_relation_config(relation)
        if rel_config and "tupleToUserset" in rel_config:
            ttu = rel_config["tupleToUserset"]
            return dict(ttu) if isinstance(ttu, dict) else None
        return None

    def get_permission_usersets(self, permission: str) -> list[str]:
        """Get the list of usersets (relations) that grant a permission.

        P0-1: Explicit permission-to-userset mapping for Zanzibar-style semantics.

        Permissions can be defined as:
        1. Simple list: "read": ["viewer", "editor", "owner"]
        2. Union: "read": {"union": ["viewer", "editor", "owner"]}
        3. Intersection: "read": {"intersection": ["viewer", "not_denied"]}
        4. Exclusion: "read": {"exclusion": "denied"}

        Args:
            permission: Permission name (e.g., "read", "write", "execute")

        Returns:
            List of relation names that grant this permission.
            Empty list if permission not defined (fail-safe: deny by default).
            For complex operators (union/intersection/exclusion), returns the flattened list.

        Example:
            >>> ns.get_permission_usersets("read")
            ["viewer", "editor", "owner"]
        """
        permissions_config = self.config.get("permissions", {})
        perm_def = permissions_config.get(permission, [])

        # Case 1: Simple list
        if isinstance(perm_def, list):
            return list(perm_def)

        # Case 2: Dict with operators (union, intersection, exclusion)
        if isinstance(perm_def, dict):
            # Try union first (most common)
            if "union" in perm_def:
                union_list = perm_def["union"]
                return list(union_list) if isinstance(union_list, list) else []
            # Try intersection
            if "intersection" in perm_def:
                intersection_list = perm_def["intersection"]
                return list(intersection_list) if isinstance(intersection_list, list) else []
            # Try exclusion (returns single item)
            if "exclusion" in perm_def:
                exclusion = perm_def["exclusion"]
                return [exclusion] if isinstance(exclusion, str) else []

        return []

    def has_permission(self, permission: str) -> bool:
        """Check if a permission is defined in this namespace.

        Args:
            permission: Permission name

        Returns:
            True if permission is defined
        """
        permissions_config = self.config.get("permissions", {})
        return permission in permissions_config

    def has_intersection(self, relation: str) -> bool:
        """Check if relation is defined as an intersection (AND logic).

        Args:
            relation: Relation name

        Returns:
            True if relation uses intersection
        """
        rel_config = self.get_relation_config(relation)
        return rel_config is not None and "intersection" in rel_config

    def get_intersection_relations(self, relation: str) -> list[str]:
        """Get the list of relations in an intersection.

        Args:
            relation: Relation name

        Returns:
            List of relations that must all be true (AND logic)
        """
        rel_config = self.get_relation_config(relation)
        if rel_config and "intersection" in rel_config:
            intersection_list = rel_config["intersection"]
            return list(intersection_list) if isinstance(intersection_list, list) else []
        return []

    def has_exclusion(self, relation: str) -> bool:
        """Check if relation is defined as an exclusion (NOT logic).

        Args:
            relation: Relation name

        Returns:
            True if relation uses exclusion
        """
        rel_config = self.get_relation_config(relation)
        return rel_config is not None and "exclusion" in rel_config

    def get_exclusion_relation(self, relation: str) -> str | None:
        """Get the relation to exclude (NOT logic).

        Args:
            relation: Relation name

        Returns:
            Relation name to exclude, or None
        """
        rel_config = self.get_relation_config(relation)
        if rel_config and "exclusion" in rel_config:
            exclusion = rel_config["exclusion"]
            return str(exclusion) if isinstance(exclusion, str) else None
        return None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "namespace_id": self.namespace_id,
            "object_type": self.object_type,
            "config": json.dumps(self.config),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NamespaceConfig:
        """Create from dictionary."""
        return cls(
            namespace_id=data["namespace_id"],
            object_type=data["object_type"],
            config=json.loads(data["config"])
            if isinstance(data["config"], str)
            else data["config"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )


@dataclass
class CheckCacheEntry:
    """Cache entry for permission check results.

    Attributes:
        cache_id: Unique identifier
        subject: Subject entity
        permission: Permission being checked
        object: Object entity
        result: Whether permission is granted
        computed_at: When the result was computed
        expires_at: When the cache entry expires
    """

    cache_id: str
    subject: Entity
    permission: str
    object: Entity
    result: bool
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Validate cache entry."""
        if not self.cache_id:
            self.cache_id = str(uuid.uuid4())
        if not isinstance(self.subject, Entity):
            raise TypeError(f"subject must be Entity, got {type(self.subject)}")
        if not isinstance(self.object, Entity):
            raise TypeError(f"object must be Entity, got {type(self.object)}")
        if not self.permission:
            raise ValueError("permission is required")

    def is_expired(self) -> bool:
        """Check if cache entry has expired."""
        return datetime.now(UTC) > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "cache_id": self.cache_id,
            "subject_type": self.subject.entity_type,
            "subject_id": self.subject.entity_id,
            "permission": self.permission,
            "object_type": self.object.entity_type,
            "object_id": self.object.entity_id,
            "result": self.result,
            "computed_at": self.computed_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckCacheEntry:
        """Create from dictionary."""
        return cls(
            cache_id=data["cache_id"],
            subject=Entity(data["subject_type"], data["subject_id"]),
            permission=data["permission"],
            object=Entity(data["object_type"], data["object_id"]),
            result=data["result"],
            computed_at=datetime.fromisoformat(data["computed_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
        )


@dataclass
class ChangelogEntry:
    """Changelog entry for tracking ReBAC tuple changes.

    Used for cache invalidation and audit trail.

    Attributes:
        change_id: Unique identifier (auto-increment)
        change_type: Type of change (INSERT, DELETE)
        tuple_id: ID of affected tuple
        subject: Subject entity
        relation: Relation type
        object: Object entity
        created_at: When the change occurred
    """

    change_id: int
    change_type: str
    subject: Entity
    relation: str
    object: Entity
    tuple_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Validate changelog entry."""
        if not isinstance(self.subject, Entity):
            raise TypeError(f"subject must be Entity, got {type(self.subject)}")
        if not isinstance(self.object, Entity):
            raise TypeError(f"object must be Entity, got {type(self.object)}")
        if self.change_type not in ("INSERT", "DELETE"):
            raise ValueError(f"change_type must be INSERT or DELETE, got {self.change_type}")

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "change_id": self.change_id,
            "change_type": self.change_type,
            "tuple_id": self.tuple_id,
            "subject_type": self.subject.entity_type,
            "subject_id": self.subject.entity_id,
            "relation": self.relation,
            "object_type": self.object.entity_type,
            "object_id": self.object.entity_id,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangelogEntry:
        """Create from dictionary."""
        return cls(
            change_id=data["change_id"],
            change_type=data["change_type"],
            tuple_id=data.get("tuple_id"),
            subject=Entity(data["subject_type"], data["subject_id"]),
            relation=data["relation"],
            object=Entity(data["object_type"], data["object_id"]),
            created_at=datetime.fromisoformat(data["created_at"]),
        )


# Default namespace configurations for common object types
DEFAULT_FILE_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid4()),
    object_type="file",
    config={
        "relations": {
            # Structural relation: parent directory
            "parent": {},
            # Direct relations (granted explicitly)
            "direct_owner": {},
            "direct_editor": {},
            "direct_viewer": {},
            # Parent inheritance via tupleToUserset
            # FIX: Use 'owner'/'editor'/'viewer' (not direct_*) to enable RECURSIVE parent inheritance
            # This allows permission to propagate up the entire parent chain until finding direct_owner
            # Example: /a/b/c/d/file.txt can inherit from /a if admin has direct_owner on /a
            # The recursion is bounded by max_depth and parent chain length (typically < 10 levels)
            "parent_owner": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "owner"}},
            "parent_editor": {
                "tupleToUserset": {"tupleset": "parent", "computedUserset": "editor"}
            },
            "parent_viewer": {
                "tupleToUserset": {"tupleset": "parent", "computedUserset": "viewer"}
            },
            # Group-based permissions via tupleToUserset
            "group_owner": {
                "tupleToUserset": {"tupleset": "direct_owner", "computedUserset": "member"}
            },
            "group_editor": {
                "tupleToUserset": {"tupleset": "direct_editor", "computedUserset": "member"}
            },
            "group_viewer": {
                "tupleToUserset": {"tupleset": "direct_viewer", "computedUserset": "member"}
            },
            # Cross-tenant sharing relations (PR #645)
            # These enable share_with_user() to grant access across tenant boundaries
            # Inheritance works via parent_* relations checking viewer/editor/owner unions
            "shared-viewer": {},  # Read access via cross-tenant share
            "shared-editor": {},  # Read + Write access via cross-tenant share
            "shared-owner": {},  # Full access via cross-tenant share
            # Computed relations (union of direct + parent + group + shared)
            # HYBRID: Keep unions for better memoization caching
            # Permission checks → 3 unions (viewer, editor, owner) instead of 9 relations
            # This gives better cache hit rates since many files share the same union checks
            # IMPORTANT: Don't nest unions (e.g., editor includes owner) - causes exponential checks
            # Note: Higher permission levels include lower ones (owner has editor, editor has viewer)
            "owner": {"union": ["direct_owner", "parent_owner", "group_owner", "shared-owner"]},
            "editor": {
                "union": [
                    "direct_editor",
                    "parent_editor",
                    "group_editor",
                    "shared-editor",
                    "shared-owner",
                ]
            },
            "viewer": {
                "union": [
                    "direct_viewer",
                    "parent_viewer",
                    "group_viewer",
                    "shared-viewer",
                    "shared-editor",
                    "shared-owner",
                ]
            },
        },
        # P0-1: Explicit permission-to-userset mapping (Zanzibar-style)
        # HYBRID OPTIMIZATION: Use unions for better memoization
        # Checking "viewer" on file1 and file2 uses same cache key
        # vs flattened schema where each of 9 relations needs separate cache entry
        # Result: ~3x fewer cache misses, better performance
        # PERF FIX: Check direct relations (owner, editor) BEFORE expensive traversals (viewer)
        # PERF FIX: Check direct relations first before expensive parent traversals
        # editor/viewer have direct_* relations that are found quickly
        # owner has parent_owner which triggers recursive parent traversal and can hit query limits
        "permissions": {
            "read": [
                "editor",
                "viewer",
                "owner",
            ],  # Check editor/viewer first, owner last (expensive)
            "write": ["editor", "owner"],  # Check editor first (direct), owner last (expensive)
            "execute": ["owner"],  # Execute = owner only
        },
    },
)

DEFAULT_GROUP_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid4()),
    object_type="group",
    config={
        "relations": {
            # Direct membership
            "member": {},
            # Group admin
            "admin": {},
            # Viewer can see group members
            "viewer": {"union": ["admin", "member"]},
        },
        # P0-1: Explicit permission-to-userset mapping
        "permissions": {
            "read": ["viewer", "member", "admin"],  # Read = can view group
            "write": ["admin"],  # Write = admin only
            "manage": ["admin"],  # Manage = admin only
        },
    },
)

DEFAULT_MEMORY_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid4()),
    object_type="memory",
    config={
        "relations": {
            # Direct relations (granted explicitly)
            "owner": {},
            "editor": {},
            "viewer": {},
        },
        # P0-1: Explicit permission-to-userset mapping
        "permissions": {
            "read": ["viewer", "editor", "owner"],  # Read = viewer OR editor OR owner
            "write": ["editor", "owner"],  # Write = editor OR owner
            "execute": ["owner"],  # Execute = owner only
        },
    },
)

# v0.5.0 ACE: Playbook namespace
DEFAULT_PLAYBOOK_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid4()),
    object_type="playbook",
    config={
        "relations": {
            # Direct relations (granted explicitly)
            "owner": {},
            "editor": {},
            "viewer": {},
        },
        # P0-1: Explicit permission-to-userset mapping
        "permissions": {
            "read": ["viewer", "editor", "owner"],  # Read = viewer OR editor OR owner
            "write": ["editor", "owner"],  # Write = editor OR owner
            "delete": ["owner"],  # Delete = owner only
        },
    },
)

# v0.5.0 ACE: Trajectory namespace
DEFAULT_TRAJECTORY_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid4()),
    object_type="trajectory",
    config={
        "relations": {
            # Direct relations (granted explicitly)
            "owner": {},
            "viewer": {},
        },
        # P0-1: Explicit permission-to-userset mapping
        "permissions": {
            "read": ["viewer", "owner"],  # Read = viewer OR owner
            "write": ["owner"],  # Write = owner only (trajectories typically write-once)
            "delete": ["owner"],  # Delete = owner only
        },
    },
)

# v0.5.0 Skills System: Skill namespace
DEFAULT_SKILL_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid4()),
    object_type="skill",
    config={
        "relations": {
            # Direct ownership relations
            "owner": {},  # Full control over skill
            "editor": {},  # Can modify skill content
            "viewer": {},  # Can read and fork skill
            # Tenant membership for skill access
            "tenant": {},  # Skill belongs to this tenant
            "tenant_member": {  # Inherit viewer from tenant membership
                "tupleToUserset": {"tupleset": "tenant", "computedUserset": "member"}
            },
            # Public/system skill access
            "public": {},  # Globally readable (system skills)
            # Governance roles
            "approver": {},  # Can approve skill for publication
        },
        # P0-1: Explicit permission-to-userset mapping
        "permissions": {
            "read": ["viewer", "editor", "owner", "tenant_member", "public"],
            "write": ["editor", "owner"],
            "delete": ["owner"],
            "fork": ["viewer", "editor", "owner", "tenant_member", "public"],
            "publish": ["owner"],  # Requires ownership (+ approval in workflow)
            "approve": ["approver"],  # Can approve for publication
        },
    },
)
