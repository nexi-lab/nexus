"""Connector capability discovery REST API (Issue #2069).

Endpoints for querying registered connectors and their capabilities.
Public endpoints — no auth required (discovery only, like /api/v2/features).

Endpoints:
    GET  /api/v2/connectors — List all registered connectors with capabilities
    GET  /api/v2/connectors/{name}/capabilities — Get capabilities for a connector
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/connectors", tags=["connectors"])

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ConnectorSummary(BaseModel):
    """Summary of a registered connector."""

    name: str
    description: str
    category: str
    capabilities: list[str]
    user_scoped: bool


class ConnectorsListResponse(BaseModel):
    """Response for GET /api/v2/connectors."""

    connectors: list[ConnectorSummary]


class ConnectorCapabilitiesResponse(BaseModel):
    """Response for GET /api/v2/connectors/{name}/capabilities."""

    name: str
    capabilities: list[str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=ConnectorsListResponse)
def list_connectors(_: dict = Depends(require_auth)) -> ConnectorsListResponse:
    """List all registered connectors with their capabilities."""
    from nexus.backends.base.registry import ConnectorRegistry

    connectors = []
    for info in ConnectorRegistry.list_all():
        connectors.append(
            ConnectorSummary(
                name=info.name,
                description=info.description,
                category=info.category,
                capabilities=sorted(str(c) for c in info.capabilities),
                user_scoped=info.user_scoped,
            )
        )

    return ConnectorsListResponse(connectors=connectors)


@router.get("/{name}/capabilities", response_model=ConnectorCapabilitiesResponse)
def get_connector_capabilities(name: str) -> ConnectorCapabilitiesResponse:
    """Get capabilities for a specific connector."""
    from nexus.backends.base.registry import ConnectorRegistry

    if not ConnectorRegistry.is_registered(name):
        raise HTTPException(status_code=404, detail=f"Connector '{name}' not found")

    info = ConnectorRegistry.get_info(name)
    return ConnectorCapabilitiesResponse(
        name=info.name,
        capabilities=sorted(str(c) for c in info.capabilities),
    )
