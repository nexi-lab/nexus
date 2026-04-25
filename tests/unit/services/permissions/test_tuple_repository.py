"""Unit tests for TupleRepository — data access layer for ReBAC relationship tuples.

Tests cover:
- Connection management (get/close/context manager)
- SQL dialect abstraction (SQLite placeholder conversion)
- Zone revision tracking (get/increment)
- Tuple query methods (subject_sets, related_objects, direct_subjects)
- Ancestor detection via recursive CTE
- Cross-zone validation
- ABAC condition evaluation
- Direct tuple lookup (concrete + wildcard)
- Bulk existence check
- Read/write engine separation (Issue #725)
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pyroaring")

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from nexus.bricks.rebac.consistency.metastore_version_store import MetastoreVersionStore
from nexus.bricks.rebac.domain import Entity
from nexus.bricks.rebac.tuples.repository import TupleRepository
from nexus.storage.models import Base
from tests.helpers.inmemory_nexus_fs import InMemoryNexusFS

# ============================================================================
# Fixtures
# ============================================================================

ZONE = "default"


@pytest.fixture
def engine():
    """Create in-memory SQLite database with ReBAC schema.

    Uses StaticPool so all engine.connect() calls share the same underlying
    DBAPI connection — required for SQLite :memory: where each connection
    otherwise creates a separate database.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def version_store():
    """Create an in-memory MetastoreVersionStore for zone revision tracking."""
    return MetastoreVersionStore(InMemoryNexusFS())


@pytest.fixture
def repo(engine, version_store):
    """Create a TupleRepository backed by in-memory SQLite."""
    return TupleRepository(engine=engine, version_store=version_store)


def _insert_tuple(
    conn,
    repo: TupleRepository,
    *,
    subject: tuple[str, str],
    relation: str,
    obj: tuple[str, str],
    zone_id: str = ZONE,
    subject_relation: str | None = None,
    expires_at: str | None = None,
    conditions: str | None = None,
    commit: bool = True,
) -> str:
    """Helper to insert a tuple row directly into the database.

    Note: Uses raw DBAPI conn.commit() — callers inside a ``repo.connection()``
    context manager should pass ``commit=False`` if they want the CM to handle
    transaction control (e.g., for rollback tests).
    """
    tid = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    cursor = repo.create_cursor(conn)
    cursor.execute(
        repo.fix_sql_placeholders(
            """
            INSERT INTO rebac_tuples
                (tuple_id, subject_type, subject_id, subject_relation,
                 relation, object_type, object_id,
                 zone_id, subject_zone_id, object_zone_id,
                 created_at, expires_at, conditions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        ),
        (
            tid,
            subject[0],
            subject[1],
            subject_relation,
            relation,
            obj[0],
            obj[1],
            zone_id,
            zone_id,
            zone_id,
            now,
            expires_at,
            conditions,
        ),
    )
    if commit:
        conn.commit()
    return tid


# ============================================================================
# Connection management
# ============================================================================


class TestConnectionManagement:
    """Tests for get_connection / close_connection / connection() context manager."""

    def test_get_and_close_connection(self, repo: TupleRepository):
        conn = repo.get_connection()
        assert conn is not None
        assert id(conn) in repo._conn_map
        repo.close_connection(conn)
        assert id(conn) not in repo._conn_map

    def test_close_unknown_connection(self, repo: TupleRepository):
        """Closing an unknown connection should not raise."""
        conn = repo.get_connection()
        conn_id = id(conn)
        repo._conn_map.pop(conn_id, None)
        repo.close_connection(conn)

    def test_data_persists_across_connections(self, repo: TupleRepository):
        """Data committed via raw DBAPI is visible from subsequent connections."""
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("agent", "alice"),
                relation="viewer-of",
                obj=("file", "readme"),
            )
        finally:
            repo.close_connection(conn)

        # Query from a fresh connection
        results = repo.find_subjects_with_relation(Entity("file", "readme"), "viewer-of")
        assert len(results) == 1
        assert results[0].entity_id == "alice"

    def test_uncommitted_data_not_visible_after_rollback(self, repo: TupleRepository):
        """Uncommitted data is rolled back when connection is closed without commit."""
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("agent", "alice"),
                relation="viewer-of",
                obj=("file", "readme"),
                commit=False,  # Don't commit
            )
            conn.rollback()
        finally:
            repo.close_connection(conn)

        results = repo.find_subjects_with_relation(Entity("file", "readme"), "viewer-of")
        assert len(results) == 0


# ============================================================================
# SQL dialect helpers
# ============================================================================


class TestSQLDialect:
    """Tests for fix_sql_placeholders and create_cursor."""

    def test_sqlite_placeholders_unchanged(self, repo: TupleRepository):
        """SQLite uses ? natively — no conversion."""
        sql = "SELECT * FROM t WHERE id = ? AND name = ?"
        assert repo.fix_sql_placeholders(sql) == sql

    def test_create_cursor_sets_row_factory(self, repo: TupleRepository):
        """SQLite cursor should use sqlite3.Row for dict-like access."""
        import sqlite3

        conn = repo.get_connection()
        try:
            cursor = repo.create_cursor(conn)
            assert conn.row_factory is sqlite3.Row
            cursor.execute("SELECT 1 AS val")
            row = cursor.fetchone()
            assert row["val"] == 1
        finally:
            repo.close_connection(conn)

    def test_supports_old_new_returning_false_for_sqlite(self, repo: TupleRepository):
        """SQLite never supports OLD/NEW RETURNING."""
        assert repo.supports_old_new_returning is False


# ============================================================================
# Zone revision tracking
# ============================================================================


class TestZoneRevision:
    """Tests for get_zone_revision / increment_zone_revision.

    Note: increment_zone_revision does NOT commit internally — it expects the
    caller to commit.  Raw DBAPI conn.commit() is used explicitly in these tests.
    """

    def test_get_revision_zero_for_new_zone(self, repo: TupleRepository):
        """A zone with no writes has revision 0."""
        assert repo.get_zone_revision("zone-a") == 0

    def test_increment_creates_and_returns_one(self, repo: TupleRepository):
        """First increment for a new zone creates the row and returns 1."""
        conn = repo.get_connection()
        try:
            rev = repo.increment_zone_revision("zone-a", conn)
            conn.commit()
        finally:
            repo.close_connection(conn)
        assert rev == 1

    def test_increment_monotonic(self, repo: TupleRepository):
        """Repeated increments produce 1, 2, 3, ..."""
        conn = repo.get_connection()
        try:
            for expected in range(1, 6):
                rev = repo.increment_zone_revision("zone-a", conn)
                conn.commit()
                assert rev == expected
        finally:
            repo.close_connection(conn)

    def test_get_after_increment(self, repo: TupleRepository):
        """get_zone_revision returns the value set by increment."""
        conn = repo.get_connection()
        try:
            repo.increment_zone_revision("zone-b", conn)
            repo.increment_zone_revision("zone-b", conn)
            repo.increment_zone_revision("zone-b", conn)
            conn.commit()
        finally:
            repo.close_connection(conn)
        assert repo.get_zone_revision("zone-b") == 3

    def test_zones_are_isolated(self, repo: TupleRepository):
        """Incrementing one zone doesn't affect another."""
        conn = repo.get_connection()
        try:
            repo.increment_zone_revision("z1", conn)
            repo.increment_zone_revision("z1", conn)
            conn.commit()
        finally:
            repo.close_connection(conn)

        conn = repo.get_connection()
        try:
            repo.increment_zone_revision("z2", conn)
            conn.commit()
        finally:
            repo.close_connection(conn)

        assert repo.get_zone_revision("z1") == 2
        assert repo.get_zone_revision("z2") == 1

    def test_default_zone_id(self, repo: TupleRepository):
        """None zone_id maps to 'root'."""
        conn = repo.get_connection()
        try:
            repo.increment_zone_revision(None, conn)
            conn.commit()
        finally:
            repo.close_connection(conn)
        assert repo.get_zone_revision(None) == 1
        assert repo.get_zone_revision("root") == 1

    def test_get_revision_with_provided_connection(self, repo: TupleRepository):
        """get_zone_revision with conn= parameter still works (delegates to version_store)."""
        # Seed a version via increment
        conn = repo.get_connection()
        try:
            for _ in range(42):
                repo.increment_zone_revision("manual", conn)
            rev = repo.get_zone_revision("manual", conn=conn)
            assert rev == 42
        finally:
            repo.close_connection(conn)


# ============================================================================
# Cross-zone validation
# ============================================================================


class TestCrossZoneValidation:
    """Tests for validate_cross_zone static method."""

    def test_same_zone_ok(self):
        TupleRepository.validate_cross_zone("z1", "z1", "z1")

    def test_none_zones_ok(self):
        TupleRepository.validate_cross_zone(None, None, None)

    def test_subject_zone_mismatch_raises(self):
        with pytest.raises(ValueError, match="subject zone"):
            TupleRepository.validate_cross_zone("z1", "z2", "z1")

    def test_object_zone_mismatch_raises(self):
        with pytest.raises(ValueError, match="object zone"):
            TupleRepository.validate_cross_zone("z1", "z1", "z2")

    def test_none_subject_zone_ok(self):
        """If subject_zone_id is None, no validation needed."""
        TupleRepository.validate_cross_zone("z1", None, "z1")

    def test_none_tuple_zone_ok(self):
        """If tuple zone_id is None, no validation needed."""
        TupleRepository.validate_cross_zone(None, "z2", "z3")


# ============================================================================
# Ancestor detection (would_create_cycle)
# ============================================================================


class TestAncestorDetection:
    """Tests for would_create_cycle.

    The CTE starts from ``subject`` and follows existing parent edges upward.
    It returns True if ``object_entity`` is already an ancestor of ``subject``,
    which would mean adding subject→parent→object_entity creates a redundant
    ancestor link or a cycle.
    """

    def test_no_ancestors_empty_graph(self, repo: TupleRepository):
        """No ancestors when graph is empty."""
        conn = repo.get_connection()
        try:
            result = repo.would_create_cycle(
                conn,
                subject=Entity("file", "a"),
                object_entity=Entity("file", "b"),
                zone_id=ZONE,
            )
        finally:
            repo.close_connection(conn)
        assert result is False

    def test_direct_ancestor_detected(self, repo: TupleRepository):
        """Adding B->parent->A when A->parent->B exists would create a cycle."""
        conn = repo.get_connection()
        try:
            # A's parent is B  (A -> parent -> B)
            _insert_tuple(
                conn,
                repo,
                subject=("file", "a"),
                relation="parent",
                obj=("file", "b"),
            )
            # Want to add: B -> parent -> A
            # CTE starts from object_entity=A, walks up ancestors, finds B
            # Then checks: is subject=B in ancestors? Yes → cycle
            result = repo.would_create_cycle(
                conn,
                subject=Entity("file", "b"),
                object_entity=Entity("file", "a"),
                zone_id=ZONE,
            )
        finally:
            repo.close_connection(conn)
        assert result is True

    def test_transitive_ancestor_detected(self, repo: TupleRepository):
        """Adding C->parent->A when A->parent->B->parent->C exists would cycle."""
        conn = repo.get_connection()
        try:
            # A -> parent -> B
            _insert_tuple(
                conn,
                repo,
                subject=("file", "a"),
                relation="parent",
                obj=("file", "b"),
            )
            # B -> parent -> C
            _insert_tuple(
                conn,
                repo,
                subject=("file", "b"),
                relation="parent",
                obj=("file", "c"),
            )
            # Want to add: C -> parent -> A
            # CTE starts from object_entity=A, walks A→B→C
            # Then checks: is subject=C in ancestors? Yes → cycle
            result = repo.would_create_cycle(
                conn,
                subject=Entity("file", "c"),
                object_entity=Entity("file", "a"),
                zone_id=ZONE,
            )
        finally:
            repo.close_connection(conn)
        assert result is True

    def test_no_ancestor_unrelated_nodes(self, repo: TupleRepository):
        """Unrelated nodes — object is NOT an ancestor of subject."""
        conn = repo.get_connection()
        try:
            # A's parent is B
            _insert_tuple(
                conn,
                repo,
                subject=("file", "a"),
                relation="parent",
                obj=("file", "b"),
            )
            # Check: is D an ancestor of C? (no — unrelated)
            result = repo.would_create_cycle(
                conn,
                subject=Entity("file", "c"),
                object_entity=Entity("file", "d"),
                zone_id=ZONE,
            )
        finally:
            repo.close_connection(conn)
        assert result is False

    def test_zone_scoped_ancestor_detection(self, repo: TupleRepository):
        """Ancestor detection respects zone isolation."""
        conn = repo.get_connection()
        try:
            # A→parent→B in zone1
            _insert_tuple(
                conn,
                repo,
                subject=("file", "a"),
                relation="parent",
                obj=("file", "b"),
                zone_id="zone1",
            )
            # In zone2, B should NOT be an ancestor of A
            result = repo.would_create_cycle(
                conn,
                subject=Entity("file", "a"),
                object_entity=Entity("file", "b"),
                zone_id="zone2",
            )
        finally:
            repo.close_connection(conn)
        assert result is False


# ============================================================================
# Tuple query methods
# ============================================================================


class TestFindSubjectSets:
    """Tests for find_subject_sets (userset-as-subject queries)."""

    def test_empty_when_no_tuples(self, repo: TupleRepository):
        result = repo.find_subject_sets("editor-of", Entity("file", "readme"), zone_id=ZONE)
        assert result == []

    def test_finds_subject_sets(self, repo: TupleRepository):
        """Finds tuples with subject_relation set."""
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("group", "eng"),
                relation="editor-of",
                obj=("file", "readme"),
                subject_relation="member",
            )
        finally:
            repo.close_connection(conn)
        result = repo.find_subject_sets("editor-of", Entity("file", "readme"), zone_id=ZONE)
        assert len(result) == 1
        assert result[0] == ("group", "eng", "member")

    def test_excludes_concrete_subjects(self, repo: TupleRepository):
        """Only returns tuples where subject_relation IS NOT NULL."""
        conn = repo.get_connection()
        try:
            # Concrete subject (no subject_relation)
            _insert_tuple(
                conn,
                repo,
                subject=("agent", "alice"),
                relation="editor-of",
                obj=("file", "readme"),
            )
            # Subject set (with subject_relation)
            _insert_tuple(
                conn,
                repo,
                subject=("group", "eng"),
                relation="editor-of",
                obj=("file", "readme"),
                subject_relation="member",
            )
        finally:
            repo.close_connection(conn)
        result = repo.find_subject_sets("editor-of", Entity("file", "readme"), zone_id=ZONE)
        assert len(result) == 1
        assert result[0][0] == "group"

    def test_zone_scoped_query(self, repo: TupleRepository):
        """Queries are scoped to a specific zone."""
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("group", "eng"),
                relation="editor-of",
                obj=("file", "readme"),
                subject_relation="member",
                zone_id="z1",
            )
            _insert_tuple(
                conn,
                repo,
                subject=("group", "ops"),
                relation="editor-of",
                obj=("file", "readme"),
                subject_relation="member",
                zone_id="z2",
            )
        finally:
            repo.close_connection(conn)
        result = repo.find_subject_sets("editor-of", Entity("file", "readme"), zone_id="z1")
        assert len(result) == 1
        assert result[0][1] == "eng"

    def test_excludes_expired_tuples(self, repo: TupleRepository):
        """Expired tuples should not appear in results."""
        # Use strftime format (space separator) to match SQLAlchemy's SQLite DateTime adapter.
        # isoformat() uses 'T' separator which breaks SQLite string comparison.
        past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("group", "eng"),
                relation="editor-of",
                obj=("file", "readme"),
                subject_relation="member",
                expires_at=past,
            )
        finally:
            repo.close_connection(conn)
        result = repo.find_subject_sets("editor-of", Entity("file", "readme"), zone_id=ZONE)
        assert len(result) == 0


class TestFindRelatedObjects:
    """Tests for find_related_objects (tupleToUserset traversal)."""

    def test_empty_when_no_tuples(self, repo: TupleRepository):
        result = repo.find_related_objects(Entity("file", "a"), "parent")
        assert result == []

    def test_finds_parents(self, repo: TupleRepository):
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("file", "readme"),
                relation="parent",
                obj=("folder", "docs"),
            )
        finally:
            repo.close_connection(conn)
        result = repo.find_related_objects(Entity("file", "readme"), "parent")
        assert len(result) == 1
        assert result[0].entity_type == "folder"
        assert result[0].entity_id == "docs"

    def test_multiple_relations(self, repo: TupleRepository):
        """Multiple parents are returned."""
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("file", "x"),
                relation="parent",
                obj=("folder", "a"),
            )
            _insert_tuple(
                conn,
                repo,
                subject=("file", "x"),
                relation="parent",
                obj=("folder", "b"),
            )
        finally:
            repo.close_connection(conn)
        result = repo.find_related_objects(Entity("file", "x"), "parent")
        assert len(result) == 2
        ids = {e.entity_id for e in result}
        assert ids == {"a", "b"}


class TestFindSubjectsWithRelation:
    """Tests for find_subjects_with_relation (reverse lookup)."""

    def test_empty_when_no_tuples(self, repo: TupleRepository):
        result = repo.find_subjects_with_relation(Entity("file", "readme"), "viewer-of")
        assert result == []

    def test_finds_viewers(self, repo: TupleRepository):
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("agent", "alice"),
                relation="viewer-of",
                obj=("file", "readme"),
            )
            _insert_tuple(
                conn,
                repo,
                subject=("agent", "bob"),
                relation="viewer-of",
                obj=("file", "readme"),
            )
        finally:
            repo.close_connection(conn)
        result = repo.find_subjects_with_relation(Entity("file", "readme"), "viewer-of")
        assert len(result) == 2
        ids = {e.entity_id for e in result}
        assert ids == {"alice", "bob"}


class TestGetDirectSubjects:
    """Tests for get_direct_subjects."""

    def test_empty(self, repo: TupleRepository):
        result = repo.get_direct_subjects("viewer-of", Entity("file", "x"))
        assert result == []

    def test_returns_subject_tuples(self, repo: TupleRepository):
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("agent", "alice"),
                relation="viewer-of",
                obj=("file", "readme"),
            )
        finally:
            repo.close_connection(conn)
        result = repo.get_direct_subjects("viewer-of", Entity("file", "readme"))
        assert len(result) == 1
        assert result[0] == ("agent", "alice")


# ============================================================================
# ABAC condition evaluation
# ============================================================================


class TestEvaluateConditions:
    """Tests for evaluate_conditions static method."""

    def test_no_conditions_returns_true(self):
        assert TupleRepository.evaluate_conditions(None, None) is True
        assert TupleRepository.evaluate_conditions({}, {}) is True

    def test_conditions_but_no_context_returns_false(self):
        assert TupleRepository.evaluate_conditions({"time_window": {}}, None) is False
        assert TupleRepository.evaluate_conditions({"time_window": {}}, {}) is False

    def test_time_window_within_range(self):
        conditions = {"time_window": {"start": "09:00:00", "end": "17:00:00"}}
        assert TupleRepository.evaluate_conditions(conditions, {"time": "12:00:00"}) is True

    def test_time_window_outside_range(self):
        conditions = {"time_window": {"start": "09:00:00", "end": "17:00:00"}}
        assert TupleRepository.evaluate_conditions(conditions, {"time": "20:00:00"}) is False

    def test_time_window_iso_format(self):
        """Handles ISO datetime strings (extracts time component)."""
        conditions = {"time_window": {"start": "09:00:00", "end": "17:00:00"}}
        assert (
            TupleRepository.evaluate_conditions(conditions, {"time": "2024-01-15T12:00:00+00:00"})
            is True
        )

    def test_ip_allowlist_match(self):
        conditions = {"allowed_ips": ["10.0.0.0/8", "192.168.1.0/24"]}
        assert TupleRepository.evaluate_conditions(conditions, {"ip": "10.1.2.3"}) is True

    def test_ip_allowlist_no_match(self):
        conditions = {"allowed_ips": ["10.0.0.0/8"]}
        assert TupleRepository.evaluate_conditions(conditions, {"ip": "172.16.0.1"}) is False

    def test_ip_allowlist_missing_ip(self):
        conditions = {"allowed_ips": ["10.0.0.0/8"]}
        assert TupleRepository.evaluate_conditions(conditions, {"other": "val"}) is False

    def test_device_allowlist(self):
        conditions = {"allowed_devices": ["desktop", "mobile"]}
        assert TupleRepository.evaluate_conditions(conditions, {"device": "desktop"}) is True
        assert TupleRepository.evaluate_conditions(conditions, {"device": "tablet"}) is False

    def test_custom_attributes(self):
        conditions = {"attributes": {"department": "engineering", "level": 3}}
        assert (
            TupleRepository.evaluate_conditions(
                conditions, {"department": "engineering", "level": 3}
            )
            is True
        )
        assert (
            TupleRepository.evaluate_conditions(conditions, {"department": "marketing", "level": 3})
            is False
        )

    def test_combined_conditions(self):
        """All conditions must pass (AND semantics)."""
        conditions = {
            "allowed_devices": ["desktop"],
            "attributes": {"clearance": "top-secret"},
        }
        assert (
            TupleRepository.evaluate_conditions(
                conditions, {"device": "desktop", "clearance": "top-secret"}
            )
            is True
        )
        assert (
            TupleRepository.evaluate_conditions(
                conditions, {"device": "mobile", "clearance": "top-secret"}
            )
            is False
        )


# ============================================================================
# Direct tuple lookup
# ============================================================================


class TestFindDirectTupleBySubject:
    """Tests for find_direct_tuple_by_subject."""

    def test_finds_direct_match(self, repo: TupleRepository):
        conn = repo.get_connection()
        try:
            tid = _insert_tuple(
                conn,
                repo,
                subject=("agent", "alice"),
                relation="viewer-of",
                obj=("file", "readme"),
            )
            cursor = repo.create_cursor(conn)
            row = repo.find_direct_tuple_by_subject(
                cursor,
                Entity("agent", "alice"),
                "viewer-of",
                Entity("file", "readme"),
                zone_id=ZONE,
            )
            assert row is not None
            assert row["tuple_id"] == tid
        finally:
            repo.close_connection(conn)

    def test_no_match_returns_none(self, repo: TupleRepository):
        conn = repo.get_connection()
        try:
            cursor = repo.create_cursor(conn)
            row = repo.find_direct_tuple_by_subject(
                cursor,
                Entity("agent", "bob"),
                "viewer-of",
                Entity("file", "readme"),
                zone_id=ZONE,
            )
            assert row is None
        finally:
            repo.close_connection(conn)

    def test_wildcard_fallback(self, repo: TupleRepository):
        """Falls back to wildcard (*:*) match when direct match not found."""
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("*", "*"),
                relation="viewer-of",
                obj=("file", "public-doc"),
            )
            cursor = repo.create_cursor(conn)
            row = repo.find_direct_tuple_by_subject(
                cursor,
                Entity("agent", "anyone"),
                "viewer-of",
                Entity("file", "public-doc"),
                zone_id=ZONE,
            )
            assert row is not None
            assert row["subject_type"] == "*"
            assert row["subject_id"] == "*"
        finally:
            repo.close_connection(conn)

    def test_zone_scoped(self, repo: TupleRepository):
        """Zone-scoped lookup finds tuples in the specified zone."""
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("agent", "alice"),
                relation="viewer-of",
                obj=("file", "readme"),
                zone_id="zone-a",
            )
            cursor = repo.create_cursor(conn)
            # Should find in zone-a
            row = repo.find_direct_tuple_by_subject(
                cursor,
                Entity("agent", "alice"),
                "viewer-of",
                Entity("file", "readme"),
                zone_id="zone-a",
            )
            assert row is not None

            # Should NOT find in zone-b
            row = repo.find_direct_tuple_by_subject(
                cursor,
                Entity("agent", "alice"),
                "viewer-of",
                Entity("file", "readme"),
                zone_id="zone-b",
            )
            assert row is None
        finally:
            repo.close_connection(conn)

    def test_expired_tuple_not_found(self, repo: TupleRepository):
        """Expired tuples should not be returned."""
        # Use strftime format (space separator) to match SQLAlchemy's SQLite DateTime adapter.
        # isoformat() uses 'T' separator which breaks SQLite string comparison.
        past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("agent", "alice"),
                relation="viewer-of",
                obj=("file", "readme"),
                expires_at=past,
            )
            cursor = repo.create_cursor(conn)
            row = repo.find_direct_tuple_by_subject(
                cursor,
                Entity("agent", "alice"),
                "viewer-of",
                Entity("file", "readme"),
                zone_id=ZONE,
            )
            assert row is None
        finally:
            repo.close_connection(conn)

    def test_conditions_returned_in_row(self, repo: TupleRepository):
        """Tuples with ABAC conditions include the conditions in the result."""
        import json

        conditions = json.dumps({"time_window": {"start": "09:00", "end": "17:00"}})
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("agent", "alice"),
                relation="viewer-of",
                obj=("file", "readme"),
                conditions=conditions,
            )
            cursor = repo.create_cursor(conn)
            row = repo.find_direct_tuple_by_subject(
                cursor,
                Entity("agent", "alice"),
                "viewer-of",
                Entity("file", "readme"),
                zone_id=ZONE,
            )
            assert row is not None
            assert row["conditions"] is not None
        finally:
            repo.close_connection(conn)

    def test_cross_zone_wildcard(self, repo: TupleRepository):
        """Cross-zone wildcard: *:* tuple in one zone matches via fallback."""
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("*", "*"),
                relation="viewer-of",
                obj=("file", "public"),
            )
            cursor = repo.create_cursor(conn)
            # Query from a different zone — should find via cross-zone wildcard fallback
            row = repo.find_direct_tuple_by_subject(
                cursor,
                Entity("agent", "anyone"),
                "viewer-of",
                Entity("file", "public"),
                zone_id="some-zone",
            )
            assert row is not None
        finally:
            repo.close_connection(conn)


# ============================================================================
# Bulk existence check
# ============================================================================


class TestBulkCheckTuplesExist:
    """Tests for bulk_check_tuples_exist."""

    def test_empty_input_returns_empty(self, repo: TupleRepository):
        conn = repo.get_connection()
        try:
            cursor = repo.create_cursor(conn)
            result = repo.bulk_check_tuples_exist(cursor, [])
            assert result == set()
        finally:
            repo.close_connection(conn)

    def test_finds_existing_tuples(self, repo: TupleRepository):
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("agent", "alice"),
                relation="viewer-of",
                obj=("file", "readme"),
            )
            cursor = repo.create_cursor(conn)
            parsed = [
                {
                    "subject_type": "agent",
                    "subject_id": "alice",
                    "subject_relation": None,
                    "relation": "viewer-of",
                    "object_type": "file",
                    "object_id": "readme",
                    "zone_id": ZONE,
                },
            ]
            result = repo.bulk_check_tuples_exist(cursor, parsed)
            assert len(result) == 1
        finally:
            repo.close_connection(conn)

    def test_missing_tuples_not_in_result(self, repo: TupleRepository):
        conn = repo.get_connection()
        try:
            cursor = repo.create_cursor(conn)
            parsed = [
                {
                    "subject_type": "agent",
                    "subject_id": "ghost",
                    "subject_relation": None,
                    "relation": "viewer-of",
                    "object_type": "file",
                    "object_id": "nothing",
                    "zone_id": ZONE,
                },
            ]
            result = repo.bulk_check_tuples_exist(cursor, parsed)
            assert len(result) == 0
        finally:
            repo.close_connection(conn)

    def test_mixed_existing_and_missing(self, repo: TupleRepository):
        conn = repo.get_connection()
        try:
            _insert_tuple(
                conn,
                repo,
                subject=("agent", "alice"),
                relation="viewer-of",
                obj=("file", "a"),
            )
            cursor = repo.create_cursor(conn)
            parsed = [
                {
                    "subject_type": "agent",
                    "subject_id": "alice",
                    "subject_relation": None,
                    "relation": "viewer-of",
                    "object_type": "file",
                    "object_id": "a",
                    "zone_id": ZONE,
                },
                {
                    "subject_type": "agent",
                    "subject_id": "bob",
                    "subject_relation": None,
                    "relation": "viewer-of",
                    "object_type": "file",
                    "object_id": "b",
                    "zone_id": ZONE,
                },
            ]
            result = repo.bulk_check_tuples_exist(cursor, parsed)
            assert len(result) == 1
        finally:
            repo.close_connection(conn)


# ============================================================================
# Read/write engine separation (Issue #725)
# ============================================================================


class TestReadWriteSeparation:
    """Tests for read_engine / readonly connection support (Issue #725)."""

    @pytest.fixture
    def read_engine(self):
        """Create a separate in-memory SQLite database for reads."""
        eng = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(eng)
        return eng

    @pytest.fixture
    def dual_repo(self, engine, read_engine, version_store):
        """Create a TupleRepository with separate write and read engines."""
        return TupleRepository(engine=engine, read_engine=read_engine, version_store=version_store)

    def test_fallback_to_primary_when_no_read_engine(self, engine):
        """When no read_engine is provided, read_engine defaults to engine."""
        repo = TupleRepository(engine=engine)
        assert repo.read_engine is repo.engine

    def test_read_engine_is_separate_when_provided(self, engine, read_engine):
        """When read_engine is provided, it is stored separately."""
        repo = TupleRepository(engine=engine, read_engine=read_engine)
        assert repo.read_engine is read_engine
        assert repo.read_engine is not repo.engine

    def test_connection_readonly_uses_read_engine(self, dual_repo):
        """connection(readonly=True) uses read_engine."""
        # Insert data into write engine
        conn = dual_repo.get_connection()
        try:
            _insert_tuple(
                conn,
                dual_repo,
                subject=("agent", "alice"),
                relation="viewer-of",
                obj=("file", "readme"),
            )
        finally:
            dual_repo.close_connection(conn)

        # Read engine is separate, so data inserted into write engine
        # should NOT be visible from read engine (separate in-memory DBs)
        with dual_repo.connection(readonly=True) as read_conn:
            cursor = dual_repo.create_cursor(read_conn)
            cursor.execute("SELECT COUNT(*) as cnt FROM rebac_tuples")
            row = cursor.fetchone()
            assert row["cnt"] == 0  # Not in read DB

    def test_connection_readwrite_uses_primary_engine(self, dual_repo):
        """connection(readonly=False) writes to primary, not read engine."""
        # Insert via the default (write) path using get_connection
        # (which uses the primary engine directly)
        conn = dual_repo.get_connection()
        try:
            _insert_tuple(
                conn,
                dual_repo,
                subject=("agent", "bob"),
                relation="editor-of",
                obj=("file", "doc"),
            )
        finally:
            dual_repo.close_connection(conn)

        # Data written to primary engine should NOT be visible from read engine
        # (they are separate in-memory databases in this test)
        with dual_repo.connection(readonly=True) as read_conn:
            cursor = dual_repo.create_cursor(read_conn)
            cursor.execute("SELECT COUNT(*) as cnt FROM rebac_tuples")
            row = cursor.fetchone()
            assert row["cnt"] == 0  # Not in read DB — proves write used primary

    def test_connection_readonly_skips_commit(self, dual_repo, read_engine):
        """connection(readonly=True) does not commit (read-only path)."""
        from sqlalchemy import text

        # Insert data into the read engine directly so we can read it
        with read_engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO rebac_tuples "
                    "(tuple_id, subject_type, subject_id, subject_relation, "
                    "relation, object_type, object_id, zone_id, subject_zone_id, "
                    "object_zone_id, created_at, expires_at, conditions) "
                    "VALUES (:tid, :st, :si, :sr, :r, :ot, :oi, :z, :sz, :oz, :ca, :ea, :c)"
                ),
                {
                    "tid": "ro-test",
                    "st": "agent",
                    "si": "bob",
                    "sr": None,
                    "r": "viewer-of",
                    "ot": "file",
                    "oi": "doc",
                    "z": ZONE,
                    "sz": ZONE,
                    "oz": ZONE,
                    "ca": datetime.now(UTC).isoformat(),
                    "ea": None,
                    "c": None,
                },
            )
            conn.commit()

        # Readonly connection should read successfully
        with dual_repo.connection(readonly=True) as read_conn:
            cursor = dual_repo.create_cursor(read_conn)
            cursor.execute("SELECT COUNT(*) as cnt FROM rebac_tuples")
            row = cursor.fetchone()
            assert row["cnt"] == 1

    def test_get_zone_revision_uses_version_store(self, dual_repo):
        """get_zone_revision() delegates to MetastoreVersionStore, not SQL engine."""
        # Seed version via increment (uses version_store, not SQL)
        conn = dual_repo.get_connection()
        try:
            for _ in range(42):
                dual_repo.increment_zone_revision("test-zone", conn)
        finally:
            dual_repo.close_connection(conn)

        rev = dual_repo.get_zone_revision("test-zone")
        assert rev == 42
