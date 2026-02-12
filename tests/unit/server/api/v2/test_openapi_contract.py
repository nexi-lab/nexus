"""OpenAPI contract / schema snapshot tests.

Issue #995: API versioning — contract enforcement.

These tests guard against accidental breaking changes by:
1. Verifying all expected v2 endpoint paths exist.
2. Checking that response models haven't lost required fields.
3. Validating the API version constant.

A full JSON-snapshot approach is intentionally avoided because it creates
excessive churn. Instead we test structural invariants that matter for
backward compatibility.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from nexus.server.api.v2.versioning import API_VERSION

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def v2_app() -> FastAPI:
    """Build a minimal app with all v2 routers for schema introspection."""
    from nexus.server.api.v2.routers import (
        conflicts,
        consolidation,
        curate,
        feedback,
        memories,
        mobile_search,
        operations,
        playbooks,
        reflect,
        trajectories,
    )

    app = FastAPI(title="Nexus Contract Test", version=API_VERSION)
    for r in [
        memories.router,
        trajectories.router,
        feedback.router,
        playbooks.router,
        reflect.router,
        curate.router,
        consolidation.router,
        mobile_search.router,
        conflicts.router,
        operations.router,
    ]:
        app.include_router(r)
    return app


@pytest.fixture(scope="module")
def openapi_schema(v2_app: FastAPI) -> dict[str, Any]:
    return v2_app.openapi()


@pytest.fixture(scope="module")
def schema_paths(openapi_schema: dict[str, Any]) -> dict[str, Any]:
    return openapi_schema.get("paths", {})


# =============================================================================
# Expected endpoints — the contract
# =============================================================================

# Minimum set of paths that must exist.  New endpoints can be added freely,
# but removing any of these is a breaking change.
REQUIRED_PATHS: set[str] = {
    # Memories (14)
    "/api/v2/memories",
    "/api/v2/memories/{memory_id}",
    "/api/v2/memories/search",
    "/api/v2/memories/query",
    "/api/v2/memories/batch",
    "/api/v2/memories/{memory_id}/history",
    "/api/v2/memories/{memory_id}/versions/{version}",
    "/api/v2/memories/{memory_id}/invalidate",
    "/api/v2/memories/{memory_id}/revalidate",
    "/api/v2/memories/{memory_id}/rollback",
    "/api/v2/memories/{memory_id}/diff",
    "/api/v2/memories/{memory_id}/lineage",
    "/api/v2/memories/stats",
    # Trajectories (5)
    "/api/v2/trajectories",
    "/api/v2/trajectories/{trajectory_id}",
    "/api/v2/trajectories/{trajectory_id}/steps",
    "/api/v2/trajectories/{trajectory_id}/complete",
    # Feedback (5)
    "/api/v2/feedback",
    "/api/v2/feedback/queue",
    "/api/v2/feedback/score",
    "/api/v2/feedback/relearn",
    "/api/v2/feedback/{trajectory_id}",
    # Playbooks (6)
    "/api/v2/playbooks",
    "/api/v2/playbooks/{playbook_id}",
    "/api/v2/playbooks/{playbook_id}/usage",
    # Reflect (1)
    "/api/v2/reflect",
    # Curate (2)
    "/api/v2/curate",
    "/api/v2/curate/bulk",
    # Consolidation (4)
    "/api/v2/consolidate",
    "/api/v2/consolidate/hierarchy",
    "/api/v2/consolidate/decay",
    # Mobile (2)
    "/api/v2/mobile/detect",
    "/api/v2/mobile/download",
    # Conflicts (3)
    "/api/v2/sync/conflicts",
    "/api/v2/sync/conflicts/{conflict_id}/resolve",
    # Operations (2)
    "/api/v2/operations",
}


# =============================================================================
# Tests
# =============================================================================


class TestEndpointContract:
    """Verify all required endpoints exist in the OpenAPI schema."""

    def test_all_required_paths_present(self, schema_paths: dict[str, Any]) -> None:
        actual = set(schema_paths.keys())
        missing = REQUIRED_PATHS - actual
        assert not missing, f"Missing required endpoints: {sorted(missing)}"

    def test_no_unexpected_removals(self, schema_paths: dict[str, Any]) -> None:
        """Sanity check: at least as many paths as required."""
        assert len(schema_paths) >= len(REQUIRED_PATHS)


class TestSchemaStability:
    """Verify critical model schemas haven't lost required fields."""

    def _get_schema_properties(self, openapi_schema: dict[str, Any], model_name: str) -> set[str]:
        """Extract property names from a named schema component."""
        schemas = openapi_schema.get("components", {}).get("schemas", {})
        model = schemas.get(model_name, {})
        return set(model.get("properties", {}).keys())

    def test_memory_store_request_fields(self, openapi_schema: dict[str, Any]) -> None:
        props = self._get_schema_properties(openapi_schema, "MemoryStoreRequest")
        required_fields = {"content", "scope", "memory_type", "importance"}
        assert required_fields.issubset(props), f"Missing fields: {required_fields - props}"

    def test_memory_store_response_fields(self, openapi_schema: dict[str, Any]) -> None:
        props = self._get_schema_properties(openapi_schema, "MemoryStoreResponse")
        required_fields = {"memory_id", "status"}
        assert required_fields.issubset(props), f"Missing fields: {required_fields - props}"

    def test_trajectory_start_request_fields(self, openapi_schema: dict[str, Any]) -> None:
        props = self._get_schema_properties(openapi_schema, "TrajectoryStartRequest")
        required_fields = {"task_description", "task_type"}
        assert required_fields.issubset(props), f"Missing fields: {required_fields - props}"

    def test_operation_response_fields(self, openapi_schema: dict[str, Any]) -> None:
        props = self._get_schema_properties(openapi_schema, "OperationResponse")
        required_fields = {"id", "operation_type", "path", "status", "timestamp"}
        assert required_fields.issubset(props), f"Missing fields: {required_fields - props}"

    def test_reflection_response_fields(self, openapi_schema: dict[str, Any]) -> None:
        props = self._get_schema_properties(openapi_schema, "ReflectionResponse")
        required_fields = {"memory_id", "trajectory_id", "confidence"}
        assert required_fields.issubset(props), f"Missing fields: {required_fields - props}"

    def test_conflict_detail_response_fields(self, openapi_schema: dict[str, Any]) -> None:
        props = self._get_schema_properties(openapi_schema, "ConflictDetailResponse")
        required_fields = {"conflict_id", "path", "status", "strategy", "outcome"}
        assert required_fields.issubset(props), f"Missing fields: {required_fields - props}"


class TestApiVersion:
    def test_version_format(self) -> None:
        """API_VERSION must be semver-ish MAJOR.MINOR."""
        parts = API_VERSION.split(".")
        assert len(parts) == 2
        assert all(p.isdigit() for p in parts)

    def test_current_version(self) -> None:
        assert API_VERSION == "2.0"
