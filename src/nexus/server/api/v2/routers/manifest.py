"""Context Manifest REST API endpoints (Issue #1427, #2130).

Provides:
- GET  /api/v2/agents/{agent_id}/manifest         — Get current manifest
- PUT  /api/v2/agents/{agent_id}/manifest         — Replace manifest (full)
- POST /api/v2/agents/{agent_id}/manifest/resolve — Trigger resolution

All endpoints are authenticated via existing auth middleware.

Note: This module intentionally does NOT use ``from __future__ import annotations``
because FastAPI uses ``eval_str=True`` on dependency signatures at import time.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.server.api.v2.dependencies import (
    _get_operation_context,
    _get_require_auth,
    get_nexus_fs,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["manifest"])

MAX_SOURCES_PER_MANIFEST = 20

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ManifestRequest(BaseModel):
    """Request body for setting a context manifest."""

    sources: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_SOURCES_PER_MANIFEST,
        description="List of context source definitions (max 20).",
    )


class ManifestResponse(BaseModel):
    """Response body for manifest retrieval/update."""

    agent_id: str
    sources: list[dict[str, Any]]
    source_count: int


class ResolveResponse(BaseModel):
    """Response body after manifest resolution."""

    resolved_at: str
    total_ms: float
    source_count: int
    sources: list[dict[str, Any]]
    data: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_agent_registry(nexus_fs: Any) -> Any:
    """Extract AgentRegistry from NexusFS instance."""
    registry = getattr(nexus_fs, "_agent_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Agent registry not initialized")
    return registry


def _get_manifest_resolver_from_state(request: Request) -> Any:
    """Extract ManifestResolver from app state (Issue #2130: DI pattern #4A)."""
    resolver = getattr(request.app.state, "manifest_resolver", None)
    if resolver is None:
        raise HTTPException(status_code=503, detail="Manifest resolver not initialized")
    return resolver


def _get_authorized_agent(
    agent_id: str,
    auth_result: dict[str, Any],
    registry: Any,
) -> Any:
    """Fetch agent record and verify caller ownership (Issue #2130: DRY #5A).

    Raises:
        HTTPException: 404 if agent not found, 403 if not authorized.
    """
    record = registry.get(agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    context = _get_operation_context(auth_result)
    owner_id = context.user_id or ""
    if record.owner_id != owner_id:
        raise HTTPException(status_code=403, detail="Not authorized for this agent")

    return record


def _validate_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate source dicts against Pydantic ContextSource union.

    Returns validated source dicts (round-tripped through Pydantic).
    Raises HTTPException on validation failure.
    """
    from pydantic import TypeAdapter, ValidationError

    from nexus.bricks.context_manifest.models import ContextSource

    adapter: TypeAdapter[ContextSource] = TypeAdapter(ContextSource)
    validated: list[dict[str, Any]] = []
    for i, src in enumerate(sources):
        try:
            model = adapter.validate_python(src)
            validated.append(model.model_dump())
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid source at index {i}: {exc.errors()}",
            ) from exc
    return validated


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/v2/agents/{agent_id}/manifest")
def get_manifest(
    agent_id: str,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> ManifestResponse:
    """Get the current context manifest for an agent."""
    registry = _get_agent_registry(nexus_fs)
    record = _get_authorized_agent(agent_id, auth_result, registry)

    return ManifestResponse(
        agent_id=agent_id,
        sources=list(record.context_manifest),
        source_count=len(record.context_manifest),
    )


@router.put("/api/v2/agents/{agent_id}/manifest")
def set_manifest(
    agent_id: str,
    request: ManifestRequest,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> ManifestResponse:
    """Replace the context manifest for an agent (full replace)."""
    registry = _get_agent_registry(nexus_fs)
    _get_authorized_agent(agent_id, auth_result, registry)  # auth check only

    # Validate sources through Pydantic
    validated = _validate_sources(request.sources)

    # Persist
    updated = registry.update_manifest(agent_id, validated)

    return ManifestResponse(
        agent_id=agent_id,
        sources=list(updated.context_manifest),
        source_count=len(updated.context_manifest),
    )


@router.post("/api/v2/agents/{agent_id}/manifest/resolve")
async def resolve_manifest(
    agent_id: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> ResolveResponse:
    """Trigger manifest resolution for an agent.

    Resolves all sources in the agent's manifest and returns results.
    """
    from pydantic import TypeAdapter

    from nexus.bricks.context_manifest.models import ContextSource, ManifestResolutionError

    registry = _get_agent_registry(nexus_fs)
    resolver = _get_manifest_resolver_from_state(request)
    record = _get_authorized_agent(agent_id, auth_result, registry)

    if not record.context_manifest:
        return ResolveResponse(
            resolved_at=datetime.now(UTC).isoformat(),
            total_ms=0.0,
            source_count=0,
            sources=[],
        )

    # Deserialize manifest dicts to Pydantic models
    adapter: TypeAdapter[ContextSource] = TypeAdapter(ContextSource)
    sources = [adapter.validate_python(d) for d in record.context_manifest]

    # Build template variables from agent context
    variables: dict[str, str] = {
        "agent.id": agent_id,
        "agent.owner_id": record.owner_id,
    }
    if record.zone_id:
        variables["agent.zone_id"] = record.zone_id

    # Populate workspace.root from agent metadata (Issue #1428)
    agent_metadata = getattr(record, "metadata", None) or {}
    workspace_root = agent_metadata.get("workspace_path", "")
    if workspace_root:
        variables["workspace.root"] = workspace_root

    # Per-agent MemoryQueryExecutor wiring (Issue #1428: 1B)
    # Memory is per-agent (scoped by zone/user), so we bind at resolve-time.
    request_resolver = resolver
    _mem_provider = getattr(nexus_fs, "_memory_provider", None)
    memory = _mem_provider.get_for_context() if _mem_provider else None
    if memory is not None:
        try:
            from nexus.bricks.context_manifest.executors.memory_query import (
                MemoryQueryExecutor,
            )
            from nexus.bricks.context_manifest.executors.memory_search_adapter import (
                MemorySearchAdapter,
            )

            mem_adapter = MemorySearchAdapter(memory=memory)
            mem_executor = MemoryQueryExecutor(memory_search=mem_adapter)
            request_resolver = resolver.with_executors({"memory_query": mem_executor})
        except Exception:
            logger.warning("MemoryQueryExecutor wiring failed", exc_info=True)

    # Resolve — skip file writes since results are returned in-memory (15A)
    try:
        result = await request_resolver.resolve(sources, variables)
    except ManifestResolutionError as exc:
        # Return sanitized error info (no internal paths/stack traces)
        failed_summary = [
            {
                "source_type": sr.source_type,
                "source_name": sr.source_name,
                "status": sr.status,
            }
            for sr in exc.failed_sources
        ]
        raise HTTPException(
            status_code=500,
            detail={
                "message": "One or more required sources failed during resolution",
                "failed_sources": failed_summary,
            },
        ) from exc

    source_results = [
        {
            "source_type": sr.source_type,
            "source_name": sr.source_name,
            "status": sr.status,
            "elapsed_ms": sr.elapsed_ms,
            "error_message": sr.error_message,
        }
        for sr in result.sources
    ]

    # Include resolved data in response (Issue #1428: 2A)
    source_data = [
        {
            "source_type": sr.source_type,
            "source_name": sr.source_name,
            "data": sr.data,
        }
        for sr in result.sources
        if sr.status in ("ok", "truncated") and sr.data is not None
    ]

    return ResolveResponse(
        resolved_at=result.resolved_at,
        total_ms=result.total_ms,
        source_count=len(result.sources),
        sources=source_results,
        data=source_data if source_data else None,
    )
