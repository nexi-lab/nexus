"""Default ReBAC namespace configurations.

These define the default permission schemas for each object type
(file, group, memory, playbook, trajectory, skill).  The canonical
definition lives in the rebac brick since it owns namespace semantics.

Canonical import:
    from nexus.bricks.rebac.default_namespaces import DEFAULT_FILE_NAMESPACE

IMPORTANT: namespace_id MUST be deterministic (uuid5, not uuid4).
uuid4() generates a new ID on every import, which breaks the update
guard in _initialize_default_namespaces_with_conn() — after a server
restart the new UUID doesn't match the stored one, so the namespace
config is never updated.  uuid5(NEXUS_NS, object_type) is stable
across restarts while remaining unique per object type.
"""

import uuid

from nexus.bricks.rebac.domain import NamespaceConfig

# Fixed namespace for deterministic UUID generation.
# uuid5(NEXUS_NS, "file") always produces the same ID.
_NEXUS_NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# ---------------------------------------------------------------------------
# File namespace (complex: parent inheritance + group + cross-zone sharing)
# ---------------------------------------------------------------------------
DEFAULT_FILE_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid5(_NEXUS_NS, "file")),
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
            # Cross-zone sharing relations (PR #645)
            # These enable share_with_user() to grant access across zone boundaries
            # Inheritance works via parent_* relations checking viewer/editor/owner unions
            "shared-viewer": {},  # Read access via cross-zone share
            "shared-editor": {},  # Read + Write access via cross-zone share
            "shared-owner": {},  # Full access via cross-zone share
            # Computed relations (union of direct + parent + group + shared)
            # HYBRID: Keep unions for better memoization caching
            # Permission checks -> 3 unions (viewer, editor, owner) instead of 9 relations
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

# ---------------------------------------------------------------------------
# Group namespace
# ---------------------------------------------------------------------------
DEFAULT_GROUP_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid5(_NEXUS_NS, "group")),
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

# ---------------------------------------------------------------------------
# Memory namespace
# ---------------------------------------------------------------------------
DEFAULT_MEMORY_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid5(_NEXUS_NS, "memory")),
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

# ---------------------------------------------------------------------------
# v0.5.0 ACE: Playbook namespace
# ---------------------------------------------------------------------------
DEFAULT_PLAYBOOK_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid5(_NEXUS_NS, "playbook")),
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

# ---------------------------------------------------------------------------
# v0.5.0 ACE: Trajectory namespace
# ---------------------------------------------------------------------------
DEFAULT_TRAJECTORY_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid5(_NEXUS_NS, "trajectory")),
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

# ---------------------------------------------------------------------------
# Approvals namespace (Issue #3790)
#
# Drives ReBACCapabilityAuth on the ApprovalsV1 gRPC servicer. The brick
# treats ``("approvals", "global")`` as a single flat resource — zone
# scoping happens at the row level inside ApprovalService, not via ReBAC
# zone tuples — so a single namespace tuple per subject suffices. Operators
# (or auth_keys.py grants) can write any of the standard
# ``viewer``/``editor``/``owner`` direct relations against
# ``("approvals", "global")``; the relation→permission expansion below
# matches the three capability strings ``ApprovalsServicer`` checks today:
#
#   approvals:read    -> ReBAC ``read``   (viewer | editor | owner)
#   approvals:decide  -> ReBAC ``write``  (editor | owner)
#   approvals:request -> ReBAC ``create`` (editor | owner)
#
# Without this namespace registered the manager has no way to expand
# ``viewer -> read``: ``rebac_check`` falls back to looking for a literal
# ``read`` direct tuple, which never exists, and every non-admin call hits
# PERMISSION_DENIED even with a valid grant.
# ---------------------------------------------------------------------------
DEFAULT_APPROVALS_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid5(_NEXUS_NS, "approvals")),
    object_type="approvals",
    config={
        "relations": {
            # Direct relations granted explicitly via
            # POST /api/v2/rebac/tuples (or auth_keys.py grants).
            "viewer": {},
            "editor": {},
            "owner": {},
        },
        # Capability-string mapping consumed by
        # nexus.bricks.approvals.grpc_auth._CAPABILITY_TO_PERMISSION:
        #   approvals:read    -> "read"
        #   approvals:decide  -> "write"
        #   approvals:request -> "create"
        "permissions": {
            "read": ["viewer", "editor", "owner"],
            "write": ["editor", "owner"],
            "create": ["editor", "owner"],
        },
    },
)


# ---------------------------------------------------------------------------
# v0.5.0 Skills System: Skill namespace
# ---------------------------------------------------------------------------
DEFAULT_SKILL_NAMESPACE = NamespaceConfig(
    namespace_id=str(uuid.uuid5(_NEXUS_NS, "skill")),
    object_type="skill",
    config={
        "relations": {
            # Direct ownership relations
            "owner": {},  # Full control over skill
            "editor": {},  # Can modify skill content
            "viewer": {},  # Can read and fork skill
            # Zone membership for skill access
            "zone": {},  # Skill belongs to this zone
            "zone_member": {  # Inherit viewer from zone membership
                "tupleToUserset": {"tupleset": "zone", "computedUserset": "member"}
            },
            # Public/system skill access
            "public": {},  # Globally readable (system skills)
            # Governance roles
            "approver": {},  # Can approve skill for publication
        },
        # P0-1: Explicit permission-to-userset mapping
        "permissions": {
            "read": ["viewer", "editor", "owner", "zone_member", "public"],
            "write": ["editor", "owner"],
            "delete": ["owner"],
            "fork": ["viewer", "editor", "owner", "zone_member", "public"],
            "publish": ["owner"],  # Requires ownership (+ approval in workflow)
            "approve": ["approver"],  # Can approve for publication
        },
    },
)
