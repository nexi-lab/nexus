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

    result = []
    for m in mounts:
        mp = m.get("mount_point", "")
        # Get backend info
        skill_name = None
        operations: list[str] = []
        try:
            nx = _get_nx(request)
            route = nx._router.route(mp)
            if route:
                backend = route.backend
                skill_name = getattr(backend, "SKILL_NAME", None)
                schemas = getattr(backend, "SCHEMAS", {})
                operations = list(schemas.keys()) if schemas else []
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
    _: dict = Depends(require_auth),
) -> SyncResponse:
    """Trigger sync for a mounted connector."""
    mount_svc = _get_mount_service(request)
    try:
        result = await mount_svc.sync_mount(
            mount_point=req.mount_point,
            recursive=req.recursive,
            full_sync=req.full_sync,
        )
        return SyncResponse(
            mount_point=req.mount_point,
            files_scanned=result.get("files_scanned", 0),
            files_synced=result.get("files_synced", 0),
        )
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

    # Read SKILL.md
    skill_md_path = f"{mount_path.rstrip('/')}/.skill/SKILL.md"
    content = ""
    try:
        raw = await nx.sys_read(skill_md_path)
        content = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except Exception:
        # Fall back to generating from backend
        try:
            route = nx._router.route(mount_path)
            if route and hasattr(route.backend, "generate_skill_doc"):
                content = route.backend.generate_skill_doc(mount_path)
        except Exception:
            raise HTTPException(
                status_code=404, detail=f"No skill docs found for {mount_path}"
            ) from None

    # List schemas
    schemas: list[str] = []
    try:
        schemas_dir = f"{mount_path.rstrip('/')}/.skill/schemas"
        entries = await nx.sys_readdir(schemas_dir)
        schemas = [str(e).replace(".yaml", "") for e in entries if str(e).endswith(".yaml")]
    except Exception:
        # Fall back to SCHEMAS from backend
        try:
            route = nx._router.route(mount_path)
            if route:
                schemas = list(getattr(route.backend, "SCHEMAS", {}).keys())
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

    # Try reading from .skill/schemas/
    schema_path = f"{mount_path.rstrip('/')}/.skill/schemas/{operation}.yaml"
    try:
        raw = await nx.sys_read(schema_path)
        content = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return SchemaResponse(mount_point=mount_path, operation=operation, content=content)
    except Exception:
        pass

    # Fall back to generating from backend
    try:
        route = nx._router.route(mount_path)
        if route:
            backend = route.backend
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
        result = nx.write(mount_path, req.yaml_content.encode("utf-8"))
        return WriteResponse(
            success=True,
            content_hash=getattr(result, "content_hash", None),
        )
    except Exception as e:
        return WriteResponse(success=False, error=str(e))
