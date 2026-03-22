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

    # Create the kernel infrastructure
    import os

    # SQLite metastore in a temp or user-local directory
    import tempfile

    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import PathRouter
    from nexus.fs._facade import SlimNexusFS
    from nexus.fs._sqlite_meta import SQLiteMetastore

    state_dir = os.environ.get("NEXUS_FS_STATE_DIR") or os.path.join(
        tempfile.gettempdir(), "nexus-fs"
    )
    os.makedirs(state_dir, exist_ok=True)
    db_path = os.path.join(state_dir, "metadata.db")

    metastore = SQLiteMetastore(db_path)
    router = PathRouter(metastore)

    # Mount each backend and create DT_MOUNT entries in the metastore
    from datetime import UTC, datetime

    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.metadata import FileMetadata
    from nexus.contracts.types import OperationContext
    from nexus.core.hash_fast import hash_content

    empty_hash = hash_content(b"")
    now = datetime.now(UTC)

    for spec, mp in resolved_mounts:
        backend = _create_backend(spec)
        router.add_mount(mp, backend)
        # Create DT_MOUNT entry so stat(mount_point) works
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

    # Build kernel (minimal — no factory, no bricks)
    from nexus.core.config import BrickServices, KernelServices, PermissionConfig

    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        kernel_services=KernelServices(router=router),
        brick_services=BrickServices(),
    )

    ctx = OperationContext(user_id="local", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True)
    kernel._default_context = ctx

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
        from nexus.contracts.exceptions import NexusURIError

        raise NexusURIError(
            spec.uri,
            f"No backend available for scheme '{spec.scheme}://'. "
            f"Supported: s3://, gcs://, local://",
        )
