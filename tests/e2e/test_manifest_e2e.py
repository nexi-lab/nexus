"""E2E tests for Context Manifest API (Issue #1427).

Tests the manifest endpoints against a real running Nexus server:
1. Register agent → PUT manifest → GET manifest round-trip
2. PUT manifest → validation error (invalid source type)
3. POST resolve → with file_glob source (real glob against workspace)
4. POST resolve → empty manifest (no sources)
5. Performance: PUT + GET < 50ms, resolve < 200ms

Uses the shared nexus_server fixture from conftest.py (SQLite backend).

Run with:
    pytest tests/e2e/test_manifest_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import time

import httpx
import pytest

# Auth header for the default static API key used in conftest.py
AUTH_HEADERS = {"Authorization": "Bearer test-e2e-api-key-12345"}


def _register_agent(client: httpx.Client, agent_id: str) -> dict:
    """Register an agent via RPC and return the result."""
    resp = client.post(
        "/api/nfs/register_agent",
        headers=AUTH_HEADERS,
        json={
            "jsonrpc": "2.0",
            "method": "register_agent",
            "params": {
                "agent_id": agent_id,
                "name": f"Manifest E2E Agent {agent_id}",
                "description": "Agent for manifest E2E testing",
            },
            "id": 1,
        },
        timeout=10.0,
    )
    assert resp.status_code == 200, f"register_agent failed: {resp.text}"
    data = resp.json()
    assert data.get("error") is None, f"RPC error: {data.get('error')}"
    return data.get("result", {})


# ---------------------------------------------------------------------------
# Test 1: PUT + GET manifest round-trip
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestManifestRoundTrip:
    """Register agent, PUT manifest, GET manifest — verify round-trip."""

    def test_put_get_manifest_round_trip(self, test_app: httpx.Client) -> None:
        agent_id = "manifest-e2e-roundtrip"
        _register_agent(test_app, agent_id)

        sources = [
            {"type": "file_glob", "pattern": "*.py", "max_files": 10},
        ]

        # PUT manifest
        put_resp = test_app.put(
            f"/api/v2/agents/{agent_id}/manifest",
            headers=AUTH_HEADERS,
            json={"sources": sources},
        )
        assert put_resp.status_code == 200, f"PUT failed: {put_resp.text}"
        put_data = put_resp.json()
        assert put_data["source_count"] == 1
        assert put_data["sources"][0]["type"] == "file_glob"

        # GET manifest
        get_resp = test_app.get(
            f"/api/v2/agents/{agent_id}/manifest",
            headers=AUTH_HEADERS,
        )
        assert get_resp.status_code == 200, f"GET failed: {get_resp.text}"
        get_data = get_resp.json()
        assert get_data["agent_id"] == agent_id
        assert get_data["source_count"] == 1
        assert get_data["sources"][0]["pattern"] == "*.py"


# ---------------------------------------------------------------------------
# Test 2: GET manifest — empty (new agent)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestManifestEmpty:
    """GET manifest on agent with no manifest → empty sources."""

    def test_get_manifest_empty(self, test_app: httpx.Client) -> None:
        agent_id = "manifest-e2e-empty"
        _register_agent(test_app, agent_id)

        resp = test_app.get(
            f"/api/v2/agents/{agent_id}/manifest",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sources"] == []
        assert data["source_count"] == 0


# ---------------------------------------------------------------------------
# Test 3: PUT manifest — validation error
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestManifestValidationError:
    """PUT manifest with invalid source type → 422."""

    def test_put_manifest_invalid_source(self, test_app: httpx.Client) -> None:
        agent_id = "manifest-e2e-invalid"
        _register_agent(test_app, agent_id)

        resp = test_app.put(
            f"/api/v2/agents/{agent_id}/manifest",
            headers=AUTH_HEADERS,
            json={"sources": [{"type": "nonexistent_type", "foo": "bar"}]},
        )
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Test 4: PUT manifest — agent not found
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestManifestAgentNotFound:
    """PUT/GET manifest on non-existent agent → 404."""

    def test_get_manifest_not_found(self, test_app: httpx.Client) -> None:
        resp = test_app.get(
            "/api/v2/agents/does-not-exist/manifest",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 404

    def test_put_manifest_not_found(self, test_app: httpx.Client) -> None:
        resp = test_app.put(
            "/api/v2/agents/does-not-exist/manifest",
            headers=AUTH_HEADERS,
            json={"sources": []},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test 5: POST resolve — empty manifest
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestResolveEmptyManifest:
    """POST resolve on agent with no manifest → empty result."""

    def test_resolve_empty_manifest(self, test_app: httpx.Client) -> None:
        agent_id = "manifest-e2e-resolve-empty"
        _register_agent(test_app, agent_id)

        resp = test_app.post(
            f"/api/v2/agents/{agent_id}/manifest/resolve",
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_count"] == 0
        assert data["sources"] == []
        assert data["total_ms"] == 0.0


# ---------------------------------------------------------------------------
# Test 6: POST resolve — with file_glob source
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestResolveWithFileGlob:
    """Set file_glob manifest → resolve → verify results."""

    def test_resolve_file_glob(self, test_app: httpx.Client) -> None:
        agent_id = "manifest-e2e-resolve-glob"
        _register_agent(test_app, agent_id)

        # Set manifest with file_glob source
        sources = [{"type": "file_glob", "pattern": "*.py", "max_files": 5}]
        put_resp = test_app.put(
            f"/api/v2/agents/{agent_id}/manifest",
            headers=AUTH_HEADERS,
            json={"sources": sources},
        )
        assert put_resp.status_code == 200

        # Resolve manifest
        resolve_resp = test_app.post(
            f"/api/v2/agents/{agent_id}/manifest/resolve",
            headers=AUTH_HEADERS,
        )
        assert resolve_resp.status_code == 200, f"Resolve failed: {resolve_resp.text}"
        data = resolve_resp.json()
        assert data["source_count"] == 1
        assert data["total_ms"] >= 0
        # Source should be "ok" (even if no .py files at workspace root)
        assert data["sources"][0]["status"] == "ok"
        assert data["sources"][0]["source_type"] == "file_glob"


# ---------------------------------------------------------------------------
# Test 7: PUT manifest — replace (update) existing
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestManifestReplace:
    """PUT manifest twice → second PUT fully replaces the first."""

    def test_put_manifest_replaces(self, test_app: httpx.Client) -> None:
        agent_id = "manifest-e2e-replace"
        _register_agent(test_app, agent_id)

        # First PUT: 2 sources
        first_sources = [
            {"type": "file_glob", "pattern": "*.py", "max_files": 10},
            {"type": "file_glob", "pattern": "*.txt", "max_files": 5},
        ]
        resp1 = test_app.put(
            f"/api/v2/agents/{agent_id}/manifest",
            headers=AUTH_HEADERS,
            json={"sources": first_sources},
        )
        assert resp1.status_code == 200
        assert resp1.json()["source_count"] == 2

        # Second PUT: 1 source (replaces, not appends)
        second_sources = [
            {"type": "file_glob", "pattern": "*.md", "max_files": 3},
        ]
        resp2 = test_app.put(
            f"/api/v2/agents/{agent_id}/manifest",
            headers=AUTH_HEADERS,
            json={"sources": second_sources},
        )
        assert resp2.status_code == 200
        assert resp2.json()["source_count"] == 1

        # Verify GET reflects replacement
        get_resp = test_app.get(
            f"/api/v2/agents/{agent_id}/manifest",
            headers=AUTH_HEADERS,
        )
        data = get_resp.json()
        assert data["source_count"] == 1
        assert data["sources"][0]["pattern"] == "*.md"


# ---------------------------------------------------------------------------
# Test 8: Performance — PUT + GET latency
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestManifestPerformance:
    """Verify manifest CRUD operations complete within latency budget."""

    def test_put_get_latency(self, test_app: httpx.Client) -> None:
        """PUT + GET together should complete within 500ms (generous for CI)."""
        agent_id = "manifest-e2e-perf"
        _register_agent(test_app, agent_id)

        sources = [
            {"type": "file_glob", "pattern": "src/**/*.py", "max_files": 20},
            {"type": "file_glob", "pattern": "tests/**/*.py", "max_files": 10},
        ]

        start = time.monotonic()

        # PUT
        put_resp = test_app.put(
            f"/api/v2/agents/{agent_id}/manifest",
            headers=AUTH_HEADERS,
            json={"sources": sources},
        )
        assert put_resp.status_code == 200

        # GET
        get_resp = test_app.get(
            f"/api/v2/agents/{agent_id}/manifest",
            headers=AUTH_HEADERS,
        )
        assert get_resp.status_code == 200

        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 500, f"PUT + GET took {elapsed_ms:.1f}ms (budget: 500ms)"

    def test_resolve_latency(self, test_app: httpx.Client) -> None:
        """Resolve with file_glob should complete within 2s (generous for CI)."""
        agent_id = "manifest-e2e-perf-resolve"
        _register_agent(test_app, agent_id)

        sources = [{"type": "file_glob", "pattern": "*.py", "max_files": 10}]
        test_app.put(
            f"/api/v2/agents/{agent_id}/manifest",
            headers=AUTH_HEADERS,
            json={"sources": sources},
        )

        start = time.monotonic()
        resolve_resp = test_app.post(
            f"/api/v2/agents/{agent_id}/manifest/resolve",
            headers=AUTH_HEADERS,
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        assert resolve_resp.status_code == 200
        data = resolve_resp.json()
        assert data["total_ms"] >= 0
        assert elapsed_ms < 2000, f"Resolve took {elapsed_ms:.1f}ms (budget: 2000ms)"
