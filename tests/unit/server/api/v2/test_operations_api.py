"""Tests for Operations REST API (Event Replay + Agent Activity Summary).

Tests for issue #1197: Add Event Replay API (GET /api/v2/operations).
Tests for issue #1198: Add Agent Activity Summary endpoint.

Test categories:
1. OperationLogger extension tests (since, until, path_pattern, count)
2. Router integration tests (offset/cursor pagination, filters, auth, zone scoping)
3. Agent Activity Summary tests (aggregation, zone scoping, since filter, edge cases)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.server.api.v2.routers.operations import router
from nexus.storage.models import Base, OperationLogModel
from nexus.storage.operation_logger import OperationLogger

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database with OperationLogModel table.

    Uses StaticPool + check_same_thread=False so the same connection
    can be shared across the test thread and TestClient's worker thread.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def op_logger(db_session: Session) -> OperationLogger:
    """Create an OperationLogger backed by in-memory SQLite."""
    return OperationLogger(session=db_session)


@pytest.fixture
def seed_operations(op_logger: OperationLogger, db_session: Session) -> list[str]:
    """Seed 5 operations with varying attributes.

    Returns list of operation_ids in insertion order.
    """
    base_time = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    ops = [
        ("write", "/docs/readme.md", "zone-a", "agent-1", base_time),
        ("delete", "/docs/old.txt", "zone-a", "agent-2", base_time + timedelta(hours=1)),
        ("rename", "/src/main.py", "zone-a", "agent-1", base_time + timedelta(hours=2)),
        ("write", "/src/utils.py", "zone-b", "agent-1", base_time + timedelta(hours=3)),
        ("mkdir", "/src/lib/", "zone-a", "agent-3", base_time + timedelta(hours=4)),
    ]
    ids = []
    for op_type, path, zone_id, agent_id, created_at in ops:
        op = OperationLogModel(
            operation_type=op_type,
            path=path,
            zone_id=zone_id,
            agent_id=agent_id,
            status="success",
            created_at=created_at,
        )
        db_session.add(op)
        db_session.flush()
        ids.append(op.operation_id)
    db_session.commit()
    return ids


# =============================================================================
# OperationLogger Unit Tests
# =============================================================================


class TestOperationLoggerFilters:
    """Tests for OperationLogger since/until/path_pattern extensions."""

    def test_list_operations_since_filter(
        self, op_logger: OperationLogger, seed_operations: list[str]
    ):
        """Only operations created_at >= since are returned."""
        since = datetime(2026, 1, 15, 14, 0, 0, tzinfo=UTC)  # 2 hours after base
        results = op_logger.list_operations(zone_id="zone-a", since=since)
        # Should get ops at +2h and +4h (zone-a only, skips +3h which is zone-b)
        assert len(results) == 2
        paths = {r.path for r in results}
        assert "/src/main.py" in paths
        assert "/src/lib/" in paths

    def test_list_operations_until_filter(
        self, op_logger: OperationLogger, seed_operations: list[str]
    ):
        """Only operations created_at <= until are returned."""
        until = datetime(2026, 1, 15, 13, 0, 0, tzinfo=UTC)  # 1 hour after base
        results = op_logger.list_operations(zone_id="zone-a", until=until)
        # Should get ops at base and +1h
        assert len(results) == 2
        paths = {r.path for r in results}
        assert "/docs/readme.md" in paths
        assert "/docs/old.txt" in paths

    def test_list_operations_since_and_until(
        self, op_logger: OperationLogger, seed_operations: list[str]
    ):
        """Time range filters combine correctly."""
        since = datetime(2026, 1, 15, 12, 30, 0, tzinfo=UTC)
        until = datetime(2026, 1, 15, 14, 30, 0, tzinfo=UTC)
        results = op_logger.list_operations(zone_id="zone-a", since=since, until=until)
        # Should get ops at +1h and +2h
        assert len(results) == 2

    def test_list_operations_path_pattern_wildcard(
        self, op_logger: OperationLogger, seed_operations: list[str]
    ):
        """Wildcard * in path_pattern matches via SQL LIKE."""
        results = op_logger.list_operations(zone_id="zone-a", path_pattern="/docs/*")
        assert len(results) == 2
        assert all(r.path.startswith("/docs/") for r in results)

    def test_list_operations_path_pattern_exact(
        self, op_logger: OperationLogger, seed_operations: list[str]
    ):
        """Path pattern without wildcards matches exact path."""
        results = op_logger.list_operations(zone_id="zone-a", path_pattern="/src/main.py")
        assert len(results) == 1
        assert results[0].path == "/src/main.py"

    def test_path_pattern_sql_injection_safe(
        self, op_logger: OperationLogger, seed_operations: list[str]
    ):
        """SQL LIKE special chars % and _ in user input are escaped."""
        # % and _ should be treated as literals, not wildcards
        results = op_logger.list_operations(zone_id="zone-a", path_pattern="/docs/100%_done")
        assert len(results) == 0  # No match (literal % and _ in pattern)

    def test_count_operations_with_filters(
        self, op_logger: OperationLogger, seed_operations: list[str]
    ):
        """count_operations returns correct count with filters."""
        count = op_logger.count_operations(zone_id="zone-a", operation_type="write")
        assert count == 1  # Only /docs/readme.md in zone-a

    def test_count_operations_all_in_zone(
        self, op_logger: OperationLogger, seed_operations: list[str]
    ):
        """count_operations counts all operations in a zone."""
        count = op_logger.count_operations(zone_id="zone-a")
        assert count == 4  # 4 ops in zone-a

    def test_count_operations_empty(self, op_logger: OperationLogger, seed_operations: list[str]):
        """count_operations returns 0 when no operations match."""
        count = op_logger.count_operations(zone_id="nonexistent-zone")
        assert count == 0

    def test_cursor_with_since_until(self, op_logger: OperationLogger, seed_operations: list[str]):
        """Cursor pagination works with time range filters."""
        since = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        until = datetime(2026, 1, 15, 16, 30, 0, tzinfo=UTC)
        results, next_cursor = op_logger.list_operations_cursor(
            zone_id="zone-a", since=since, until=until, limit=2
        )
        assert len(results) == 2
        assert next_cursor is not None

        # Get next page
        results2, next_cursor2 = op_logger.list_operations_cursor(
            zone_id="zone-a", since=since, until=until, limit=2, cursor=next_cursor
        )
        assert len(results2) == 2
        # All 4 results should be unique
        all_ids = {r.operation_id for r in results} | {r.operation_id for r in results2}
        assert len(all_ids) == 4


# =============================================================================
# Router Integration Tests
# =============================================================================


def _create_test_app(
    db_session: Session, zone_id: str = "zone-a", *, require_auth: bool = True
) -> FastAPI:
    """Create a FastAPI test app with the operations router."""
    app = FastAPI()
    app.include_router(router)

    from nexus.server.api.v2.dependencies import get_operation_logger

    if require_auth:
        logger_instance = OperationLogger(session=db_session)

        async def _mock_get_operation_logger():
            return logger_instance, zone_id

        app.dependency_overrides[get_operation_logger] = _mock_get_operation_logger
    else:
        # Simulate auth failure
        async def _mock_get_operation_logger_unauthed():
            raise HTTPException(status_code=401, detail="Authentication required")

        app.dependency_overrides[get_operation_logger] = _mock_get_operation_logger_unauthed

    return app


class TestOperationsRouter:
    """Integration tests for GET /api/v2/operations."""

    def test_get_operations_offset_mode(self, db_session: Session, seed_operations: list[str]):
        """Offset mode returns operations with has_more (no COUNT by default)."""
        app = _create_test_app(db_session)
        client = TestClient(app)
        resp = client.get("/api/v2/operations?limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["offset"] == 0
        assert data["limit"] == 2
        assert data["has_more"] is True  # 4 ops in zone-a, limit=2
        assert data["total"] is None  # no COUNT by default
        assert len(data["operations"]) == 2

    def test_get_operations_include_total(self, db_session: Session, seed_operations: list[str]):
        """include_total=true adds exact COUNT."""
        app = _create_test_app(db_session)
        client = TestClient(app)
        resp = client.get("/api/v2/operations?limit=2&include_total=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 4  # exact count
        assert data["has_more"] is True
        assert len(data["operations"]) == 2

    def test_get_operations_has_more_false(self, db_session: Session, seed_operations: list[str]):
        """has_more is false when all results fit in one page."""
        app = _create_test_app(db_session)
        client = TestClient(app)
        resp = client.get("/api/v2/operations?limit=100")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_more"] is False
        assert len(data["operations"]) == 4

    def test_get_operations_cursor_mode(self, db_session: Session, seed_operations: list[str]):
        """Cursor mode returns operations and next_cursor."""
        app = _create_test_app(db_session)
        client = TestClient(app)

        # Get first page in offset mode to find an operation_id
        resp1 = client.get("/api/v2/operations?limit=2")
        data1 = resp1.json()
        assert len(data1["operations"]) == 2
        last_id = data1["operations"][-1]["id"]

        # Now use cursor mode
        resp2 = client.get(f"/api/v2/operations?cursor={last_id}&limit=2")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert "next_cursor" in data2
        assert data2["has_more"] is True or data2["has_more"] is False
        assert data2["total"] is None  # cursor mode never includes total
        assert data2["offset"] is None  # cursor mode doesn't include offset
        assert len(data2["operations"]) == 2

    def test_get_operations_filters(self, db_session: Session, seed_operations: list[str]):
        """Filters by agent_id, operation_type, path_pattern."""
        app = _create_test_app(db_session)
        client = TestClient(app)

        # Filter by agent_id (agent-1 in zone-a: write /docs/readme.md, rename /src/main.py)
        resp = client.get("/api/v2/operations?agent_id=agent-1&include_total=true")
        data = resp.json()
        assert data["total"] == 2
        assert all(op["agent_id"] == "agent-1" for op in data["operations"])

        # Filter by operation_type
        resp = client.get("/api/v2/operations?operation_type=write&include_total=true")
        data = resp.json()
        assert data["total"] == 1  # zone-a write: only /docs/readme.md

        # Filter by path_pattern
        resp = client.get("/api/v2/operations?path_pattern=/src/*&include_total=true")
        data = resp.json()
        assert data["total"] == 2  # /src/main.py and /src/lib/ in zone-a

    def test_get_operations_time_range(self, db_session: Session, seed_operations: list[str]):
        """since/until filter operations by time."""
        app = _create_test_app(db_session)
        client = TestClient(app)
        since = "2026-01-15T13:00:00Z"
        until = "2026-01-15T15:00:00Z"
        resp = client.get(f"/api/v2/operations?since={since}&until={until}&include_total=true")
        assert resp.status_code == 200
        data = resp.json()
        # +1h (delete /docs/old.txt) and +2h (rename /src/main.py) in zone-a
        assert data["total"] == 2

    def test_get_operations_auth_required(self, db_session: Session):
        """Returns 401 without authentication."""
        app = _create_test_app(db_session, require_auth=False)
        client = TestClient(app)
        resp = client.get("/api/v2/operations")
        assert resp.status_code == 401

    def test_get_operations_zone_scoping(self, db_session: Session, seed_operations: list[str]):
        """Only returns operations for the authenticated user's zone."""
        # zone-b has 1 operation (write /src/utils.py by agent-1)
        app = _create_test_app(db_session, zone_id="zone-b")
        client = TestClient(app)
        resp = client.get("/api/v2/operations?include_total=true")
        data = resp.json()
        assert data["total"] == 1
        assert data["operations"][0]["path"] == "/src/utils.py"

    def test_get_operations_limit_capped(self, db_session: Session, seed_operations: list[str]):
        """Limit is capped at 1000 by FastAPI validation."""
        app = _create_test_app(db_session)
        client = TestClient(app)
        resp = client.get("/api/v2/operations?limit=2000")
        assert resp.status_code == 422  # Validation error

    def test_get_operations_empty(self, db_session: Session):
        """No results returns empty array with has_more=false."""
        app = _create_test_app(db_session, zone_id="empty-zone")
        client = TestClient(app)
        resp = client.get("/api/v2/operations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["operations"] == []
        assert data["has_more"] is False
        assert data["total"] is None  # no COUNT by default

    def test_get_operations_empty_with_total(self, db_session: Session):
        """include_total=true with no results returns total=0."""
        app = _create_test_app(db_session, zone_id="empty-zone")
        client = TestClient(app)
        resp = client.get("/api/v2/operations?include_total=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["operations"] == []
        assert data["total"] == 0
        assert data["has_more"] is False

    def test_operation_response_format(self, db_session: Session, seed_operations: list[str]):
        """Each operation in the response has the expected fields."""
        app = _create_test_app(db_session)
        client = TestClient(app)
        resp = client.get("/api/v2/operations?limit=1")
        data = resp.json()
        op = data["operations"][0]
        assert "id" in op
        assert "operation_type" in op
        assert "path" in op
        assert "status" in op
        assert "timestamp" in op
        assert "agent_id" in op
        assert "has_more" in data


# =============================================================================
# Agent Activity Summary Fixtures (Issue #1198)
# =============================================================================


@pytest.fixture
def seed_activity_ops(db_session: Session) -> dict[str, Any]:
    """Seed operations for agent activity summary tests.

    Creates a mix of agents, zones, statuses, and timestamps
    for comprehensive summary testing.

    agent-1 in zone-a (within 24h): 4 ops
      - write /docs/readme.md (success, -6h)
      - write /docs/readme.md (success, -4h)  [duplicate path]
      - rename /src/main.py  (success, -2h)
      - delete /tmp/old.txt  (failure, -1h)

    agent-1 in zone-a (>24h old): 1 op
      - write /archive/old.txt (success, -48h)

    agent-1 in zone-b: 1 op
      - write /other/file.py (success, -3h)

    agent-2 in zone-a: 1 op
      - mkdir /src/lib/ (success, -5h)
    """
    now = datetime.now(UTC)
    ops = [
        # agent-1, zone-a: recent (within 24h)
        ("write", "/docs/readme.md", "zone-a", "agent-1", "success", now - timedelta(hours=6)),
        ("write", "/docs/readme.md", "zone-a", "agent-1", "success", now - timedelta(hours=4)),
        ("rename", "/src/main.py", "zone-a", "agent-1", "success", now - timedelta(hours=2)),
        ("delete", "/tmp/old.txt", "zone-a", "agent-1", "failure", now - timedelta(hours=1)),
        # agent-1, zone-a: old (>24h ago)
        ("write", "/archive/old.txt", "zone-a", "agent-1", "success", now - timedelta(hours=48)),
        # agent-1, zone-b: different zone
        ("write", "/other/file.py", "zone-b", "agent-1", "success", now - timedelta(hours=3)),
        # agent-2, zone-a: different agent
        ("mkdir", "/src/lib/", "zone-a", "agent-2", "success", now - timedelta(hours=5)),
    ]
    for op_type, path, zone_id, agent_id, status, created_at in ops:
        db_session.add(
            OperationLogModel(
                operation_type=op_type,
                path=path,
                zone_id=zone_id,
                agent_id=agent_id,
                status=status,
                created_at=created_at,
            )
        )
    db_session.commit()
    return {"now": now}


# =============================================================================
# Agent Activity Summary Tests (Issue #1198)
# =============================================================================


class TestAgentActivitySummary:
    """Tests for GET /api/v2/operations/agents/{agent_id}/activity."""

    def test_activity_summary_happy_path(
        self, db_session: Session, seed_activity_ops: dict[str, Any]
    ):
        """Returns correct summary with mixed operation types for agent-1 in zone-a.

        Default since=24h, so the 48h-old operation is excluded.
        Expected: 4 ops (2 write, 1 rename, 1 delete).
        """
        app = _create_test_app(db_session)
        client = TestClient(app)
        resp = client.get("/api/v2/operations/agents/agent-1/activity")
        assert resp.status_code == 200
        data = resp.json()

        assert data["agent_id"] == "agent-1"
        assert data["total_operations"] == 4
        assert data["operations_by_type"] == {"write": 2, "rename": 1, "delete": 1}
        assert isinstance(data["recent_paths"], list)
        assert len(data["recent_paths"]) == 3  # 3 unique paths
        assert data["last_active"] is not None
        assert data["first_seen"] is not None

    def test_activity_summary_empty_agent(
        self, db_session: Session, seed_activity_ops: dict[str, Any]
    ):
        """Agent with no operations returns zero counts and null timestamps."""
        app = _create_test_app(db_session)
        client = TestClient(app)
        resp = client.get("/api/v2/operations/agents/nonexistent-agent/activity")
        assert resp.status_code == 200
        data = resp.json()

        assert data["agent_id"] == "nonexistent-agent"
        assert data["total_operations"] == 0
        assert data["operations_by_type"] == {}
        assert data["recent_paths"] == []
        assert data["last_active"] is None
        assert data["first_seen"] is None

    def test_activity_summary_zone_scoped(
        self, db_session: Session, seed_activity_ops: dict[str, Any]
    ):
        """Only counts operations in the authenticated zone.

        agent-1 has ops in both zone-a (4 within 24h) and zone-b (1).
        Authenticating as zone-b should show only the zone-b operation.
        """
        app = _create_test_app(db_session, zone_id="zone-b")
        client = TestClient(app)
        resp = client.get("/api/v2/operations/agents/agent-1/activity")
        assert resp.status_code == 200
        data = resp.json()

        assert data["total_operations"] == 1
        assert data["operations_by_type"] == {"write": 1}
        assert data["recent_paths"] == ["/other/file.py"]

    def test_activity_summary_since_filter(
        self, db_session: Session, seed_activity_ops: dict[str, Any]
    ):
        """Explicit since parameter restricts the time window.

        since=3h ago should only include ops at -2h (rename) and -1h (delete).
        """
        now = seed_activity_ops["now"]
        # Use Z suffix (not +00:00) to avoid URL-encoding issues with '+' in query params
        since = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        app = _create_test_app(db_session)
        client = TestClient(app)
        resp = client.get(f"/api/v2/operations/agents/agent-1/activity?since={since}")
        assert resp.status_code == 200
        data = resp.json()

        assert data["total_operations"] == 2
        assert data["operations_by_type"] == {"rename": 1, "delete": 1}
        assert set(data["recent_paths"]) == {"/tmp/old.txt", "/src/main.py"}

    def test_activity_summary_default_since_excludes_old(
        self, db_session: Session, seed_activity_ops: dict[str, Any]
    ):
        """Without since param, defaults to last 24h, excluding older operations.

        agent-1 in zone-a has 5 total ops but only 4 within 24h.
        The 48h-old write to /archive/old.txt should be excluded.
        """
        app = _create_test_app(db_session)
        client = TestClient(app)
        resp = client.get("/api/v2/operations/agents/agent-1/activity")
        assert resp.status_code == 200
        data = resp.json()

        assert data["total_operations"] == 4
        # /archive/old.txt should NOT appear in recent_paths
        assert "/archive/old.txt" not in data["recent_paths"]

    def test_activity_summary_includes_failures(
        self, db_session: Session, seed_activity_ops: dict[str, Any]
    ):
        """Both success and failure operations are counted in the summary.

        agent-1 has 3 success + 1 failure in zone-a within 24h.
        All 4 should be counted.
        """
        app = _create_test_app(db_session)
        client = TestClient(app)
        resp = client.get("/api/v2/operations/agents/agent-1/activity")
        assert resp.status_code == 200
        data = resp.json()

        # delete /tmp/old.txt is a failure — should still be counted
        assert data["total_operations"] == 4
        assert "delete" in data["operations_by_type"]
        assert data["operations_by_type"]["delete"] == 1

    def test_activity_summary_recent_paths_dedup_and_order(
        self, db_session: Session, seed_activity_ops: dict[str, Any]
    ):
        """Duplicate paths appear once, ordered by most recent touch.

        agent-1 wrote /docs/readme.md at -6h and -4h.
        recent_paths should list it once, ordered by last touch time:
        /tmp/old.txt (-1h), /src/main.py (-2h), /docs/readme.md (-4h).
        """
        app = _create_test_app(db_session)
        client = TestClient(app)
        resp = client.get("/api/v2/operations/agents/agent-1/activity")
        assert resp.status_code == 200
        data = resp.json()

        paths = data["recent_paths"]
        assert len(paths) == 3
        # Most recently touched first
        assert paths[0] == "/tmp/old.txt"
        assert paths[1] == "/src/main.py"
        assert paths[2] == "/docs/readme.md"

    def test_activity_summary_auth_required(self, db_session: Session):
        """Returns 401 without authentication."""
        app = _create_test_app(db_session, require_auth=False)
        client = TestClient(app)
        resp = client.get("/api/v2/operations/agents/agent-1/activity")
        assert resp.status_code == 401
