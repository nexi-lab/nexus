"""Connector management REST API (Issue #2069, #3148, #3182).

Endpoints for connector discovery, mounting, sync, skill docs, and writes.
Used by the TUI Connectors tab and CLI.

Endpoints:
    GET   /api/v2/connectors                       — List registered connectors
    GET   /api/v2/connectors/{name}/capabilities   — Connector capabilities
    GET   /api/v2/connectors/available              — Connectors with auth/mount status
    POST  /api/v2/connectors/mount                  — Mount a connector
    GET   /api/v2/connectors/mounts                 — List mounted connectors
    POST  /api/v2/connectors/sync                   — Trigger sync for a mount
    GET   /api/v2/connectors/{mount_path:path}/skill — Get SKILL.md
    GET   /api/v2/connectors/{mount_path:path}/schema/{operation} — Get schema
    POST  /api/v2/connectors/{mount_path:path}/write — Validated write
    POST  /api/v2/connectors/unmount                — Unmount a connector
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

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


class AvailableConnector(BaseModel):
    """Connector with auth and mount status (for TUI)."""

    name: str
    description: str
    category: str
    capabilities: list[str]
    user_scoped: bool
    auth_status: str = "unknown"  # "authed", "no_auth", "expired", "unknown"
    mount_path: str | None = None  # None if not mounted
    sync_status: str | None = None  # "synced", "syncing", "error", None


class MountRequest(BaseModel):
    """Request to mount a connector."""

    connector_type: str = Field(..., description="Connector backend type")
    mount_point: str = Field(..., description="VFS mount path (e.g., /mnt/gmail)")
    config: dict[str, Any] = Field(default_factory=dict, description="Backend config")
    readonly: bool = False


class MountResponse(BaseModel):
    """Response from mount operation."""

    mounted: bool
    mount_point: str
    error: str | None = None


class SyncRequest(BaseModel):
    """Request to sync a mount."""

    mount_point: str
    recursive: bool = True
    full_sync: bool = False


class SyncResponse(BaseModel):
    """Response from sync operation."""

    mount_point: str
    files_scanned: int = 0
    files_synced: int = 0
    delta_added: int = 0
    delta_deleted: int = 0
    history_id: str | None = None
    is_delta: bool = False
    error: str | None = None


class WriteRequest(BaseModel):
    """Request to write YAML to a connector path."""

    yaml_content: str = Field(..., description="YAML content to write")


class WriteResponse(BaseModel):
    """Response from write operation."""

    success: bool
    content_hash: str | None = None
    error: str | None = None


class SkillDocResponse(BaseModel):
    """SKILL.md content for a mount."""

    mount_point: str
    content: str
    schemas: list[str] = Field(default_factory=list)


class SchemaResponse(BaseModel):
    """Annotated schema for an operation."""

    mount_point: str
    operation: str
    content: str


class MountInfo(BaseModel):
    """Info about a mounted connector."""

    mount_point: str
    readonly: bool
    connector_type: str | None = None
    skill_name: str | None = None
    operations: list[str] = Field(default_factory=list)
    sync_status: str | None = None
    last_sync: str | None = None


# ---------------------------------------------------------------------------
# Helper: get NexusFS from request
# ---------------------------------------------------------------------------


def _get_nx(request: Request) -> Any:
    nx = getattr(request.app.state, "nexus_fs", None)
    if nx is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    return nx


def _get_mount_service(request: Request) -> Any:
    nx = _get_nx(request)
    svc = nx.service("mount")
    if svc is None:
        raise HTTPException(status_code=503, detail="Mount service not available")
    return svc


# ---------------------------------------------------------------------------
# Discovery endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=ConnectorsListResponse)
def list_connectors(_: dict = Depends(require_auth)) -> ConnectorsListResponse:
    """List all registered connectors with their capabilities."""
    from nexus.backends import _register_optional_backends
    from nexus.backends.base.registry import ConnectorRegistry

    _register_optional_backends()

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


@router.get("/available", response_model=list[AvailableConnector])
async def list_available_connectors(
    request: Request,
    _: dict = Depends(require_auth),
) -> list[AvailableConnector]:
    """List connectors with auth and mount status (for TUI Connectors tab)."""
    from nexus.backends.base.registry import ConnectorRegistry

    nx = _get_nx(request)
    mount_svc = nx.service("mount")

    # Get mounted connectors
    mounted: dict[str, str] = {}  # connector_type -> mount_point
    if mount_svc:
        try:
            mounts = await mount_svc.list_mounts()
            for m in mounts:
                mp = m.get("mount_point", "")
                if mp.startswith("/mnt/"):
                    # Infer type from mount point name
                    mounted[mp] = mp
        except Exception:
            pass

    result = []
    for info in ConnectorRegistry.list_all():
        # Check if this connector is mounted
        mount_path = None
        for mp in mounted:
            if info.name.replace("_connector", "") in mp:
                mount_path = mp
                break

        result.append(
            AvailableConnector(
                name=info.name,
                description=info.description,
                category=info.category,
                capabilities=sorted(str(c) for c in info.capabilities),
                user_scoped=info.user_scoped,
                mount_path=mount_path,
            )
        )

    return result


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


# ---------------------------------------------------------------------------
# Mount management endpoints (Issue #3148)
# ---------------------------------------------------------------------------


@router.post("/mount", response_model=MountResponse)
async def mount_connector(
    req: MountRequest,
    request: Request,
    _: dict = Depends(require_auth),
) -> MountResponse:
    """Mount a connector at a VFS path."""
    mount_svc = _get_mount_service(request)
    try:
        result = await mount_svc.add_mount(
            mount_point=req.mount_point,
            backend_type=req.connector_type,
            backend_config=req.config,
            readonly=req.readonly,
        )
        return MountResponse(mounted=True, mount_point=str(result))
    except Exception as e:
        return MountResponse(mounted=False, mount_point=req.mount_point, error=str(e))


@router.get("/mounts", response_model=list[MountInfo])
async def list_mounted_connectors(
    request: Request,
    _: dict = Depends(require_auth),
) -> list[MountInfo]:
    """List all mounted connectors with status."""
    mount_svc = _get_mount_service(request)
    mounts = await mount_svc.list_mounts()

    nx = _get_nx(request)
    result = []
    for m in mounts:
        mp = m.get("mount_point", "")
        skill_name = None
        operations: list[str] = []
        try:
            # Route to a dummy file path inside the mount to get the backend
            route = nx.router.route(f"{mp.rstrip('/')}/_.yaml")
            if route:
                backend = route.backend
                skill_name = getattr(backend, "SKILL_NAME", None)
                # Check multiple sources for operation names
                schemas = getattr(backend, "SCHEMAS", {})
                traits = getattr(backend, "OPERATION_TRAITS", {})
                if schemas:
                    operations = list(schemas.keys())
                elif traits:
                    operations = list(traits.keys())
        except Exception:
            pass

        result.append(
            MountInfo(
                mount_point=mp,
                readonly=m.get("readonly", False),
                skill_name=skill_name,
                operations=operations,
            )
        )

    return result


@router.post("/unmount", response_model=MountResponse)
async def unmount_connector(
    req: MountRequest,
    request: Request,
    _: dict = Depends(require_auth),
) -> MountResponse:
    """Unmount a connector."""
    mount_svc = _get_mount_service(request)
    try:
        await mount_svc.remove_mount(mount_point=req.mount_point)
        return MountResponse(mounted=False, mount_point=req.mount_point)
    except Exception as e:
        return MountResponse(mounted=False, mount_point=req.mount_point, error=str(e))


# ---------------------------------------------------------------------------
# Sync endpoints (Issue #3148)
# ---------------------------------------------------------------------------


@router.post("/sync", response_model=SyncResponse)
async def sync_mount(
    req: SyncRequest,
    request: Request,
    auth: dict = Depends(require_auth),
) -> SyncResponse:
    """Trigger sync for a mounted connector.

    If the backend supports delta sync (e.g., Gmail historyId, Calendar syncToken),
    the response includes delta_added/delta_deleted counts and history_id.
    """
    import asyncio

    nx = _get_nx(request)
    mount_svc = _get_mount_service(request)

    # Check if backend supports delta sync
    mp = req.mount_point.rstrip("/")
    backend = None
    try:
        route = nx.router.route(f"{mp}/_.yaml")
        if route:
            backend = route.backend
    except Exception:
        pass

    # Try delta sync first if backend supports it
    delta_result: dict[str, Any] | None = None
    if backend and hasattr(backend, "sync_delta") and not req.full_sync:
        try:
            delta_result = await asyncio.to_thread(backend.sync_delta)
        except Exception:
            delta_result = None

    # Build context with zone_id from authenticated user — ensures synced
    # files are in the same zone as the user's API key, so they're visible
    # in the TUI and HTTP file listing (which apply zone isolation).
    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.types import OperationContext

    sync_context = OperationContext(
        user_id=auth.get("subject_id", "system"),
        groups=[],
        is_admin=auth.get("is_admin", True),
        is_system=True,
        zone_id=auth.get("zone_id") or ROOT_ZONE_ID,
    )

    # Run full sync (populates metadata)
    try:
        result = await mount_svc.sync_mount(
            mount_point=req.mount_point,
            recursive=req.recursive,
            full_sync=req.full_sync,
            context=sync_context,
        )

        resp = SyncResponse(
            mount_point=req.mount_point,
            files_scanned=result.get("files_scanned", 0),
            files_synced=result.get("files_synced", 0),
        )

        if delta_result:
            resp.is_delta = not delta_result.get("full_sync", True)
            resp.delta_added = len(delta_result.get("added", []))
            resp.delta_deleted = len(delta_result.get("deleted", []))
            resp.history_id = delta_result.get("history_id")

        return resp
    except Exception as e:
        return SyncResponse(mount_point=req.mount_point, error=str(e))


# ---------------------------------------------------------------------------
# Skill doc & schema endpoints (Issue #3148)
# ---------------------------------------------------------------------------


@router.get("/skill/{mount_path:path}", response_model=SkillDocResponse)
async def get_skill_doc(
    mount_path: str,
    request: Request,
    _: dict = Depends(require_auth),
) -> SkillDocResponse:
    """Get SKILL.md and schema list for a mounted connector."""
    if not mount_path.startswith("/"):
        mount_path = f"/{mount_path}"

    nx = _get_nx(request)
    mp = mount_path.rstrip("/")

    # Get backend via router
    backend = None
    try:
        route = nx.router.route(f"{mp}/_.yaml")
        if route:
            backend = route.backend
    except Exception:
        pass

    # Generate skill doc from backend (preferred — always fresh)
    content = ""
    if backend and hasattr(backend, "generate_skill_doc"):
        import contextlib

        with contextlib.suppress(Exception):
            content = backend.generate_skill_doc(mp)

    # Fall back to reading from VFS if backend generation failed
    if not content:
        try:
            raw = await nx.sys_read(f"{mp}/.skill/SKILL.md")
            content = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        except Exception:
            pass

    if not content:
        raise HTTPException(status_code=404, detail=f"No skill docs found for {mount_path}")

    # List schemas — from backend first, then VFS
    schemas: list[str] = []
    if backend:
        s = getattr(backend, "SCHEMAS", {})
        t = getattr(backend, "OPERATION_TRAITS", {})
        schemas = list(s.keys()) if s else list(t.keys())
    if not schemas:
        try:
            entries = await nx.sys_readdir(f"{mp}/.skill/schemas")
            schemas = [str(e).replace(".yaml", "") for e in entries if str(e).endswith(".yaml")]
        except Exception:
            pass

    return SkillDocResponse(mount_point=mount_path, content=content, schemas=schemas)


@router.get("/schema/{mount_path:path}/{operation}")
async def get_schema(
    mount_path: str,
    operation: str,
    request: Request,
    _: dict = Depends(require_auth),
) -> SchemaResponse:
    """Get annotated schema for an operation."""
    if not mount_path.startswith("/"):
        mount_path = f"/{mount_path}"

    nx = _get_nx(request)
    mp = mount_path.rstrip("/")

    # Get backend
    backend = None
    try:
        route = nx.router.route(f"{mp}/_.yaml")
        if route:
            backend = route.backend
    except Exception:
        pass

    # Try generating from backend's schema generator
    if backend:
        try:
            generator = getattr(backend, "_get_doc_generator", None)
            if generator:
                doc_gen = generator()
                schemas = getattr(backend, "SCHEMAS", {})
                if operation in schemas:
                    content = doc_gen._generate_annotated_schema(operation, schemas[operation])
                    return SchemaResponse(
                        mount_point=mount_path, operation=operation, content=content
                    )
        except Exception:
            pass

        # For backends with OPERATION_TRAITS but no SCHEMAS (e.g., hand-written docs),
        # extract the operation section from the full skill doc
        traits = getattr(backend, "OPERATION_TRAITS", {})
        if operation in traits:
            try:
                full_doc = backend.generate_skill_doc(mp)
                # Find the operation section in the doc
                op_display = operation.replace("_", " ").title()
                idx = full_doc.lower().find(op_display.lower())
                if idx >= 0:
                    section = full_doc[idx : idx + 500]
                    return SchemaResponse(
                        mount_point=mount_path, operation=operation, content=section
                    )
            except Exception:
                pass

    # Try reading from VFS
    try:
        raw = await nx.sys_read(f"{mp}/.skill/schemas/{operation}.yaml")
        content = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return SchemaResponse(mount_point=mount_path, operation=operation, content=content)
    except Exception:
        pass

    raise HTTPException(status_code=404, detail=f"Schema '{operation}' not found for {mount_path}")


# ---------------------------------------------------------------------------
# Write endpoint (Issue #3148)
# ---------------------------------------------------------------------------


@router.post("/write/{mount_path:path}", response_model=WriteResponse)
async def write_to_connector(
    mount_path: str,
    req: WriteRequest,
    request: Request,
    _: dict = Depends(require_auth),
) -> WriteResponse:
    """Write validated YAML to a connector path."""
    if not mount_path.startswith("/"):
        mount_path = f"/{mount_path}"

    nx = _get_nx(request)
    try:
        import asyncio

        # nx.write may be sync or async — handle both
        write_fn = getattr(nx, "write", None) or getattr(nx, "sys_write", None)
        if write_fn is None:
            return WriteResponse(success=False, error="NexusFS has no write method")

        data = req.yaml_content.encode("utf-8")
        result = write_fn(mount_path, data)
        if asyncio.iscoroutine(result):
            result = await result

        return WriteResponse(
            success=True,
            content_hash=getattr(result, "content_hash", None) if result else None,
        )
    except Exception as e:
        return WriteResponse(success=False, error=str(e))
