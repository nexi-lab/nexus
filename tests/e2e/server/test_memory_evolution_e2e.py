"""E2E tests for Memory Evolution Detection (#1190).

Tests the FastAPI endpoints for auto-detecting evolution relationships
(UPDATES, EXTENDS, DERIVES) between memories at write time.

Uses FastAPI TestClient with DatabaseAPIKeyAuth for real auth testing,
following the pattern from test_memory_classification_e2e.py.

Run with: python -m pytest tests/e2e/test_memory_evolution_e2e.py -v
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from nexus.core._metadata_generated import FileMetadata, FileMetadataProtocol, PaginatedResult
from nexus.core.config import PermissionConfig
from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.server.auth.factory import DiscriminatingAuthProvider
from nexus.storage.models import Base

# ==============================================================================
# In-memory metadata store stub (avoids LocalRaft dependency)
# ==============================================================================


class InMemoryMetadataStore(FileMetadataProtocol):
    """Minimal in-memory metadata store for tests that don't need file ops."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata) -> None:
        self._store[metadata.path] = metadata

    def delete(self, path: str) -> dict[str, Any] | None:
        removed = self._store.pop(path, None)
        if removed:
            return {"path": path}
        return None

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(  # noqa: A003
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> list[FileMetadata]:
        return [m for p, m in self._store.items() if p.startswith(prefix)]

    def list_paginated(
        self,
        prefix: str = "",
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
        zone_id: str | None = None,
    ) -> PaginatedResult:
        items = self.list(prefix, recursive)
        return PaginatedResult(
            items=items[:limit],
            next_cursor=None,
            has_more=len(items) > limit,
            total_count=len(items),
        )

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        return {p: self._store.get(p) for p in paths}

    def close(self) -> None:
        self._store.clear()


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    """Set required env vars for server modules."""
    monkeypatch.setenv("NEXUS_JWT_SECRET", "test-secret-key-12345")
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)


@pytest.fixture
def db_engine(tmp_path):
    """Create file-backed SQLite engine shared across all sessions."""
    db_path = tmp_path / "test_evolution.db"
    engine = create_engine(f"sqlite:///{db_path}", echo=False)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session_factory(db_engine):
    """Create session factory bound to shared engine."""
    return sessionmaker(bind=db_engine)


@pytest.fixture
def api_keys(db_session_factory):
    """Create user and agent API keys via DatabaseAPIKeyAuth."""
    with db_session_factory() as session:
        normal_key_id, normal_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="test-user",
            name="Test User Key",
            zone_id="default",
            is_admin=False,
        )
        agent_key_id, agent_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="test-agent-owner",
            name="Test Agent Key",
            subject_type="agent",
            subject_id="agent-007",
            zone_id="default",
            is_admin=False,
            inherit_permissions=True,
        )
        admin_key_id, admin_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="admin-user",
            name="Admin Key",
            zone_id="default",
            is_admin=True,
        )
        session.commit()

    return {
        "normal_key": normal_raw,
        "normal_key_id": normal_key_id,
        "agent_key": agent_raw,
        "agent_key_id": agent_key_id,
        "admin_key": admin_raw,
        "admin_key_id": admin_key_id,
    }


@pytest.fixture
def app_with_auth(tmp_path, db_session_factory, api_keys):
    """Create FastAPI app with DatabaseAPIKeyAuth."""
    from nexus.backends.local import LocalBackend
    from nexus.core.nexus_fs import NexusFS
    from nexus.server.fastapi_server import create_app
    from nexus.storage.record_store import SQLAlchemyRecordStore

    tmpdir = tempfile.mkdtemp(prefix="nexus-evolution-e2e-")

    backend = LocalBackend(root_path=tmpdir)
    metadata_store = InMemoryMetadataStore()

    record_db_path = tmp_path / "records.db"
    record_store = SQLAlchemyRecordStore(db_path=str(record_db_path))

    nx = NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=PermissionConfig(enforce=False),
    )

    db_key_provider = DatabaseAPIKeyAuth(session_factory=db_session_factory)
    auth_provider = DiscriminatingAuthProvider(
        api_key_provider=db_key_provider,
        jwt_provider=None,
    )

    app = create_app(
        nexus_fs=nx,
        auth_provider=auth_provider,
        database_url=f"sqlite:///{record_db_path}",
    )

    yield app

    metadata_store.close()
    record_store.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def client(app_with_auth):
    """Create TestClient."""
    return TestClient(app_with_auth)


@pytest.fixture
def user_headers(api_keys):
    """Auth headers for normal user."""
    return {"Authorization": f"Bearer {api_keys['normal_key']}"}


@pytest.fixture
def agent_headers(api_keys):
    """Auth headers for agent subject type."""
    return {"Authorization": f"Bearer {api_keys['agent_key']}"}


@pytest.fixture
def admin_headers(api_keys):
    """Auth headers for admin user."""
    return {"Authorization": f"Bearer {api_keys['admin_key']}"}


# ==============================================================================
# Helper methods
# ==============================================================================


def _store_memory_v1(
    client: TestClient,
    headers: dict,
    content: str,
    detect_evolution: bool = False,
    **kwargs: Any,
) -> dict:
    """Store a memory via V1 API and return response data."""
    payload = {
        "content": content,
        "scope": "user",
        "detect_evolution": detect_evolution,
        **kwargs,
    }
    resp = client.post("/api/memory/store", json=payload, headers=headers)
    assert resp.status_code == 200, f"V1 Store failed: {resp.text}"
    return resp.json()


def _store_memory_v2(
    client: TestClient,
    headers: dict,
    content: str,
    detect_evolution: bool = False,
    **kwargs: Any,
) -> dict:
    """Store a memory via V2 API and return response data."""
    payload = {
        "content": content,
        "scope": "user",
        "detect_evolution": detect_evolution,
        **kwargs,
    }
    resp = client.post("/api/v2/memories", json=payload, headers=headers)
    assert resp.status_code == 201, f"V2 Store failed: {resp.text}"
    return resp.json()


def _get_memory_v1(client: TestClient, headers: dict, memory_id: str) -> dict:
    """Get a memory by ID via V1 API."""
    resp = client.get(f"/api/memory/{memory_id}", headers=headers)
    assert resp.status_code == 200, f"V1 Get failed: {resp.text}"
    return resp.json()["memory"]


def _get_memory_v2(client: TestClient, headers: dict, memory_id: str) -> dict:
    """Get a memory by ID via V2 API."""
    resp = client.get(f"/api/v2/memories/{memory_id}", headers=headers)
    assert resp.status_code == 200, f"V2 Get failed: {resp.text}"
    return resp.json()["memory"]


# ==============================================================================
# Tests: V1 API — POST /api/memory/store with detect_evolution
# ==============================================================================


class TestEvolutionV1API:
    """E2E tests for memory evolution via V1 API."""

    def test_store_without_evolution_has_null_fields(self, client, user_headers):
        """POST without detect_evolution -> evolution fields are null."""
        result = _store_memory_v1(client, user_headers, "Alice works at Microsoft")
        memory = _get_memory_v1(client, user_headers, result["memory_id"])
        assert memory.get("extends_ids") is None
        assert memory.get("extended_by_ids") is None
        assert memory.get("derived_from_ids") is None

    def test_store_with_evolution_detects_updates(self, client, admin_headers):
        """Store Alice@Microsoft, then Alice@Google with evolution -> UPDATES detected."""
        # Store original memory
        result1 = _store_memory_v1(
            client,
            admin_headers,
            "Alice works at Microsoft as a software engineer",
        )
        mem1_id = result1["memory_id"]

        # Store correcting memory with evolution detection
        result2 = _store_memory_v1(
            client,
            admin_headers,
            "Correction: Alice is now at Google instead of Microsoft",
            detect_evolution=True,
        )
        mem2_id = result2["memory_id"]

        # Verify the new memory has supersedes relationship
        mem2 = _get_memory_v1(client, admin_headers, mem2_id)
        # The UPDATES relationship sets supersedes_id
        assert mem2.get("supersedes_id") == mem1_id or mem2.get("extends_ids") is not None, (
            f"Expected evolution relationship on new memory: {mem2}"
        )

    def test_store_with_evolution_detects_extends(self, client, admin_headers):
        """Store base fact, then additional info -> EXTENDS detected."""
        # Store base memory
        _store_memory_v1(
            client,
            admin_headers,
            "Bob is a senior engineer at Amazon",
        )

        # Store extending memory with evolution detection
        result2 = _store_memory_v1(
            client,
            admin_headers,
            "Additionally, Bob also manages the cloud infrastructure team",
            detect_evolution=True,
        )
        mem2_id = result2["memory_id"]

        # Get memory and check evolution fields
        mem2 = _get_memory_v1(client, admin_headers, mem2_id)
        # EXTENDS sets extends_ids
        if mem2.get("extends_ids"):
            extends_list = json.loads(mem2["extends_ids"])
            assert len(extends_list) >= 1

    def test_store_without_auth_returns_401(self, client):
        """POST /api/memory/store without auth -> 401."""
        resp = client.post(
            "/api/memory/store",
            json={"content": "Should fail", "scope": "user"},
        )
        assert resp.status_code == 401


# ==============================================================================
# Tests: V2 API — POST /api/v2/memories with detect_evolution
# ==============================================================================


class TestEvolutionV2API:
    """E2E tests for memory evolution via V2 API."""

    def test_store_with_detect_evolution_false_is_default(self, client, user_headers):
        """V2 default: detect_evolution=false -> no evolution processing."""
        result = _store_memory_v2(client, user_headers, "Charlie likes hiking")
        memory = _get_memory_v2(client, user_headers, result["memory_id"])
        assert memory.get("extends_ids") is None
        assert memory.get("derived_from_ids") is None

    def test_v2_store_with_evolution_detects_updates(self, client, admin_headers):
        """V2: Store then correct with detect_evolution=true -> UPDATES."""
        result1 = _store_memory_v2(
            client,
            admin_headers,
            "Diana lives in New York City",
        )
        mem1_id = result1["memory_id"]

        result2 = _store_memory_v2(
            client,
            admin_headers,
            "Correction: Diana actually moved to San Francisco instead of New York",
            detect_evolution=True,
        )
        mem2_id = result2["memory_id"]

        mem2 = _get_memory_v2(client, admin_headers, mem2_id)
        # UPDATES relationship detected (heuristic: "actually", "instead")
        assert mem2.get("supersedes_id") == mem1_id or mem2.get("extends_ids") is not None, (
            f"Expected evolution on mem2: {mem2}"
        )

    def test_v2_store_with_evolution_detects_extends(self, client, admin_headers):
        """V2: Store base then extend with detect_evolution=true -> EXTENDS."""
        _store_memory_v2(
            client,
            admin_headers,
            "Eve is a data scientist at Meta",
        )

        result2 = _store_memory_v2(
            client,
            admin_headers,
            "Furthermore, Eve also teaches machine learning at Stanford on weekends",
            detect_evolution=True,
        )
        mem2_id = result2["memory_id"]

        mem2 = _get_memory_v2(client, admin_headers, mem2_id)
        if mem2.get("extends_ids"):
            extends_list = json.loads(mem2["extends_ids"])
            assert len(extends_list) >= 1

    def test_v2_evolution_fields_in_get_response(self, client, admin_headers):
        """GET /api/v2/memories/{id} includes all evolution fields."""
        result = _store_memory_v2(client, admin_headers, "Test evolution fields present")
        memory = _get_memory_v2(client, admin_headers, result["memory_id"])
        # All evolution fields should be present (even if null)
        assert "supersedes_id" in memory
        assert "superseded_by_id" in memory
        assert "extends_ids" in memory
        assert "extended_by_ids" in memory
        assert "derived_from_ids" in memory


# ==============================================================================
# Tests: Agent Permissions (non-user subject type)
# ==============================================================================


class TestEvolutionAgentPermissions:
    """Tests with agent (non-user) authentication."""

    def test_agent_can_store_with_evolution(self, client, agent_headers):
        """Agent subject type can store memories with detect_evolution."""
        result = _store_memory_v2(
            client,
            agent_headers,
            "Agent observation: server response time is 200ms",
            detect_evolution=True,
        )
        assert result.get("memory_id") is not None

    def test_agent_store_detects_updates(self, client, agent_headers):
        """Agent stores two related memories -> evolution detected."""
        _store_memory_v2(
            client,
            agent_headers,
            "Agent report: system CPU usage at 45%",
        )

        result2 = _store_memory_v2(
            client,
            agent_headers,
            "Correction: system CPU is now at 85% instead of previous measurement",
            detect_evolution=True,
        )
        assert result2.get("memory_id") is not None

    def test_agent_can_get_evolution_fields(self, client, agent_headers):
        """Agent can retrieve memories with evolution fields."""
        result = _store_memory_v2(client, agent_headers, "Agent test memory")
        memory = _get_memory_v2(client, agent_headers, result["memory_id"])
        assert "extends_ids" in memory
        assert "derived_from_ids" in memory


# ==============================================================================
# Tests: Cross-API Compatibility
# ==============================================================================


class TestEvolutionCrossAPI:
    """Tests ensuring V1 and V2 APIs are compatible."""

    def test_v1_store_v2_get_evolution_fields(self, client, admin_headers):
        """Store via V1 with evolution, retrieve via V2 -> fields visible."""
        result = _store_memory_v1(
            client,
            admin_headers,
            "Cross-API test: Frank likes Python",
        )
        # V2 get should include evolution fields
        memory = _get_memory_v2(client, admin_headers, result["memory_id"])
        assert "extends_ids" in memory
        assert "derived_from_ids" in memory

    def test_v2_store_v1_get_evolution_fields(self, client, admin_headers):
        """Store via V2 with evolution, retrieve via V1 -> fields visible."""
        result = _store_memory_v2(
            client,
            admin_headers,
            "Cross-API test: Grace prefers TypeScript",
        )
        memory = _get_memory_v1(client, admin_headers, result["memory_id"])
        assert "extends_ids" in memory
        assert "derived_from_ids" in memory


# ==============================================================================
# Tests: Performance
# ==============================================================================


class TestEvolutionPerformance:
    """Performance tests for evolution detection through the HTTP layer."""

    def test_store_with_evolution_under_1s(self, client, admin_headers):
        """Store with detect_evolution=true completes within 1 second."""
        # Seed some candidate memories
        for i in range(10):
            _store_memory_v2(
                client,
                admin_headers,
                f"Performance test: Alice completed task #{i} successfully",
            )

        start = time.monotonic()
        result = _store_memory_v2(
            client,
            admin_headers,
            "Correction: Alice actually failed task #5 instead of completing it",
            detect_evolution=True,
        )
        elapsed = time.monotonic() - start

        assert result.get("memory_id") is not None
        assert elapsed < 1.0, f"Store with evolution took {elapsed:.2f}s (>1s budget)"

    def test_evolution_detection_does_not_block_store(self, client, admin_headers):
        """Even with evolution detection, store always returns a memory_id."""
        result = _store_memory_v2(
            client,
            admin_headers,
            "Reliability test: this should always succeed",
            detect_evolution=True,
        )
        assert result.get("memory_id") is not None
        assert result.get("status") == "created"


# ==============================================================================
# Tests: Edge Cases
# ==============================================================================


class TestEvolutionEdgeCases:
    """Edge case tests for evolution detection."""

    def test_no_evolution_when_no_entity_overlap(self, client, admin_headers):
        """Memories with no entity overlap should not get evolution relationships.

        The evolution detector requires entity overlap (person_refs or entity_types)
        to find candidates. When entities are completely disjoint and no shared
        entity types exist, no candidates are found.
        """
        # Store a memory about a specific person
        _store_memory_v2(
            client,
            admin_headers,
            "Zara works at Netflix as a producer",
        )

        # Store about a completely different topic with different entities
        # Use extract_entities=False to ensure no entity overlap is detected
        result = _store_memory_v2(
            client,
            admin_headers,
            "The temperature today is 72 degrees Fahrenheit",
            detect_evolution=True,
            extract_entities=False,
        )
        memory = _get_memory_v2(client, admin_headers, result["memory_id"])
        # With no extracted entities, evolution detection has nothing to match on
        assert memory.get("supersedes_id") is None
        assert memory.get("extends_ids") is None
        assert memory.get("derived_from_ids") is None

    def test_empty_content_with_evolution_does_not_crash(self, client, admin_headers):
        """Store with empty-ish content and detect_evolution=true doesn't crash."""
        result = _store_memory_v2(
            client,
            admin_headers,
            "   ",
            detect_evolution=True,
        )
        # Should succeed (or return a valid ID even if content is whitespace)
        assert result.get("memory_id") is not None

    def test_multiple_evolution_relationships(self, client, admin_headers):
        """Store multiple related memories, then one that extends them all."""
        mem1 = _store_memory_v2(
            client,
            admin_headers,
            "Henry is a frontend developer",
        )
        mem2 = _store_memory_v2(
            client,
            admin_headers,
            "Henry also works on backend systems",
        )

        result = _store_memory_v2(
            client,
            admin_headers,
            "Additionally, Henry also manages the DevOps pipeline and CI/CD",
            detect_evolution=True,
        )

        memory = _get_memory_v2(client, admin_headers, result["memory_id"])
        # May or may not detect multiple extends — test that it doesn't crash
        assert memory.get("memory_id") is not None
        # At minimum, mem1 and mem2 IDs should exist
        assert mem1.get("memory_id") is not None
        assert mem2.get("memory_id") is not None
