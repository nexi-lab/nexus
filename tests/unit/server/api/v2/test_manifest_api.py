"""Tests for Context Manifest REST API endpoints (Issue #1427).

Covers:
1.  GET  manifest — empty (new agent)
2.  PUT  manifest — success
3.  PUT  manifest — validation error (invalid source type)
4.  PUT  manifest — unauthorized (no auth)
5.  PUT  manifest — wrong owner (403)
6.  PUT  manifest — agent not found (404)
7.  GET  manifest — after PUT round-trip
8.  POST resolve — happy path
9.  POST resolve — empty manifest
10. POST resolve — agent not found (404)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.server.api.v2.routers.manifest import router
from nexus.services.agents.agent_registry import AgentRegistry
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def agent_registry(session_factory):
    return AgentRegistry(session_factory=session_factory)


def _make_mock_context(user_id: str = "owner-1", zone_id: str = "zone-1"):
    """Create a mock operation context."""
    ctx = MagicMock()
    ctx.user_id = user_id
    ctx.user = user_id
    ctx.zone_id = zone_id
    return ctx


@pytest.fixture
def app(agent_registry):
    """Create a test FastAPI app with the manifest router mounted."""
    test_app = FastAPI()
    test_app.include_router(router)

    # Mock NexusFS with agent registry attached
    mock_nexus_fs = MagicMock()
    mock_nexus_fs._agent_registry = agent_registry
    mock_nexus_fs._services = MagicMock()
    # manifest_resolver accessed via _service_extras dict
    mock_nexus_fs._service_extras = {"manifest_resolver": MagicMock()}

    # Override dependencies
    from nexus.server.api.v2.routers.manifest import _get_require_auth, get_nexus_fs

    test_app.dependency_overrides[get_nexus_fs] = lambda: mock_nexus_fs
    test_app.dependency_overrides[_get_require_auth()] = lambda: {"user_id": "owner-1"}

    return test_app


@pytest.fixture
def client(app, agent_registry):
    """TestClient that patches auth context."""
    with patch(
        "nexus.server.api.v2.routers.manifest._get_operation_context",
        return_value=_make_mock_context("owner-1"),
    ):
        yield TestClient(app)


@pytest.fixture
def registered_agent(agent_registry) -> str:
    """Register a test agent and return its ID."""
    agent_registry.register("agent-1", "owner-1", zone_id="zone-1")
    return "agent-1"


# ---------------------------------------------------------------------------
# Test 1: GET manifest — empty
# ---------------------------------------------------------------------------


class TestGetManifestEmpty:
    def test_get_manifest_empty(self, client, registered_agent):
        resp = client.get(f"/api/v2/agents/{registered_agent}/manifest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent-1"
        assert data["sources"] == []
        assert data["source_count"] == 0


# ---------------------------------------------------------------------------
# Test 2: PUT manifest — success
# ---------------------------------------------------------------------------


class TestPutManifestSuccess:
    def test_put_manifest_success(self, client, registered_agent):
        sources = [
            {"type": "file_glob", "pattern": "*.py", "max_files": 10},
            {"type": "memory_query", "query": "auth context", "top_k": 5},
        ]
        resp = client.put(
            f"/api/v2/agents/{registered_agent}/manifest",
            json={"sources": sources},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_count"] == 2
        assert data["sources"][0]["type"] == "file_glob"


# ---------------------------------------------------------------------------
# Test 3: PUT manifest — validation error
# ---------------------------------------------------------------------------


class TestPutManifestValidationError:
    def test_put_manifest_validation_error(self, client, registered_agent):
        """Invalid source type → 422."""
        sources = [{"type": "invalid_type", "foo": "bar"}]
        resp = client.put(
            f"/api/v2/agents/{registered_agent}/manifest",
            json={"sources": sources},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test 4: PUT manifest — unauthorized (no auth mock returns no user)
# ---------------------------------------------------------------------------


class TestPutManifestUnauthorized:
    def test_put_manifest_agent_not_found(self, client):
        """Agent not found → 404."""
        resp = client.put(
            "/api/v2/agents/nonexistent/manifest",
            json={"sources": []},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test 5: PUT manifest — wrong owner
# ---------------------------------------------------------------------------


class TestPutManifestWrongOwner:
    def test_put_manifest_wrong_owner(self, app, agent_registry):
        """Different user → 403."""
        agent_registry.register("agent-other", "other-owner", zone_id="zone-1")

        # Client authenticated as "owner-1" tries to access "other-owner"'s agent
        with patch(
            "nexus.server.api.v2.routers.manifest._get_operation_context",
            return_value=_make_mock_context("owner-1"),
        ):
            c = TestClient(app)
            resp = c.put(
                "/api/v2/agents/agent-other/manifest",
                json={"sources": []},
            )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test 6: PUT manifest — agent not found
# ---------------------------------------------------------------------------


class TestPutManifestNotFound:
    def test_put_manifest_not_found(self, client):
        resp = client.put(
            "/api/v2/agents/does-not-exist/manifest",
            json={"sources": []},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test 7: GET manifest — after PUT round-trip
# ---------------------------------------------------------------------------


class TestGetManifestAfterPut:
    def test_get_manifest_after_put(self, client, registered_agent):
        sources = [{"type": "file_glob", "pattern": "src/**/*.py", "max_files": 20}]
        client.put(
            f"/api/v2/agents/{registered_agent}/manifest",
            json={"sources": sources},
        )
        resp = client.get(f"/api/v2/agents/{registered_agent}/manifest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_count"] == 1
        assert data["sources"][0]["pattern"] == "src/**/*.py"


# ---------------------------------------------------------------------------
# Test 8: POST resolve — happy path
# ---------------------------------------------------------------------------


class TestResolveHappyPath:
    def test_resolve_happy_path(self, app, agent_registry):
        """Set manifest then resolve — returns ManifestResult-like response."""
        from nexus.services.context_manifest.models import ManifestResult, SourceResult

        agent_registry.register("agent-resolve", "owner-1")
        sources = [{"type": "file_glob", "pattern": "*.py", "max_files": 5}]
        agent_registry.update_manifest("agent-resolve", sources)

        # Mock the resolver to return a ManifestResult
        mock_result = ManifestResult(
            sources=(
                SourceResult.ok(
                    source_type="file_glob",
                    source_name="*.py",
                    data={"files": {"main.py": "print()"}},
                    elapsed_ms=3.0,
                ),
            ),
            resolved_at="2026-01-15T12:00:00+00:00",
            total_ms=5.0,
        )

        # Get the mock nexus_fs from app dependencies
        from nexus.server.api.v2.routers.manifest import get_nexus_fs

        mock_fs = app.dependency_overrides[get_nexus_fs]()

        # Make resolve an async mock
        async def mock_resolve(_sources: object, _variables: object, _output_dir: object) -> object:
            return mock_result

        mock_fs._service_extras["manifest_resolver"].resolve = mock_resolve

        with patch(
            "nexus.server.api.v2.routers.manifest._get_operation_context",
            return_value=_make_mock_context("owner-1"),
        ):
            c = TestClient(app)
            resp = c.post("/api/v2/agents/agent-resolve/manifest/resolve")

        assert resp.status_code == 200
        data = resp.json()
        assert data["source_count"] == 1
        assert data["total_ms"] > 0
        assert data["sources"][0]["status"] == "ok"


# ---------------------------------------------------------------------------
# Test 9: POST resolve — empty manifest
# ---------------------------------------------------------------------------


class TestResolveEmptyManifest:
    def test_resolve_empty_manifest(self, client, registered_agent):
        resp = client.post(f"/api/v2/agents/{registered_agent}/manifest/resolve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_count"] == 0
        assert data["sources"] == []


# ---------------------------------------------------------------------------
# Test 10: POST resolve — agent not found
# ---------------------------------------------------------------------------


class TestResolveNotFound:
    def test_resolve_agent_not_found(self, client):
        resp = client.post("/api/v2/agents/nonexistent/manifest/resolve")
        assert resp.status_code == 404
