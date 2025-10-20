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
    - shared-with: Sharing relationship

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
from datetime import datetime
from enum import Enum
from typing import Any


class RelationType(str, Enum):
    """Standard relationship types in ReBAC."""

    MEMBER_OF = "member-of"
    OWNER_OF = "owner-of"
    VIEWER_OF = "viewer-of"
    EDITOR_OF = "editor-of"
    PARENT_OF = "parent-of"
    SHARED_WITH = "shared-with"


class EntityType(str, Enum):
    """Types of entities in ReBAC system."""

    AGENT = "agent"
    GROUP = "group"
    FILE = "file"
    WORKSPACE = "workspace"
    TENANT = "tenant"


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
    created_at: datetime = field(default_factory=datetime.utcnow)
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
        return datetime.utcnow() > self.expires_at

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
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

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
    computed_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime = field(default_factory=lambda: datetime.utcnow())

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
        return datetime.utcnow() > self.expires_at

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
    created_at: datetime = field(default_factory=datetime.utcnow)

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
            # Owner has full permissions
            "owner": {"union": ["direct_owner", "parent_owner"]},
            # Direct ownership
            "direct_owner": {},
            # Inherited from parent (e.g., folder owner)
            "parent_owner": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "owner"}},
            # Viewer can read
            "viewer": {"union": ["owner", "direct_viewer"]},
            "direct_viewer": {},
            # Editor can read and write
            "editor": {"union": ["owner", "direct_editor"]},
            "direct_editor": {},
        }
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
        }
    },
)
