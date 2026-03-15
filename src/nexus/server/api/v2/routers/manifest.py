"""Context Manifest REST API — DEPRECATED (Issue #2984).

These endpoints are deprecated. Context manifest resolution is now available
via the ``nexus_resolve_context`` MCP tool, which provides a stateless
interface without requiring DB persistence.

All endpoints return 410 Gone with a deprecation message directing callers
to the MCP tool.
"""

from fastapi import APIRouter

router = APIRouter(tags=["manifest"])


_DEPRECATION_DETAIL = {
    "message": "Context manifest REST endpoints are deprecated (Issue #2984). "
    "Use the nexus_resolve_context MCP tool instead.",
    "migration": "Pass sources directly to nexus_resolve_context — no DB persistence needed.",
}


@router.get("/api/v2/agents/{agent_id}/manifest", status_code=410)
def get_manifest(agent_id: str) -> dict:  # noqa: ARG001
    """Deprecated — use nexus_resolve_context MCP tool."""
    return _DEPRECATION_DETAIL


@router.put("/api/v2/agents/{agent_id}/manifest", status_code=410)
def set_manifest(agent_id: str) -> dict:  # noqa: ARG001
    """Deprecated — use nexus_resolve_context MCP tool."""
    return _DEPRECATION_DETAIL


@router.post("/api/v2/agents/{agent_id}/manifest/resolve", status_code=410)
def resolve_manifest(agent_id: str) -> dict:  # noqa: ARG001
    """Deprecated — use nexus_resolve_context MCP tool."""
    return _DEPRECATION_DETAIL
