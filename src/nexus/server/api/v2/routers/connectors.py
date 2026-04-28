"""Connector management REST API (Issue #2069, #3148, #3182).

Endpoints for connector discovery, mounting, sync, readme docs, and writes.
Used by the TUI Connectors tab and CLI.

Endpoints:
    GET   /api/v2/connectors                       — List registered connectors
    GET   /api/v2/connectors/{name}/capabilities   — Connector capabilities
    GET   /api/v2/connectors/available              — Connectors with auth/mount status
    POST  /api/v2/connectors/mount                  — Mount a connector
    GET   /api/v2/connectors/mounts                 — List mounted connectors
    POST  /api/v2/connectors/sync                   — Trigger sync for a mount
    GET   /api/v2/connectors/{mount_path:path}/skill — Get README.md
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
    auth_source: str | None = None
    mount_path: str | None = None  # None if not mounted
    sync_status: str | None = None  # "synced", "syncing", "error", None


class MountRequest(BaseModel):
    """Request to mount a connector."""

    connector_type: str = Field(..., description="Connector backend type")
    mount_point: str = Field(..., description="VFS mount path (e.g., /mnt/gmail)")
    config: dict[str, Any] = Field(default_factory=dict, description="Backend config")


class MountResponse(BaseModel):
    """Response from mount operation."""

    mounted: bool
    mount_point: str
    error: str | None = None


class WriteRequest(BaseModel):
    """Request to write YAML to a connector path."""

    yaml_content: str = Field(..., description="YAML content to write")


class WriteResponse(BaseModel):
    """Response from write operation."""

    success: bool
    content_id: str | None = None
    error: str | None = None


class ReadmeDocResponse(BaseModel):
    """README.md content for a mount."""

    mount_point: str
    content: str
    schemas: list[str] = Field(default_factory=list)


class SchemaResponse(BaseModel):
    """Annotated schema for an operation."""

    mount_point: str
    operation: str
    content: str


class AuthInitRequest(BaseModel):
    """Request to initiate OAuth for a connector."""

    connector_name: str = Field(..., description="Connector name (e.g., gmail_connector)")
    provider: str | None = Field(None, description="OAuth provider override")


class AuthInitResponse(BaseModel):
    """Response from auth init with URL to open in browser."""

    auth_url: str
    state_token: str
    provider: str
    expires_in: int = 300  # 5 minutes


class AuthStatusRequest(BaseModel):
    """Query params for auth status polling."""

    state_token: str


class AuthStatusResponse(BaseModel):
    """Auth completion status."""

    status: str  # "pending", "completed", "denied", "expired", "error"
    connector_name: str
    message: str | None = None


class MountInfo(BaseModel):
    """Info about a mounted connector."""

    mount_point: str
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
                capabilities=sorted(str(c) for c in info.backend_features),
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
    from nexus.backends import _register_optional_backends
    from nexus.backends.base.registry import ConnectorRegistry

    _register_optional_backends()

    nx = _get_nx(request)
    auth_svc = getattr(request.app.state, "auth_service", None) or nx.service("auth")

    # Build connector_type -> mount_point map by inverting _mount_types
    # (which stores mount_point -> connector_type).
    type_to_mount: dict[str, str] = {ct: mp for mp, ct in _mount_types.items()}

    result = []
    for info in ConnectorRegistry.list_all():
        mount_path = type_to_mount.get(info.name)

        auth_state = {"auth_status": "unknown", "auth_source": None}
        if auth_svc is not None:
            try:
                auth_state = await auth_svc.get_connector_auth_state(info.service_name)
            except Exception:
                logger.debug("Failed to resolve auth state for %s", info.name, exc_info=True)

        result.append(
            AvailableConnector(
                name=info.name,
                description=info.description,
                category=info.category,
                capabilities=sorted(str(c) for c in info.backend_features),
                user_scoped=info.user_scoped,
                auth_status=str(auth_state.get("auth_status", "unknown")),
                auth_source=auth_state.get("auth_source"),
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
        capabilities=sorted(str(c) for c in info.backend_features),
    )


# ---------------------------------------------------------------------------
# Auth endpoints (Issue #3182)
# ---------------------------------------------------------------------------

# Module-level pending auth state (TTL managed by cleanup).
# Maps state_token -> {connector_name, provider, created_at, status}
_pending_auth: dict[str, dict[str, Any]] = {}

# Module-level mount tracking: mount_point -> connector_type.
# Populated by mount/unmount endpoints so /available can show mount status
# without relying on fragile router introspection through CAS wrappers.
_mount_types: dict[str, str] = {}


@router.post("/auth/init", response_model=AuthInitResponse)
async def init_connector_auth(
    req: AuthInitRequest,
    request: Request,
    _: dict = Depends(require_auth),
) -> AuthInitResponse:
    """Initiate OAuth flow for a connector. Returns a URL to open in a browser."""
    import secrets
    import time

    import yaml

    from nexus.backends.base.registry import ConnectorRegistry

    # Validate connector exists
    if not ConnectorRegistry.is_registered(req.connector_name):
        raise HTTPException(status_code=404, detail=f"Connector '{req.connector_name}' not found")

    info = ConnectorRegistry.get_info(req.connector_name)

    # Resolve provider — use explicit override or infer from connector's service_name
    provider = req.provider or info.service_name
    if not provider:
        raise HTTPException(
            status_code=400, detail=f"No OAuth provider for connector '{req.connector_name}'"
        )

    import os

    nx = _get_nx(request)

    # Generate state token
    state_token = secrets.token_urlsafe(32)

    # Build OAuth authorization URL
    # Look up provider config from oauth.yaml
    oauth_config: dict[str, Any] = {}
    try:
        configs_dir = getattr(nx, "configs_dir", None)
        oauth_path = None
        # Search order: explicit env var → nx.configs_dir → source-tree
        # six-hop path → standard docker install path.  The six-hop
        # fallback only works when the package runs from the source tree;
        # when installed under site-packages (docker image) it resolves
        # to a directory that does not exist, so we also check the image
        # mount at /app/configs.
        search_paths: list[str | None] = [
            os.environ.get("NEXUS_OAUTH_CONFIG"),
            os.environ.get("NEXUS_CONFIGS_DIR")
            and os.path.join(os.environ["NEXUS_CONFIGS_DIR"], "oauth.yaml"),
            configs_dir and os.path.join(configs_dir, "oauth.yaml"),
            os.path.abspath(
                os.path.join(os.path.dirname(__file__), "../../../../../../configs/oauth.yaml")
            ),
            "/app/configs/oauth.yaml",
            "/etc/nexus/oauth.yaml",
        ]
        for search_path in search_paths:
            if search_path and os.path.exists(search_path):
                oauth_path = search_path
                break

        if oauth_path:
            with open(oauth_path) as f:
                all_config = yaml.safe_load(f)

            # Try direct provider name, then aliases
            provider_aliases = {
                "gmail": "gmail",
                "gmail_connector": "gmail",
                "calendar_connector": "gcalendar",
                "gcalendar": "gcalendar",
                "gdrive_connector": "google-drive",
                "google-drive": "google-drive",
                "slack_connector": "slack",
                "slack": "slack",
                "x_connector": "x",
                "x": "x",
                "gws_connector": "google-drive",
            }
            lookup = provider_aliases.get(
                provider, provider_aliases.get(req.connector_name, provider)
            )
            providers_raw = all_config.get("providers", [])
            # oauth.yaml declares `providers:` as a list of {name: ..., ...}
            # dicts (see configs/oauth.yaml).  Earlier code treated it as a
            # dict keyed by provider name — which silently failed into a 400
            # because `list.get(...)` raises AttributeError and the outer
            # except swallows it.  Accept either shape.
            if isinstance(providers_raw, list):
                providers_by_name = {p.get("name"): p for p in providers_raw if isinstance(p, dict)}
            elif isinstance(providers_raw, dict):
                providers_by_name = providers_raw
            else:
                providers_by_name = {}
            oauth_config = providers_by_name.get(lookup, providers_by_name.get(provider, {}))
    except Exception:
        logger.debug("Failed to load oauth.yaml", exc_info=True)

    if not oauth_config:
        raise HTTPException(
            status_code=400, detail=f"No OAuth configuration found for provider '{provider}'"
        )

    # Build authorization URL
    scopes = oauth_config.get("scopes", [])
    client_id_env = oauth_config.get("client_id_env", "")
    client_id = os.environ.get(client_id_env, "")
    redirect_uri = all_config.get("redirect_uri", "http://localhost:5173/oauth/callback")

    if not client_id:
        raise HTTPException(
            status_code=500,
            detail=f"OAuth client ID not configured. Set environment variable: {client_id_env}",
        )

    # Determine authorization endpoint based on provider
    provider_class = oauth_config.get("provider_class", "")
    if (
        "google" in provider_class.lower()
        or "gmail" in lookup
        or "gcalendar" in lookup
        or "google" in lookup
    ):
        auth_url = (
            f"https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&response_type=code"
            f"&scope={'+'.join(scopes)}"
            f"&state={state_token}"
            f"&access_type=offline"
            f"&prompt=consent"
        )
    elif "slack" in provider_class.lower() or "slack" in lookup:
        auth_url = (
            f"https://slack.com/oauth/v2/authorize"
            f"?client_id={client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&scope={','.join(scopes)}"
            f"&state={state_token}"
        )
    elif lookup == "x":
        auth_url = (
            f"https://twitter.com/i/oauth2/authorize"
            f"?client_id={client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&response_type=code"
            f"&scope={'+'.join(scopes)}"
            f"&state={state_token}"
            f"&code_challenge=challenge&code_challenge_method=plain"
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported OAuth provider: {provider}")

    # Snapshot the current auth state so we can detect a *change* during polling.
    # Without this, pre-existing auth for the same connector would immediately
    # report "completed" — a correctness bug for concurrent auth attempts.
    auth_svc = getattr(request.app.state, "auth_service", None) or nx.service("auth")
    baseline_status = "unknown"
    if auth_svc is not None:
        try:
            baseline = await auth_svc.get_connector_auth_state(info.service_name)
            baseline_status = str(baseline.get("auth_status", "unknown"))
        except Exception:
            pass

    # Store pending auth state with the baseline snapshot
    _pending_auth[state_token] = {
        "connector_name": req.connector_name,
        "provider": provider,
        "created_at": time.time(),
        "status": "pending",
        "baseline_auth_status": baseline_status,
    }

    # Cleanup expired entries (>5 min old)
    cutoff = time.time() - 300
    expired = [k for k, v in _pending_auth.items() if v["created_at"] < cutoff]
    for k in expired:
        del _pending_auth[k]

    resolved_provider = lookup if "lookup" in dir() else provider
    return AuthInitResponse(
        auth_url=auth_url,
        state_token=state_token,
        provider=resolved_provider,
    )


@router.get("/auth/status", response_model=AuthStatusResponse)
async def get_auth_status(
    state_token: str,
    request: Request,
    _: dict = Depends(require_auth),
) -> AuthStatusResponse:
    """Poll for OAuth completion status."""
    import time

    # Check pending auth state
    pending = _pending_auth.get(state_token)
    if not pending:
        raise HTTPException(status_code=404, detail="Unknown or expired auth state token")

    connector_name = pending["connector_name"]
    created_at = pending["created_at"]

    # Check if expired (5 min TTL)
    if time.time() - created_at > 300:
        del _pending_auth[state_token]
        return AuthStatusResponse(
            status="expired",
            connector_name=connector_name,
            message="Auth session expired. Please try again.",
        )

    # Check if auth was completed by querying the auth service.
    # Only report "completed" when the auth state *changed* from the baseline
    # captured at init time. This prevents pre-existing auth from causing
    # false completion and makes concurrent auth attempts distinguishable.
    baseline_status = pending.get("baseline_auth_status", "unknown")
    nx = _get_nx(request)
    auth_svc = getattr(request.app.state, "auth_service", None) or nx.service("auth")

    if auth_svc is not None:
        try:
            from nexus.backends.base.registry import ConnectorRegistry

            info = ConnectorRegistry.get_info(connector_name)
            auth_state = await auth_svc.get_connector_auth_state(info.service_name)
            status = str(auth_state.get("auth_status", "unknown"))

            if status == "authed" and baseline_status != "authed":
                # Auth state changed to authed — this flow completed.
                # Invalidate ALL pending tokens for the same connector so
                # concurrent auth/init calls don't also claim completion.
                # The losing tokens will get 404 on next poll, which the
                # TUI handles as "expired — retry".
                stale = [
                    k for k, v in _pending_auth.items() if v["connector_name"] == connector_name
                ]
                for k in stale:
                    del _pending_auth[k]
                return AuthStatusResponse(
                    status="completed",
                    connector_name=connector_name,
                    message="Authentication successful.",
                )
            elif status == "expired" and baseline_status != "expired":
                return AuthStatusResponse(
                    status="denied",
                    connector_name=connector_name,
                    message="Authentication was denied or token expired.",
                )
            elif status == "error" and baseline_status != "error":
                return AuthStatusResponse(
                    status="error",
                    connector_name=connector_name,
                    message="Authentication failed. Check provider configuration.",
                )
        except Exception as e:
            logger.debug("Error checking auth status for %s: %s", connector_name, e)

    return AuthStatusResponse(
        status="pending",
        connector_name=connector_name,
        message="Waiting for authentication...",
    )


# ---------------------------------------------------------------------------
# Mount management endpoints (Issue #3148)
# ---------------------------------------------------------------------------


@router.post("/mount", response_model=MountResponse)
async def mount_connector(
    req: MountRequest,
    request: Request,
    auth: dict = Depends(require_auth),
) -> MountResponse:
    """Mount a connector at a VFS path.

    Inherits the API key's zone and permissions. Parent directories
    (e.g., /mnt for /mnt/gmail) are auto-created in the same zone.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.types import OperationContext

    # Build context from the authenticated user's identity
    mount_context = OperationContext(
        user_id=auth.get("subject_id", "system"),
        groups=[],
        is_admin=auth.get("is_admin", True),
        is_system=True,
        zone_id=auth.get("zone_id") or ROOT_ZONE_ID,
    )

    mount_svc = _get_mount_service(request)
    nx = _get_nx(request)

    # Write readme docs BEFORE mounting — after mount, the path routes to the
    # connector backend instead of Raft. Writing first puts README.md + schemas
    # in the Raft metastore where the TUI file explorer can browse them.
    try:
        from nexus.backends import BackendFactory

        temp_backend = BackendFactory.create(req.connector_type, req.config or {})
        if hasattr(temp_backend, "generate_readme"):
            mp = req.mount_point.rstrip("/")
            # Extract connector name from mount path (e.g., /mnt/gmail → gmail)
            connector_name = mp.rsplit("/", 1)[-1]
            # Create /skills/ directory entry via metadata_put so it shows
            # in root-level readdir. sys_write creates files but the HTTP
            # readdir doesn't synthesize parent directories from child paths.
            try:
                from datetime import UTC, datetime

                from nexus.contracts.metadata import FileMetadata

                meta_store = nx.metadata
                if meta_store:
                    for dir_path in [
                        "/skills",
                        f"/skills/{connector_name}",
                        f"/skills/{connector_name}/schemas",
                    ]:
                        try:
                            if not meta_store.get(dir_path):
                                meta_store.put(
                                    FileMetadata(
                                        path=dir_path,
                                        size=0,
                                        content_id=None,
                                        created_at=datetime.now(UTC),
                                        modified_at=datetime.now(UTC),
                                        version=1,
                                        zone_id=mount_context.zone_id,
                                    )
                                )
                        except Exception:
                            pass
            except Exception:
                pass

            # Write readme docs OUTSIDE the mount path so they're not shadowed
            # by the connector backend. /skills/{name}/ stays in the Raft
            # metastore and is always readable by agents and the TUI.
            readme_base = f"/skills/{connector_name}"
            readme_content = temp_backend.generate_readme(mp)
            if readme_content:
                nx.write(
                    f"{readme_base}/README.md",
                    readme_content.encode("utf-8"),
                    context=mount_context,
                )
                # Write individual schema files
                schemas = getattr(temp_backend, "SCHEMAS", {})
                if schemas and hasattr(temp_backend, "get_doc_generator"):
                    doc_gen = temp_backend.get_doc_generator()
                    for op_name, schema_cls in schemas.items():
                        try:
                            schema_yaml = doc_gen.generate_schema_yaml(op_name, schema_cls)
                            nx.write(
                                f"{readme_base}/schemas/{op_name}.yaml",
                                schema_yaml.encode("utf-8"),
                                context=mount_context,
                            )
                        except Exception:
                            pass

                # Index readme doc into semantic search
                search_svc = getattr(mount_svc, "_search_service", None)
                if search_svc:
                    search_daemon = getattr(search_svc, "_search_daemon", None)
                    if search_daemon:
                        import contextlib

                        with contextlib.suppress(Exception):
                            await search_daemon.index_documents(
                                [
                                    {
                                        "id": f"{readme_base}/README.md",
                                        "text": readme_content,
                                        "path": f"{readme_base}/README.md",
                                    }
                                ],
                                zone_id=mount_context.zone_id or "default",
                            )
    except Exception:
        pass  # Best effort — mount works without skill docs

    try:
        result = await mount_svc.add_mount(
            mount_point=req.mount_point,
            backend_type=req.connector_type,
            backend_config=req.config,
            context=mount_context,
        )
        _mount_types[req.mount_point] = req.connector_type
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
        # All backends are Rust-native — no Python backend metadata
        # for skill_name/operations.  Use mount-level metadata only.
        result.append(
            MountInfo(
                mount_point=mp,
                skill_name=None,
                operations=[],
                sync_status=m.get("sync_status"),
                last_sync=m.get("last_sync"),
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
        _mount_types.pop(req.mount_point, None)
        return MountResponse(mounted=False, mount_point=req.mount_point)
    except Exception as e:
        return MountResponse(mounted=False, mount_point=req.mount_point, error=str(e))


# ---------------------------------------------------------------------------
# Skill doc & schema endpoints (Issue #3148)
# ---------------------------------------------------------------------------


@router.get("/skill/{mount_path:path}", response_model=ReadmeDocResponse)
async def get_readme_doc(
    mount_path: str,
    request: Request,
    _: dict = Depends(require_auth),
) -> ReadmeDocResponse:
    """Get README.md and schema list for a mounted connector."""
    if not mount_path.startswith("/"):
        mount_path = f"/{mount_path}"

    nx = _get_nx(request)
    mp = mount_path.rstrip("/")

    # Skill backend no longer stored in DLC — always None
    backend = None

    # Generate skill doc from backend (preferred — always fresh)
    content = ""
    if backend and hasattr(backend, "generate_readme"):
        import contextlib

        with contextlib.suppress(Exception):
            content = backend.generate_readme(mp)

    # Fall back to reading from VFS if backend generation failed
    if not content:
        try:
            raw = nx.sys_read(f"{mp}/.readme/README.md")
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
            entries = nx.sys_readdir(f"{mp}/.readme/schemas")
            schemas = [str(e).replace(".yaml", "") for e in entries if str(e).endswith(".yaml")]
        except Exception:
            pass

    return ReadmeDocResponse(mount_point=mount_path, content=content, schemas=schemas)


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

    # Skill backend no longer stored in DLC — always None
    backend = None

    # Try generating from backend's schema generator
    if backend:
        try:
            generator = getattr(backend, "get_doc_generator", None)
            if generator:
                doc_gen = generator()
                schemas = getattr(backend, "SCHEMAS", {})
                if operation in schemas:
                    content = doc_gen.generate_schema_yaml(operation, schemas[operation])
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
                full_doc = backend.generate_readme(mp)
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
        raw = nx.sys_read(f"{mp}/.readme/schemas/{operation}.yaml")
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
    auth: dict = Depends(require_auth),
) -> WriteResponse:
    """Write validated YAML to a connector path."""
    if not mount_path.startswith("/"):
        mount_path = f"/{mount_path}"

    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.types import OperationContext

    nx = _get_nx(request)

    # Preflight: discover backend type via sys_stat (§12d).
    _route_backend_name = None
    _backend_path = None
    _py_kernel = getattr(nx, "_kernel", None)
    if _py_kernel is not None:
        try:
            _stat_d = _py_kernel.sys_stat(mount_path, "root")
            if _stat_d and isinstance(_stat_d, dict):
                _route_backend_name = _stat_d.get("backend_name")
            # Derive backend_path from path minus mount point (first 2 segments)
            _parts = mount_path.strip("/").split("/")
            _mp = "/" + "/".join(_parts[:2]) if len(_parts) >= 2 else "/" + _parts[0]
            _backend_path = mount_path[len(_mp) :].lstrip("/")
        except Exception:
            pass

    write_context = OperationContext(
        user_id=auth.get("subject_id", "system"),
        groups=[],
        is_admin=auth.get("is_admin", False),
        zone_id=auth.get("zone_id") or ROOT_ZONE_ID,
        backend_path=_backend_path,
        virtual_path=mount_path,
    )

    try:
        data = req.yaml_content.encode("utf-8")

        # CLI-backed connectors dispatch YAML operations (send_email, create_draft,
        # etc.) via write_content() — they must NOT go through nx.write() which
        # stores to CAS. Gate on the "cli_backed" capability to avoid bypassing
        # the kernel write path for ordinary path-addressed backends.

        # All backends are Rust-native now — check if this is a CLI connector
        # by examining the backend_name pattern.
        is_cli_connector = _route_backend_name is not None and (
            "cli" in _route_backend_name or "gws" in _route_backend_name
        )

        if is_cli_connector and _backend_path:
            # Write permissions are enforced by the pre-write intercept hooks below.
            from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

            # Load existing metadata so permission hooks can distinguish
            # overwrite (check WRITE on file) vs create (check WRITE on parent).
            _old_meta = nx.metadata.get(mount_path)

            nx.intercept_pre_write(
                _WHC(path=mount_path, content=data, context=write_context, old_metadata=_old_meta)
            )

            # Write via kernel — Rust backend handles CLI dispatch.
            result = nx.write(mount_path, data, context=write_context)
        else:
            result = nx.write(mount_path, data, context=write_context)

        return WriteResponse(
            success=True,
            content_id=getattr(result, "content_id", None) if result else None,
        )
    except Exception as e:
        return WriteResponse(success=False, error=str(e))
