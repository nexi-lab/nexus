"""nexus-fs: Slim filesystem abstraction for cloud storage.

Two lines to mount any combination of S3, GCS, and local storage:

    import nexus.fs

    fs = await nexus.fs.mount("s3://my-bucket", "local://./data")
    readme = await fs.read("/s3/my-bucket/README.md")

Sync usage:

    fs = nexus.fs.mount_sync("s3://my-bucket")
    content = fs.read("/s3/my-bucket/file.txt")

All imports are lazy to keep ``import nexus.fs`` under 200ms.
"""

from __future__ import annotations

__version__ = "0.1.0"

# =============================================================================
# LAZY IMPORTS — everything is deferred for <200ms import time
# =============================================================================
import inspect
import os
import shutil
import subprocess
from typing import Any

_lazy_cache: dict[str, Any] = {}

_LAZY_IMPORTS = {
    "SlimNexusFS": ("nexus.fs._facade", "SlimNexusFS"),
    "SyncNexusFS": ("nexus.fs._sync", "SyncNexusFS"),
    "NexusFileSystem": ("nexus.fs._fsspec", "NexusFileSystem"),
    "parse_uri": ("nexus.fs._uri", "parse_uri"),
    "MountSpec": ("nexus.fs._uri", "MountSpec"),
}


def __getattr__(name: str) -> Any:
    if name in _lazy_cache:
        return _lazy_cache[name]
    if name in _LAZY_IMPORTS:
        import importlib

        mod_path, attr_name = _LAZY_IMPORTS[name]
        mod = importlib.import_module(mod_path)
        val = getattr(mod, attr_name)
        _lazy_cache[name] = val
        return val
    raise AttributeError(f"module 'nexus.fs' has no attribute {name!r}")


async def mount(*uris: str, at: str | None = None) -> Any:
    """Mount one or more backends and return a SlimNexusFS facade.

    Args:
        *uris: One or more backend URIs (e.g., "s3://bucket", "local://./data").
        at: Optional mount point override (only valid with a single URI).

    Returns:
        SlimNexusFS facade with all backends mounted.

    Raises:
        NexusURIError: If a URI is invalid.
        CloudCredentialError: If cloud credentials are missing.
        BackendNotFoundError: If a cloud resource doesn't exist.

    Examples:
        # Single backend
        fs = await nexus.fs.mount("s3://my-bucket")

        # Multiple backends
        fs = await nexus.fs.mount("s3://my-bucket", "local://./data")

        # Custom mount point
        fs = await nexus.fs.mount("s3://my-bucket", at="/data")
    """
    from nexus.fs._uri import derive_mount_point, parse_uri, validate_mount_collision

    if not uris:
        raise ValueError("At least one URI is required")
    if at is not None and len(uris) > 1:
        raise ValueError("'at' override is only valid with a single URI")

    # Parse all URIs first (fail fast on invalid input)
    specs = [parse_uri(uri) for uri in uris]

    # Check for collisions
    mount_points: set[str] = set()
    resolved_mounts = []
    for i, spec in enumerate(specs):
        mp = derive_mount_point(spec, at=at if i == 0 else None)
        validate_mount_collision(mp, mount_points)
        mount_points.add(mp)
        resolved_mounts.append((spec, mp))

    # Create metastore + backends
    import os
    import tempfile

    from nexus.fs._facade import SlimNexusFS
    from nexus.fs._sqlite_meta import SQLiteMetastore

    state_dir = os.environ.get("NEXUS_FS_STATE_DIR") or os.path.join(
        tempfile.gettempdir(), "nexus-fs"
    )
    os.makedirs(state_dir, exist_ok=True)
    db_path = os.path.join(state_dir, "metadata.db")

    metastore = SQLiteMetastore(db_path)

    # Create all backends
    backends = [(mp, _create_backend(spec)) for spec, mp in resolved_mounts]

    # Slim kernel boot — direct construction, no factory dependency.
    # Factory pulls in nexus.bricks/cache/system_services which are excluded
    # from the slim wheel. Issue #3326.
    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.types import OperationContext
    from nexus.core.config import BrickServices, KernelServices, PermissionConfig
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import PathRouter

    router = PathRouter(metastore)
    _first_mp, _first_backend = backends[0]

    # Mount all backends on the router
    for mp, backend in backends:
        router.add_mount(mp, backend)

    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        kernel_services=KernelServices(router=router),
        brick_services=BrickServices(),
        init_cred=OperationContext(user_id="local", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True),
    )

    # Persist mount URIs so playground can auto-discover them later
    import json

    mounts_file = os.path.join(state_dir, "mounts.json")
    try:
        with open(mounts_file, "w") as f:
            json.dump(list(uris), f)
    except OSError:
        pass  # Best-effort; don't fail mount() over this

    # Create DT_MOUNT metadata entries for each mount point
    for mp, backend in backends:
        metastore.put(_make_mount_entry(mp, backend.name))

    return SlimNexusFS(kernel)


def mount_sync(*uris: str, at: str | None = None) -> Any:
    """Synchronous version of mount().

    Returns a SyncNexusFS wrapper. See mount() for full documentation.

    Examples:
        fs = nexus.fs.mount_sync("s3://my-bucket")
        content = fs.read("/s3/my-bucket/file.txt")
    """
    from nexus.fs._sync import SyncNexusFS, run_sync

    async_fs = run_sync(mount(*uris, at=at))
    return SyncNexusFS(async_fs)


def _create_backend(spec: Any) -> Any:
    """Create a storage backend from a parsed MountSpec.

    Discovers credentials automatically and instantiates the
    appropriate backend class.
    """
    from nexus.fs._credentials import discover_credentials

    # Discover credentials (raises CloudCredentialError if missing)
    discover_credentials(spec.scheme)

    if spec.scheme == "s3":
        try:
            from nexus.backends.storage.path_s3 import PathS3Backend
        except ImportError:
            raise ImportError(
                "boto3 is required for S3 backends. Install with: pip install nexus-fs[s3]"
            ) from None
        return PathS3Backend(
            bucket_name=spec.authority,
            prefix=spec.path.lstrip("/") if spec.path else "",
        )

    elif spec.scheme == "gcs":
        try:
            from nexus.backends.storage.cas_gcs import CASGCSBackend
        except ImportError:
            raise ImportError(
                "google-cloud-storage is required for GCS backends. "
                "Install with: pip install nexus-fs[gcs]"
            ) from None
        # GCS: gcs://project/bucket → authority=project, path=/bucket
        bucket = spec.path.strip("/").split("/")[0] if spec.path else spec.authority
        return CASGCSBackend(bucket_name=bucket, project_id=spec.authority)

    elif spec.scheme == "local":
        from pathlib import Path as _Path

        from nexus.backends.storage.cas_local import CASLocalBackend

        root = _Path(spec.authority + (spec.path or "")).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return CASLocalBackend(root_path=root)

    else:
        # Fall through to the connector registry for any other scheme.
        # Connectors register themselves via @register_connector at import
        # time. We try to discover connector modules for the requested
        # scheme, then look up the registry.
        return _create_connector_backend(spec)


def _create_connector_backend(spec: Any) -> Any:
    """Create a backend from the connector registry.

    Attempts to import connector modules for the scheme and look up a
    matching connector in the ConnectorRegistry. The lookup convention:
    ``{scheme}_{authority}`` first, then ``{scheme}_connector`` as fallback.

    This is lazy — connector modules are only imported when the scheme is
    actually requested, so unused connectors add zero startup cost.
    """
    scheme = spec.scheme
    authority = spec.authority

    # Lazily import connector modules for this scheme.
    # Convention: nexus.backends.connectors.<scheme>/
    _discover_connector_module(scheme)

    from nexus.backends.base.registry import ConnectorRegistry

    # Try specific connector first: gws_sheets, gws_docs, etc.
    connector_name = f"{scheme}_{authority}" if authority else scheme
    # Also try gws_connector, gdrive_connector as fallback
    fallback_name = f"{scheme}_connector"

    connector_cls = None
    selected_name: str | None = None
    for candidate in [
        connector_name,
        f"gws_{authority}" if scheme == "gws" else None,
        fallback_name,
    ]:
        if candidate is None:
            continue
        try:
            connector_cls = ConnectorRegistry.get(candidate)
            selected_name = candidate
            break
        except KeyError:
            continue

    if connector_cls is None:
        from nexus.contracts.exceptions import NexusURIError

        available = ConnectorRegistry.list_available()
        raise NexusURIError(
            spec.uri,
            f"No backend or connector found for scheme '{scheme}://'. "
            f"Built-in: s3://, gcs://, local://. "
            f"Registered connectors: {', '.join(available) if available else 'none'}",
        )

    info = ConnectorRegistry.get_info(selected_name) if selected_name is not None else None
    return _instantiate_connector_backend(connector_cls, info=info, scheme=scheme)


def _default_token_manager_db() -> str:
    """Return the default TokenManager database path/URL for slim fs mounts."""
    from nexus.lib.env import get_database_url

    db_url = get_database_url()
    if db_url:
        return db_url

    home = os.path.expanduser("~")
    db_path = os.path.join(home, ".nexus", "nexus.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return db_path


def _infer_connector_user_email(
    *,
    scheme: str,
    info: Any | None,
) -> str | None:
    """Best-effort user identity for OAuth-backed slim connector mounts.

    Priority:
    1. ``NEXUS_FS_USER_EMAIL`` explicit override
    2. the only stored OAuth credential email for the service's provider(s)
    """
    explicit = os.getenv("NEXUS_FS_USER_EMAIL")
    if explicit:
        return explicit

    service_name = getattr(info, "service_name", None) or scheme
    try:
        from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService
        from nexus.bricks.auth.unified_service import _OAUTH_PROVIDER_ALIASES
        from nexus.cli.commands.oauth import get_token_manager
    except Exception:
        return None

    providers = _OAUTH_PROVIDER_ALIASES.get(service_name)
    if not providers:
        return None

    oauth_service = OAuthCredentialService(token_manager=get_token_manager())
    try:
        import asyncio

        creds = asyncio.run(oauth_service.list_credentials())
    except Exception:
        return None

    emails = sorted(
        {
            str(cred.get("user_email"))
            for cred in creds
            if cred.get("provider") in providers and cred.get("user_email")
        }
    )
    if len(emails) == 1:
        return emails[0]
    if "google" in providers:
        return _infer_google_workspace_cli_email()
    return None


def _infer_google_workspace_cli_email() -> str | None:
    """Best-effort Google account detection from the local gws CLI auth state."""
    if shutil.which("gws") is None:
        return None

    try:
        result = subprocess.run(
            [
                "gws",
                "gmail",
                "users",
                "getProfile",
                "--params",
                '{"userId":"me"}',
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    stdout = result.stdout.strip()
    if not stdout:
        return None

    try:
        import json

        start = stdout.find("{")
        payload = stdout[start:] if start >= 0 else stdout
        data = json.loads(payload)
    except Exception:
        return None

    email = str(data.get("emailAddress") or "").strip()
    return email or None


def _instantiate_connector_backend(connector_cls: Any, *, info: Any | None, scheme: str) -> Any:
    """Instantiate connector with the same auth defaults the mount service injects."""
    init_sig = inspect.signature(connector_cls.__init__)
    params = init_sig.parameters
    kwargs: dict[str, Any] = {}

    connection_args = getattr(info, "connection_args", {}) if info is not None else {}
    if "token_manager_db" in params or "token_manager_db" in connection_args:
        kwargs["token_manager_db"] = _default_token_manager_db()

    if "user_email" in params or "user_email" in connection_args:
        user_email = _infer_connector_user_email(scheme=scheme, info=info)
        if user_email:
            kwargs["user_email"] = user_email

    return connector_cls(**kwargs)


def _discover_connector_module(scheme: str) -> None:
    """Try to import the connector module for a given scheme.

    Connector modules register themselves via @register_connector when
    imported. This is a no-op if the module doesn't exist or has already
    been imported.
    """
    import importlib

    # Map scheme to module path. Convention:
    #   gws    -> nexus.backends.connectors.gws.connector
    #   gdrive -> nexus.backends.connectors.gdrive.connector
    #   github -> nexus.backends.connectors.github.connector
    module_paths = [
        f"nexus.backends.connectors.{scheme}.connector",
        f"nexus.backends.connectors.{scheme}",
    ]
    for mod_path in module_paths:
        try:
            importlib.import_module(mod_path)
            return
        except ImportError:
            continue


def _make_mount_entry(path: str, backend_name: str) -> Any:
    """Create a DT_MOUNT FileMetadata entry for a mount point.

    Shared by mount() and tests to avoid repeating the 13-field construction.
    """
    from datetime import UTC, datetime

    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.metadata import FileMetadata
    from nexus.core.hash_fast import hash_content

    empty_hash = hash_content(b"")
    now = datetime.now(UTC)
    return FileMetadata(
        path=path,
        backend_name=backend_name,
        physical_path=empty_hash,
        size=0,
        etag=empty_hash,
        mime_type="inode/directory",
        created_at=now,
        modified_at=now,
        version=1,
        zone_id=ROOT_ZONE_ID,
        entry_type=2,  # DT_MOUNT
    )
