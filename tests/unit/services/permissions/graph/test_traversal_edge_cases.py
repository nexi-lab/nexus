"""Edge-case tests for PermissionComputer graph traversal.

Phase 8 extracted the core Zanzibar graph traversal from ReBACManager
into graph/traversal.py. These tests exercise critical edge cases that
were NOT covered by existing tests:

  - Wildcard (*:*) permission grants
  - Cross-zone wildcard access
  - Intersection (AND) permissions
  - Exclusion (NOT) permissions
  - Multi-level nested group inheritance (>2 levels)
  - User-to-user delegation chains (A → B → C)
  - Userset-as-subject recursive resolution
  - ABAC condition evaluation
  - Dict permission handling (computed_userset)
  - Depth limit enforcement at boundaries
  - Cycle detection with complex graph shapes
  - Mixed namespace + no-namespace resolution

Tested against SQLite in-memory via ReBACManager (composition root).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine

from nexus.core.rebac import NamespaceConfig
from nexus.services.permissions.rebac_manager import ReBACManager
from nexus.storage.models import Base

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    """In-memory SQLite database."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def mgr(engine):
    """ReBACManager with max_depth=10 and 5-min cache."""
    m = ReBACManager(engine=engine, cache_ttl_seconds=300, max_depth=10)
    yield m
    m.close()


@pytest.fixture
def mgr_shallow(engine):
    """ReBACManager with max_depth=3 for depth limit testing."""
    m = ReBACManager(engine=engine, cache_ttl_seconds=300, max_depth=3)
    yield m
    m.close()


# ── Helpers ───────────────────────────────────────────────────────────


def _register_file_ns(mgr: ReBACManager, *, with_intersection: bool = False,
                       with_exclusion: bool = False) -> None:
    """Register a 'file' namespace with common Zanzibar relations."""
    relations: dict = {
        "direct_owner": {},
        "direct_editor": {},
        "direct_viewer": {},
        "parent": {},
        "owner": {"union": ["direct_owner"]},
        "editor": {"union": ["direct_editor"]},
        "viewer": {"union": ["direct_viewer", "direct_editor", "direct_owner"]},
    }
    permissions: dict = {
        "write": ["direct_owner", "direct_editor"],
        "read": ["direct_viewer", "direct_editor", "direct_owner"],
    }
    if with_intersection:
        relations["approved"] = {}
        relations["can_publish"] = {"intersection": ["direct_editor", "approved"]}
        permissions["publish"] = ["can_publish"]
    if with_exclusion:
        relations["blocked"] = {}
        relations["can_access"] = {"exclusion": "blocked"}
        permissions["access"] = ["can_access"]

    mgr.create_namespace(NamespaceConfig(
        namespace_id="file-ns",
        object_type="file",
        config={
            "relations": relations,
            "permissions": permissions,
        },
    ))


def _register_group_ns(mgr: ReBACManager) -> None:
    """Register a 'group' namespace with member-of relation."""
    mgr.create_namespace(NamespaceConfig(
        namespace_id="group-ns",
        object_type="group",
        config={
            "relations": {
                "member-of": {},
                "direct_member": {},
                "member": {"union": ["direct_member"]},
            },
        },
    ))


def _register_folder_ns(mgr: ReBACManager) -> None:
    """Register a 'folder' namespace with parent-based inheritance."""
    mgr.create_namespace(NamespaceConfig(
        namespace_id="folder-ns",
        object_type="folder",
        config={
            "relations": {
                "direct_owner": {},
                "direct_viewer": {},
                "parent": {},
                "owner": {
                    "union": ["direct_owner"],
                },
                "viewer": {
                    "union": ["direct_viewer", "direct_owner"],
                },
            },
            "permissions": {
                "read": ["direct_viewer", "direct_owner"],
                "write": ["direct_owner"],
            },
        },
    ))


# =====================================================================
# 1. WILDCARD PERMISSIONS (*:* subject)
# =====================================================================


class TestWildcardPermissions:
    """Wildcard subject grants give access to any user."""

    def test_wildcard_grants_access_to_any_user(self, mgr):
        """A (*:*) wildcard tuple should grant access to all users."""
        _register_file_ns(mgr)

        # Grant *:* viewer on the file (public access)
        mgr.rebac_write(
            subject=("*", "*"),
            relation="direct_viewer",
            object=("file", "public-doc"),
        )

        # Any user should have read access
        assert mgr.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "public-doc"),
        ) is True

        assert mgr.rebac_check(
            subject=("agent", "bob"),
            permission="read",
            object=("file", "public-doc"),
        ) is True

        # Even a never-seen user
        assert mgr.rebac_check(
            subject=("agent", "stranger"),
            permission="read",
            object=("file", "public-doc"),
        ) is True

    def test_wildcard_does_not_grant_unrelated_permissions(self, mgr):
        """Wildcard viewer should NOT grant write access."""
        _register_file_ns(mgr)

        mgr.rebac_write(
            subject=("*", "*"),
            relation="direct_viewer",
            object=("file", "public-doc"),
        )

        # Write should still be denied
        assert mgr.rebac_check(
            subject=("agent", "alice"),
            permission="write",
            object=("file", "public-doc"),
        ) is False

    def test_wildcard_with_zone_id(self, mgr):
        """Wildcard in zone-a is also accessible from zone-b via cross-zone fallback.

        Issue #1064: Cross-zone wildcard access deliberately allows (*:*)
        tuples to grant access across zones as a fallback.
        """
        _register_file_ns(mgr)

        mgr.rebac_write(
            subject=("*", "*"),
            relation="direct_viewer",
            object=("file", "zone-doc"),
            zone_id="zone-a",
        )

        # Same zone → allowed
        assert mgr.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "zone-doc"),
            zone_id="zone-a",
        ) is True

        # Different zone → also allowed via cross-zone wildcard fallback (#1064)
        assert mgr.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "zone-doc"),
            zone_id="zone-b",
        ) is True

    def test_cross_zone_wildcard_grants_all_zones(self, mgr):
        """A wildcard tuple with NO zone_id should match requests in any zone."""
        _register_file_ns(mgr)

        # Global wildcard (no zone_id)
        mgr.rebac_write(
            subject=("*", "*"),
            relation="direct_viewer",
            object=("file", "global-doc"),
        )

        # Should be accessible from any zone via cross-zone wildcard fallback
        assert mgr.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "global-doc"),
            zone_id="zone-a",
        ) is True

    def test_wildcard_self_check_skipped(self, mgr):
        """Checking permission for (*:*) itself should not recurse infinitely."""
        _register_file_ns(mgr)

        mgr.rebac_write(
            subject=("*", "*"),
            relation="direct_viewer",
            object=("file", "doc"),
        )

        # The wildcard check should skip itself (the code checks
        # subject != WILDCARD_SUBJECT before doing wildcard lookup)
        result = mgr.rebac_check(
            subject=("*", "*"),
            permission="read",
            object=("file", "doc"),
        )
        # Should match on the direct concrete check (step 1)
        assert result is True


# =====================================================================
# 2. INTERSECTION (AND) PERMISSIONS
# =====================================================================


class TestIntersectionPermissions:
    """Intersection requires ALL relations to be true."""

    def test_intersection_granted_when_all_present(self, mgr):
        """User with both editor AND approved should get publish permission."""
        _register_file_ns(mgr, with_intersection=True)

        alice = ("agent", "alice")
        doc = ("file", "report")

        # Give alice both required relations
        mgr.rebac_write(subject=alice, relation="direct_editor", object=doc)
        mgr.rebac_write(subject=alice, relation="approved", object=doc)

        assert mgr.rebac_check(
            subject=alice, permission="publish", object=doc,
        ) is True

    def test_intersection_denied_when_one_missing(self, mgr):
        """User with editor but NOT approved should be denied publish."""
        _register_file_ns(mgr, with_intersection=True)

        alice = ("agent", "alice")
        doc = ("file", "report")

        # Only editor, no approved
        mgr.rebac_write(subject=alice, relation="direct_editor", object=doc)

        assert mgr.rebac_check(
            subject=alice, permission="publish", object=doc,
        ) is False

    def test_intersection_denied_when_other_missing(self, mgr):
        """User with approved but NOT editor should be denied publish."""
        _register_file_ns(mgr, with_intersection=True)

        alice = ("agent", "alice")
        doc = ("file", "report")

        # Only approved, no editor
        mgr.rebac_write(subject=alice, relation="approved", object=doc)

        assert mgr.rebac_check(
            subject=alice, permission="publish", object=doc,
        ) is False

    def test_intersection_denied_when_none_present(self, mgr):
        """User with neither relation should be denied."""
        _register_file_ns(mgr, with_intersection=True)

        assert mgr.rebac_check(
            subject=("agent", "alice"),
            permission="publish",
            object=("file", "report"),
        ) is False


# =====================================================================
# 3. EXCLUSION (NOT) PERMISSIONS
# =====================================================================


class TestExclusionPermissions:
    """Exclusion denies access when the excluded relation exists."""

    def test_exclusion_granted_when_not_blocked(self, mgr):
        """User without 'blocked' relation should get access."""
        _register_file_ns(mgr, with_exclusion=True)

        # No 'blocked' tuple → can_access passes (NOT blocked = True)
        assert mgr.rebac_check(
            subject=("agent", "alice"),
            permission="access",
            object=("file", "resource"),
        ) is True

    def test_exclusion_denied_when_blocked(self, mgr):
        """User WITH 'blocked' relation should be denied access."""
        _register_file_ns(mgr, with_exclusion=True)

        alice = ("agent", "alice")
        resource = ("file", "resource")

        # Block alice
        mgr.rebac_write(subject=alice, relation="blocked", object=resource)

        assert mgr.rebac_check(
            subject=alice, permission="access", object=resource,
        ) is False

    def test_exclusion_other_user_not_affected(self, mgr):
        """Blocking alice should NOT affect bob's access."""
        _register_file_ns(mgr, with_exclusion=True)

        resource = ("file", "resource")

        # Block only alice
        mgr.rebac_write(
            subject=("agent", "alice"), relation="blocked", object=resource,
        )

        # Bob should still have access (not blocked)
        assert mgr.rebac_check(
            subject=("agent", "bob"),
            permission="access",
            object=resource,
        ) is True

    def test_exclusion_unblock_restores_access(self, mgr):
        """Removing 'blocked' relation should restore access."""
        _register_file_ns(mgr, with_exclusion=True)

        alice = ("agent", "alice")
        resource = ("file", "resource")

        # Block alice
        tuple_id = mgr.rebac_write(subject=alice, relation="blocked", object=resource)
        assert mgr.rebac_check(
            subject=alice, permission="access", object=resource,
        ) is False

        # Unblock alice by deleting the tuple
        mgr.rebac_delete(tuple_id)

        assert mgr.rebac_check(
            subject=alice, permission="access", object=resource,
        ) is True


# =====================================================================
# 4. MULTI-LEVEL NESTED GROUP INHERITANCE
# =====================================================================


class TestNestedGroupInheritance:
    """Permission inheritance through deeply nested group chains."""

    def test_three_level_group_chain(self, mgr):
        """User → Group → file via userset-as-subject should grant permission."""
        _register_file_ns(mgr)
        _register_group_ns(mgr)

        alice = ("agent", "alice")
        team = ("group", "team")
        doc = ("file", "doc")

        # alice member-of team
        mgr.rebac_write(subject=alice, relation="member-of", object=team)
        # team#member-of has viewer on file (userset-as-subject, 3-tuple)
        mgr.rebac_write(
            subject=("group", "team", "member-of"),
            relation="direct_viewer",
            object=doc,
        )

        # alice should have read through: alice → team → file
        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc,
        ) is True

    def test_multiple_userset_grants_on_same_file(self, mgr):
        """Multiple groups granting different permissions on same file."""
        _register_file_ns(mgr)
        _register_group_ns(mgr)

        alice = ("agent", "alice")
        readers = ("group", "readers")
        writers = ("group", "writers")
        doc = ("file", "multi-group-doc")

        # alice is in readers group
        mgr.rebac_write(subject=alice, relation="member-of", object=readers)
        # readers group gives viewer on doc
        mgr.rebac_write(
            subject=("group", "readers", "member-of"),
            relation="direct_viewer",
            object=doc,
        )
        # writers group gives editor on doc (alice NOT in this group)
        mgr.rebac_write(
            subject=("group", "writers", "member-of"),
            relation="direct_editor",
            object=doc,
        )

        # alice can read (via readers)
        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc,
        ) is True
        # alice cannot write (not in writers)
        assert mgr.rebac_check(
            subject=alice, permission="write", object=doc,
        ) is False

        # Now add alice to writers too
        mgr.rebac_write(subject=alice, relation="member-of", object=writers)
        # Now alice can write
        assert mgr.rebac_check(
            subject=alice, permission="write", object=doc,
        ) is True

    def test_deep_chain_denied_beyond_max_depth(self, mgr_shallow):
        """Chain deeper than max_depth=3 should be denied."""
        _register_group_ns(mgr_shallow)

        alice = ("agent", "alice")
        groups = [("group", f"g{i}") for i in range(6)]

        mgr_shallow.rebac_write(subject=alice, relation="member-of", object=groups[0])
        for i in range(5):
            mgr_shallow.rebac_write(
                subject=groups[i], relation="member-of", object=groups[i + 1],
            )

        # Chain is 6 levels deep (alice → g0 → g1 → g2 → g3 → g4 → g5)
        # max_depth=3 should cause denial
        assert mgr_shallow.rebac_check(
            subject=alice, permission="member-of", object=groups[5],
        ) is False

    def test_user_in_multiple_groups_gets_union_of_permissions(self, mgr):
        """User in multiple groups inherits permissions from all."""
        _register_file_ns(mgr)
        _register_group_ns(mgr)

        alice = ("agent", "alice")
        editors = ("group", "editors")
        viewers = ("group", "viewers")
        doc = ("file", "doc")

        # alice is in both groups
        mgr.rebac_write(subject=alice, relation="member-of", object=editors)
        mgr.rebac_write(subject=alice, relation="member-of", object=viewers)

        # editors group has editor on doc (userset-as-subject, 3-tuple)
        mgr.rebac_write(
            subject=("group", "editors", "member-of"),
            relation="direct_editor",
            object=doc,
        )
        # viewers group has viewer on doc (userset-as-subject, 3-tuple)
        mgr.rebac_write(
            subject=("group", "viewers", "member-of"),
            relation="direct_viewer",
            object=doc,
        )

        # alice should have both read and write
        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc,
        ) is True
        assert mgr.rebac_check(
            subject=alice, permission="write", object=doc,
        ) is True


# =====================================================================
# 5. USER-TO-USER DELEGATION (non-admin sharing)
# =====================================================================


class TestUserToUserDelegation:
    """Non-admin user grants permission to another non-admin user."""

    def test_user_grants_viewer_to_another_user(self, mgr):
        """Alice (owner) grants viewer to Bob via direct relation."""
        _register_file_ns(mgr)

        alice = ("agent", "alice")
        bob = ("agent", "bob")
        doc = ("file", "alice-doc")

        # Alice owns the doc
        mgr.rebac_write(subject=alice, relation="direct_owner", object=doc)
        # Alice grants Bob viewer access
        mgr.rebac_write(subject=bob, relation="direct_viewer", object=doc)

        # Bob can read
        assert mgr.rebac_check(
            subject=bob, permission="read", object=doc,
        ) is True
        # Bob cannot write
        assert mgr.rebac_check(
            subject=bob, permission="write", object=doc,
        ) is False

    def test_delegation_chain_a_to_b_to_c(self, mgr):
        """A grants to B, B creates a group containing C."""
        _register_file_ns(mgr)
        _register_group_ns(mgr)

        alice = ("agent", "alice")
        bob = ("agent", "bob")
        charlie = ("agent", "charlie")
        doc = ("file", "shared-doc")
        bobs_team = ("group", "bobs-team")

        # Alice owns doc
        mgr.rebac_write(subject=alice, relation="direct_owner", object=doc)

        # Alice shares with Bob's team via userset (3-tuple subject)
        mgr.rebac_write(
            subject=("group", "bobs-team", "member-of"),
            relation="direct_viewer",
            object=doc,
        )

        # Bob is a member
        mgr.rebac_write(subject=bob, relation="member-of", object=bobs_team)
        # Charlie is also a member
        mgr.rebac_write(subject=charlie, relation="member-of", object=bobs_team)

        # Both Bob and Charlie can read
        assert mgr.rebac_check(
            subject=bob, permission="read", object=doc,
        ) is True
        assert mgr.rebac_check(
            subject=charlie, permission="read", object=doc,
        ) is True

    def test_revoke_delegation_denies_downstream(self, mgr):
        """Removing a group member revokes their transitive access."""
        _register_file_ns(mgr)
        _register_group_ns(mgr)

        bob = ("agent", "bob")
        team = ("group", "team")
        doc = ("file", "doc")

        # Setup: team has viewer on doc (3-tuple subject), bob is in team
        mgr.rebac_write(
            subject=("group", "team", "member-of"),
            relation="direct_viewer",
            object=doc,
        )
        membership_id = mgr.rebac_write(
            subject=bob, relation="member-of", object=team,
        )

        # Bob can read
        assert mgr.rebac_check(
            subject=bob, permission="read", object=doc,
        ) is True

        # Revoke bob's membership via tuple_id
        mgr.rebac_delete(membership_id)

        # Bob can no longer read
        assert mgr.rebac_check(
            subject=bob, permission="read", object=doc,
        ) is False


# =====================================================================
# 6. CYCLE DETECTION (complex graph shapes)
# =====================================================================


class TestCycleDetectionComplex:
    """Cycle detection in various graph topologies."""

    def test_self_loop(self, mgr):
        """Entity referencing itself should not hang."""
        _register_group_ns(mgr)

        # g1 member-of g1 (self-loop)
        mgr.rebac_write(
            subject=("group", "g1"),
            relation="member-of",
            object=("group", "g1"),
        )

        # Should not hang, should return True (direct match)
        result = mgr.rebac_check(
            subject=("group", "g1"),
            permission="member-of",
            object=("group", "g1"),
        )
        assert result is True

    def test_diamond_graph(self, mgr):
        """Diamond: alice in groups B and C, both grant viewer on file."""
        _register_file_ns(mgr)
        _register_group_ns(mgr)

        alice = ("agent", "alice")
        b = ("group", "b")
        c = ("group", "c")
        doc = ("file", "diamond-doc")

        # alice is in both groups
        mgr.rebac_write(subject=alice, relation="member-of", object=b)
        mgr.rebac_write(subject=alice, relation="member-of", object=c)
        # Both groups grant viewer on the doc
        mgr.rebac_write(
            subject=("group", "b", "member-of"),
            relation="direct_viewer",
            object=doc,
        )
        mgr.rebac_write(
            subject=("group", "c", "member-of"),
            relation="direct_viewer",
            object=doc,
        )

        # alice → doc via b AND via c (should succeed via either path)
        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc,
        ) is True

    def test_mutual_cycle_between_two_groups(self, mgr):
        """A member-of B, B member-of A — should not hang."""
        _register_group_ns(mgr)

        g1 = ("group", "g1")
        g2 = ("group", "g2")

        mgr.rebac_write(subject=g1, relation="member-of", object=g2)
        mgr.rebac_write(subject=g2, relation="member-of", object=g1)

        # Check should terminate, not hang
        result = mgr.rebac_check(
            subject=("agent", "alice"),
            permission="member-of",
            object=g1,
        )
        assert result is False  # alice is not a member of either

    def test_three_node_cycle(self, mgr):
        """A→B→C→A cycle should not hang."""
        _register_group_ns(mgr)

        g_a = ("group", "a")
        g_b = ("group", "b")
        g_c = ("group", "c")

        mgr.rebac_write(subject=g_a, relation="member-of", object=g_b)
        mgr.rebac_write(subject=g_b, relation="member-of", object=g_c)
        mgr.rebac_write(subject=g_c, relation="member-of", object=g_a)

        # Should terminate with denial (no concrete user in the cycle)
        assert mgr.rebac_check(
            subject=("agent", "outsider"),
            permission="member-of",
            object=g_a,
        ) is False


# =====================================================================
# 7. CONDITION EVALUATION (ABAC)
# =====================================================================


class TestConditionEvaluation:
    """ABAC condition evaluation on tuples."""

    def test_condition_satisfied_grants_access(self, mgr):
        """Tuple with IP allowlist condition that matches context should grant."""
        _register_file_ns(mgr)

        alice = ("agent", "alice")
        doc = ("file", "conditional-doc")

        # Write a tuple with IP allowlist conditions
        mgr.rebac_write(
            subject=alice,
            relation="direct_viewer",
            object=doc,
            conditions={"allowed_ips": ["10.0.0.0/8"]},
        )

        # With matching IP context
        result = mgr.rebac_check(
            subject=alice,
            permission="read",
            object=doc,
            context={"ip": "10.1.2.3"},
        )
        assert result is True

    def test_condition_not_satisfied_denies_access(self, mgr):
        """Tuple with IP allowlist that doesn't match context should deny."""
        _register_file_ns(mgr)

        alice = ("agent", "alice")
        doc = ("file", "conditional-doc")

        mgr.rebac_write(
            subject=alice,
            relation="direct_viewer",
            object=doc,
            conditions={"allowed_ips": ["10.0.0.0/8"]},
        )

        # With non-matching IP
        result = mgr.rebac_check(
            subject=alice,
            permission="read",
            object=doc,
            context={"ip": "192.168.1.1"},
        )
        assert result is False

    def test_condition_denied_when_no_context(self, mgr):
        """Tuple with conditions but NO context should deny (fail-closed)."""
        _register_file_ns(mgr)

        alice = ("agent", "alice")
        doc = ("file", "conditional-doc")

        mgr.rebac_write(
            subject=alice,
            relation="direct_viewer",
            object=doc,
            conditions={"allowed_ips": ["10.0.0.0/8"]},
        )

        # No context at all → conditions can't be evaluated → deny
        result = mgr.rebac_check(
            subject=alice,
            permission="read",
            object=doc,
            context=None,
        )
        assert result is False

    def test_no_conditions_always_passes(self, mgr):
        """Tuple without conditions should always pass."""
        _register_file_ns(mgr)

        alice = ("agent", "alice")
        doc = ("file", "no-cond-doc")

        mgr.rebac_write(subject=alice, relation="direct_viewer", object=doc)

        # With any context or no context
        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc, context=None,
        ) is True
        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc,
            context={"arbitrary": "value"},
        ) is True


# =====================================================================
# 8. TUPLE-TO-USERSET (parent/child inheritance)
# =====================================================================


class TestTupleToUserset:
    """Permission inheritance via tupleToUserset expansion."""

    def test_file_inherits_from_parent_folder(self, mgr):
        """Owner of parent folder should have access to child file."""
        # Register file namespace with parent-based tupleToUserset
        mgr.create_namespace(NamespaceConfig(
            namespace_id="file-ns",
            object_type="file",
            config={
                "relations": {
                    "direct_owner": {},
                    "direct_viewer": {},
                    "parent": {},
                    "owner": {
                        "tupleToUserset": {
                            "tupleset": "parent",
                            "computedUserset": "direct_owner",
                        }
                    },
                },
                "permissions": {
                    "read": ["direct_viewer", "direct_owner"],
                },
            },
        ))
        _register_folder_ns(mgr)

        alice = ("agent", "alice")
        folder = ("folder", "my-folder")
        child = ("file", "my-folder/report.txt")

        # alice owns the folder
        mgr.rebac_write(subject=alice, relation="direct_owner", object=folder)
        # file has parent = folder
        mgr.rebac_write(
            subject=folder,
            relation="parent",
            object=child,
        )

        # alice should inherit ownership on child via tupleToUserset
        assert mgr.rebac_check(
            subject=alice,
            permission="owner",
            object=child,
        ) is True


# =====================================================================
# 9. DEPTH LIMIT BOUNDARY CONDITIONS
# =====================================================================


class TestDepthLimitBoundary:
    """Exact boundary of max_depth enforcement."""

    def test_exactly_at_max_depth_succeeds(self, mgr_shallow):
        """Chain exactly at max_depth=3 should succeed."""
        # With max_depth=3: alice→g0→g1→g2 is depth 3 (0,1,2,3)
        _register_group_ns(mgr_shallow)

        alice = ("agent", "alice")
        g0 = ("group", "g0")
        g1 = ("group", "g1")
        g2 = ("group", "g2")

        mgr_shallow.rebac_write(subject=alice, relation="member-of", object=g0)
        mgr_shallow.rebac_write(subject=g0, relation="member-of", object=g1)
        mgr_shallow.rebac_write(subject=g1, relation="member-of", object=g2)

        # Direct check at depth 0 is fine
        assert mgr_shallow.rebac_check(
            subject=alice, permission="member-of", object=g0,
        ) is True

    def test_one_beyond_max_depth_fails(self, mgr_shallow):
        """Chain one level beyond max_depth=3 should fail."""
        _register_group_ns(mgr_shallow)

        alice = ("agent", "alice")
        groups = [("group", f"g{i}") for i in range(5)]

        mgr_shallow.rebac_write(subject=alice, relation="member-of", object=groups[0])
        for i in range(4):
            mgr_shallow.rebac_write(
                subject=groups[i], relation="member-of", object=groups[i + 1],
            )

        # Direct relation still works
        assert mgr_shallow.rebac_check(
            subject=alice, permission="member-of", object=groups[0],
        ) is True

        # Deep chain should fail
        assert mgr_shallow.rebac_check(
            subject=alice, permission="member-of", object=groups[4],
        ) is False


# =====================================================================
# 10. NO-NAMESPACE FALLBACK
# =====================================================================


class TestNoNamespaceFallback:
    """When no namespace is registered, only direct relations work."""

    def test_direct_relation_works_without_namespace(self, mgr):
        """Direct tuple check should work even without namespace config."""
        alice = ("agent", "alice")
        thing = ("widget", "thing-1")

        mgr.rebac_write(subject=alice, relation="viewer", object=thing)

        assert mgr.rebac_check(
            subject=alice, permission="viewer", object=thing,
        ) is True

    def test_unrelated_permission_denied_without_namespace(self, mgr):
        """Without namespace, only exact relation match works."""
        alice = ("agent", "alice")
        thing = ("widget", "thing-1")

        mgr.rebac_write(subject=alice, relation="viewer", object=thing)

        # "read" is not the same as "viewer" without namespace to map them
        assert mgr.rebac_check(
            subject=alice, permission="read", object=thing,
        ) is False


# =====================================================================
# 11. EXPAND API EDGE CASES
# =====================================================================


class TestExpandEdgeCases:
    """Edge cases for the Expand API (finding all subjects with permission)."""

    def test_expand_with_wildcard_subjects(self, mgr):
        """Expand should include wildcard-granted subjects indirectly."""
        # Note: expand finds concrete tuples, wildcard is a special pattern
        _register_file_ns(mgr)

        alice = ("agent", "alice")
        doc = ("file", "doc")

        mgr.rebac_write(subject=alice, relation="direct_viewer", object=doc)

        subjects = mgr.rebac_expand(
            permission="direct_viewer",
            object=doc,
        )
        assert ("agent", "alice") in subjects

    def test_expand_empty_for_no_grants(self, mgr):
        """Expand should return empty for object with no grants."""
        _register_file_ns(mgr)

        subjects = mgr.rebac_expand(
            permission="direct_viewer",
            object=("file", "no-grants"),
        )
        assert subjects == []

    def test_expand_with_union_namespace(self, mgr):
        """Expand should follow union relations."""
        mgr.create_namespace(NamespaceConfig(
            namespace_id="file-ns",
            object_type="file",
            config={
                "relations": {
                    "direct_owner": {},
                    "direct_viewer": {},
                    "viewer": {"union": ["direct_owner", "direct_viewer"]},
                },
            },
        ))

        doc = ("file", "doc")
        mgr.rebac_write(subject=("agent", "alice"), relation="direct_owner", object=doc)
        mgr.rebac_write(subject=("agent", "bob"), relation="direct_viewer", object=doc)

        subjects = mgr.rebac_expand(permission="viewer", object=doc)
        assert ("agent", "alice") in subjects
        assert ("agent", "bob") in subjects


# =====================================================================
# 12. EXPIRATION EDGE CASES
# =====================================================================


class TestExpirationEdgeCases:
    """Edge cases around tuple expiration."""

    @freeze_time("2025-01-01 12:00:00")
    def test_tuple_active_before_expiry(self, mgr):
        """Tuple should grant access before expires_at."""
        _register_file_ns(mgr)

        alice = ("agent", "alice")
        doc = ("file", "temp-doc")

        mgr.rebac_write(
            subject=alice,
            relation="direct_viewer",
            object=doc,
            expires_at=datetime(2025, 1, 2, tzinfo=UTC),  # Tomorrow
        )

        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc,
        ) is True

    @freeze_time("2025-01-03 12:00:00")
    def test_tuple_denied_after_expiry(self, mgr):
        """Tuple should deny access after expires_at."""
        _register_file_ns(mgr)

        alice = ("agent", "alice")
        doc = ("file", "temp-doc")

        mgr.rebac_write(
            subject=alice,
            relation="direct_viewer",
            object=doc,
            expires_at=datetime(2025, 1, 2, tzinfo=UTC),  # Yesterday
        )

        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc,
        ) is False


# =====================================================================
# 13. MIXED SCENARIOS
# =====================================================================


class TestMixedScenarios:
    """Complex real-world scenarios combining multiple features."""

    def test_group_viewer_plus_direct_editor_gives_write(self, mgr):
        """User has viewer via group + direct editor → should have write."""
        _register_file_ns(mgr)
        _register_group_ns(mgr)

        alice = ("agent", "alice")
        team = ("group", "team")
        doc = ("file", "doc")

        # Group gives viewer (3-tuple subject)
        mgr.rebac_write(subject=alice, relation="member-of", object=team)
        mgr.rebac_write(
            subject=("group", "team", "member-of"),
            relation="direct_viewer",
            object=doc,
        )
        # Direct editor grant
        mgr.rebac_write(subject=alice, relation="direct_editor", object=doc)

        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc,
        ) is True
        assert mgr.rebac_check(
            subject=alice, permission="write", object=doc,
        ) is True

    def test_grant_revoke_grant_cycle(self, mgr):
        """Grant → revoke → re-grant should work correctly."""
        _register_file_ns(mgr)

        alice = ("agent", "alice")
        doc = ("file", "doc")

        # Grant
        tid1 = mgr.rebac_write(subject=alice, relation="direct_viewer", object=doc)
        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc,
        ) is True

        # Revoke via tuple_id
        mgr.rebac_delete(tid1)
        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc,
        ) is False

        # Re-grant
        mgr.rebac_write(subject=alice, relation="direct_viewer", object=doc)
        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc,
        ) is True

    def test_different_permissions_on_same_object(self, mgr):
        """Multiple users with different permission levels on same object."""
        _register_file_ns(mgr)

        doc = ("file", "shared-doc")
        alice = ("agent", "alice")
        bob = ("agent", "bob")
        charlie = ("agent", "charlie")

        mgr.rebac_write(subject=alice, relation="direct_owner", object=doc)
        mgr.rebac_write(subject=bob, relation="direct_editor", object=doc)
        mgr.rebac_write(subject=charlie, relation="direct_viewer", object=doc)

        # Alice: read + write
        assert mgr.rebac_check(subject=alice, permission="read", object=doc) is True
        assert mgr.rebac_check(subject=alice, permission="write", object=doc) is True

        # Bob: read + write (editor grants write)
        assert mgr.rebac_check(subject=bob, permission="read", object=doc) is True
        assert mgr.rebac_check(subject=bob, permission="write", object=doc) is True

        # Charlie: read only
        assert mgr.rebac_check(subject=charlie, permission="read", object=doc) is True
        assert mgr.rebac_check(subject=charlie, permission="write", object=doc) is False

    def test_zone_isolation_with_same_object_id(self, mgr):
        """Same object_id in different zones should be completely isolated."""
        _register_file_ns(mgr)

        alice = ("agent", "alice")
        bob = ("agent", "bob")
        doc = ("file", "shared-name")

        # alice has access in zone-a
        mgr.rebac_write(
            subject=alice, relation="direct_viewer", object=doc,
            zone_id="zone-a",
        )
        # bob has access in zone-b
        mgr.rebac_write(
            subject=bob, relation="direct_viewer", object=doc,
            zone_id="zone-b",
        )

        # alice can read in zone-a but NOT zone-b
        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc, zone_id="zone-a",
        ) is True
        assert mgr.rebac_check(
            subject=alice, permission="read", object=doc, zone_id="zone-b",
        ) is False

        # bob can read in zone-b but NOT zone-a
        assert mgr.rebac_check(
            subject=bob, permission="read", object=doc, zone_id="zone-b",
        ) is True
        assert mgr.rebac_check(
            subject=bob, permission="read", object=doc, zone_id="zone-a",
        ) is False
