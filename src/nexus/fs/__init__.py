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

    # Use first backend as root for factory boot (Issue #1801: unified boot path)
    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.types import OperationContext
    from nexus.core.config import PermissionConfig
    from nexus.factory import create_nexus_fs

    _slim_cred = OperationContext(user_id="local", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True)
    _first_mp, _first_backend = backends[0]

    kernel = await create_nexus_fs(
        backend=_first_backend,
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        is_admin=True,
        enabled_bricks=frozenset(),  # SLIM profile: no bricks
        init_cred=_slim_cred,
    )

    # Mount remaining backends (factory already mounted first at "/")
    from datetime import UTC, datetime

    from nexus.contracts.metadata import FileMetadata
    from nexus.core.hash_fast import hash_content

    empty_hash = hash_content(b"")
    now = datetime.now(UTC)

    # Create DT_MOUNT entry for first backend at its derived path
    if _first_mp != "/":
        kernel.router.add_mount(_first_mp, _first_backend)
    metastore.put(
        FileMetadata(
            path=_first_mp,
            backend_name=_first_backend.name,
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
    )

    # Additional mounts
    for mp, backend in backends[1:]:
        kernel.router.add_mount(mp, backend)
        metastore.put(
            FileMetadata(
                path=mp,
                backend_name=backend.name,
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
        )

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
    for name in [connector_name, f"gws_{authority}" if scheme == "gws" else None, fallback_name]:
        if name is None:
            continue
        try:
            connector_cls = ConnectorRegistry.get(name)
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

    # Instantiate the connector. CLIConnectors accept config via kwargs.
    return connector_cls()


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
