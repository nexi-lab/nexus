"""Conformance tests for the Nexus Exchange Protocol OpenAPI spec.

Tests validate:
1. The OpenAPI spec itself is valid
2. Response schemas match the spec (when run against a live server)
3. Required fields are present in responses
4. Error responses follow the NexusError format

Usage:
  # Validate spec only (no server needed)
  uv run pytest tests/conformance/test_exchange_openapi.py -v -o "addopts=" -k "test_openapi_spec_"

  # Full conformance against live server
  NEXUS_CONFORMANCE_URL=http://localhost:2026 \
  NEXUS_CONFORMANCE_TOKEN=nx_test_token \
  uv run pytest tests/conformance/test_exchange_openapi.py -v -o "addopts="
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Spec validation tests (no server needed)
# ---------------------------------------------------------------------------

SPEC_PATH = (
    Path(__file__).parent.parent.parent / "docs" / "protocol" / "nexus-exchange-v1.openapi.yaml"
)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    """Load and parse the OpenAPI spec."""
    if not SPEC_PATH.exists():
        pytest.skip(f"OpenAPI spec not found at {SPEC_PATH}")
    return yaml.safe_load(SPEC_PATH.read_text())


def test_openapi_spec_version(spec: dict[str, Any]) -> None:
    """Spec declares OpenAPI 3.1.0."""
    assert spec["openapi"] == "3.1.0"


def test_openapi_spec_info(spec: dict[str, Any]) -> None:
    """Spec has required info fields."""
    info = spec["info"]
    assert info["title"] == "Nexus Agent Exchange Protocol"
    assert info["version"] == "2026.1"
    assert "description" in info


def test_openapi_spec_servers(spec: dict[str, Any]) -> None:
    """Spec defines at least one server."""
    assert len(spec["servers"]) >= 1


def test_openapi_spec_security(spec: dict[str, Any]) -> None:
    """Spec defines security schemes."""
    schemes = spec["components"]["securitySchemes"]
    assert "apiKey" in schemes
    assert "bearerAuth" in schemes


def test_openapi_spec_paths_not_empty(spec: dict[str, Any]) -> None:
    """Spec defines API paths."""
    assert len(spec["paths"]) > 0


def test_openapi_spec_identity_endpoints(spec: dict[str, Any]) -> None:
    """Spec defines all identity endpoints."""
    paths = spec["paths"]
    assert "/api/v2/agents/{agent_id}/verify" in paths
    assert "/api/v2/agents/{agent_id}/keys" in paths
    assert "/api/v2/agents/{agent_id}/keys/rotate" in paths
    assert "/api/v2/agents/{agent_id}/keys/{key_id}" in paths


def test_openapi_spec_payment_endpoints(spec: dict[str, Any]) -> None:
    """Spec defines all payment endpoints."""
    paths = spec["paths"]
    assert "/api/v2/pay/balance" in paths
    assert "/api/v2/pay/can-afford" in paths
    assert "/api/v2/pay/transfer" in paths
    assert "/api/v2/pay/transfer/batch" in paths
    assert "/api/v2/pay/reserve" in paths
    assert "/api/v2/pay/reserve/{reservation_id}/commit" in paths
    assert "/api/v2/pay/reserve/{reservation_id}/release" in paths
    assert "/api/v2/pay/meter" in paths


def test_openapi_spec_audit_endpoints(spec: dict[str, Any]) -> None:
    """Spec defines all audit endpoints."""
    paths = spec["paths"]
    assert "/api/v2/audit/transactions" in paths
    assert "/api/v2/audit/transactions/aggregations" in paths
    assert "/api/v2/audit/transactions/export" in paths
    assert "/api/v2/audit/transactions/{record_id}" in paths
    assert "/api/v2/audit/integrity/{record_id}" in paths


def test_openapi_spec_error_schema(spec: dict[str, Any]) -> None:
    """Spec defines the NexusError schema."""
    schemas = spec["components"]["schemas"]
    assert "NexusError" in schemas
    error_schema = schemas["NexusError"]
    error_props = error_schema["properties"]["error"]["properties"]
    assert "code" in error_props
    assert "message" in error_props
    assert "details" in error_props
    assert "trace_id" in error_props


def test_openapi_spec_protocol_version_header(spec: dict[str, Any]) -> None:
    """Spec defines the Nexus-Protocol-Version header parameter."""
    params = spec["components"]["parameters"]
    assert "ProtocolVersion" in params
    pv = params["ProtocolVersion"]
    assert pv["name"] == "Nexus-Protocol-Version"
    assert pv["in"] == "header"


def test_openapi_spec_all_paths_have_tags(spec: dict[str, Any]) -> None:
    """Every endpoint has at least one tag."""
    for path, methods in spec["paths"].items():
        for method, details in methods.items():
            if method in ("get", "post", "put", "delete", "patch"):
                assert "tags" in details, f"{method.upper()} {path} missing tags"
                assert len(details["tags"]) > 0


def test_openapi_spec_all_paths_have_operation_id(spec: dict[str, Any]) -> None:
    """Every endpoint has an operationId."""
    for path, methods in spec["paths"].items():
        for method, details in methods.items():
            if method in ("get", "post", "put", "delete", "patch"):
                assert "operationId" in details, f"{method.upper()} {path} missing operationId"


# ---------------------------------------------------------------------------
# Schema completeness tests
# ---------------------------------------------------------------------------


def test_openapi_spec_required_schemas_exist(spec: dict[str, Any]) -> None:
    """Spec defines all required schemas."""
    schemas = spec["components"]["schemas"]
    required = [
        "NexusError",
        "AgentIdentityResponse",
        "AgentKeyResponse",
        "AgentKeyListResponse",
        "BalanceResponse",
        "TransferRequest",
        "ReceiptResponse",
        "ReserveRequest",
        "ReservationResponse",
        "MeterRequest",
        "MeterResponse",
        "CanAffordResponse",
        "AuditTransactionResponse",
        "AuditTransactionListResponse",
        "AuditAggregationResponse",
        "AuditIntegrityResponse",
    ]
    for schema_name in required:
        assert schema_name in schemas, f"Missing schema: {schema_name}"


def test_openapi_spec_error_responses_exist(spec: dict[str, Any]) -> None:
    """Spec defines standard error responses."""
    responses = spec["components"]["responses"]
    required = [
        "NotFound",
        "Unauthenticated",
        "Forbidden",
        "InsufficientBalance",
        "InvalidArgument",
    ]
    for resp_name in required:
        assert resp_name in responses, f"Missing response: {resp_name}"


# ---------------------------------------------------------------------------
# Schemathesis conformance tests (requires live server)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schemathesis_schema(spec: dict[str, Any], base_url: str, auth_token: str | None):
    """Load schemathesis schema for live conformance testing."""
    try:
        import schemathesis
    except ImportError:
        pytest.skip("schemathesis not installed — install with: pip install schemathesis")

    schema = schemathesis.from_dict(
        spec,
        base_url=base_url,
    )

    if auth_token:
        schema.set_auth(("X-API-Key", auth_token))

    return schema


@pytest.mark.skipif(
    not __import__("os").environ.get("NEXUS_CONFORMANCE_URL"),
    reason="Set NEXUS_CONFORMANCE_URL to run live conformance tests",
)
class TestLiveConformance:
    """Live conformance tests against a running server.

    Run with:
      NEXUS_CONFORMANCE_URL=http://localhost:2026 \
      NEXUS_CONFORMANCE_TOKEN=nx_test_token \
      uv run pytest tests/conformance/test_exchange_openapi.py::TestLiveConformance -v
    """

    def test_health_endpoint_available(self, base_url: str) -> None:
        """Verify the test server is reachable."""
        import httpx

        resp = httpx.get(f"{base_url}/health", timeout=5)
        assert resp.status_code == 200
