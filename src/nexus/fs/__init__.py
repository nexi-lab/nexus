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
import importlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

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
        mod_path, attr_name = _LAZY_IMPORTS[name]
        mod = importlib.import_module(mod_path)
        val = getattr(mod, attr_name)
        _lazy_cache[name] = val
        return val
    raise AttributeError(f"module 'nexus.fs' has no attribute {name!r}")


async def mount(
    *uris: str,
    at: str | None = None,
    mount_overrides: dict[str, str] | None = None,
) -> Any:
    """Mount one or more backends and return a SlimNexusFS facade.

    Args:
        *uris: One or more backend URIs (e.g., "s3://bucket", "local://./data").
        at: Optional mount point override (only valid with a single URI).
        mount_overrides: Optional mapping of URI → custom mount point.
            Allows per-URI mount points when mounting multiple backends.
            Takes precedence over ``at``.

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

        # Per-URI mount points
        fs = await nexus.fs.mount(
            "s3://my-bucket", "local://./data",
            mount_overrides={"s3://my-bucket": "/data"},
        )

        # Context manager (recommended for resource cleanup)
        async with await nexus.fs.mount("s3://my-bucket") as fs:
            content = await fs.read("/s3/my-bucket/file.txt")
    """
    from nexus.fs._uri import derive_mount_point, parse_uri, validate_mount_collision

    if not uris:
        raise ValueError("At least one URI is required")
    if at is not None and len(uris) > 1:
        raise ValueError("'at' override is only valid with a single URI")

    overrides = mount_overrides or {}

    # Parse all URIs first (fail fast on invalid input)
    specs = [parse_uri(uri) for uri in uris]

    # Check for collisions
    mount_points: set[str] = set()
    resolved_mounts = []
    for i, spec in enumerate(specs):
        # Per-URI override from mount_overrides takes precedence,
        # then at= for single-URI case, then default derivation.
        uri_at = overrides.get(spec.uri) or (at if i == 0 else None)
        mp = derive_mount_point(spec, at=uri_at)
        validate_mount_collision(mp, mount_points)
        mount_points.add(mp)
        resolved_mounts.append((spec, mp))

    # Create metastore
    from nexus.fs._backend_factory import create_backend
    from nexus.fs._facade import SlimNexusFS
    from nexus.fs._paths import metadata_db
    from nexus.fs._sqlite_meta import SQLiteMetastore

    metastore = SQLiteMetastore(str(metadata_db()))

    # Create all backends with cleanup on partial failure
    backends: list[tuple[str, Any]] = []
    try:
        for spec, mp in resolved_mounts:
            backend = create_backend(spec, metastore=metastore)
            backends.append((mp, backend))
    except Exception:
        # Clean up any already-created backends and the metastore
        for _, be in backends:
            _close_backend(be)
        metastore.close()
        raise

    # Slim kernel boot — direct construction, no factory dependency.
    # Wrapped in try/except so backends and metastore are cleaned up if
    # PathRouter, NexusFS, or the mount-entry writes fail.
    try:
        from nexus.contracts.constants import ROOT_ZONE_ID
        from nexus.contracts.types import OperationContext
        from nexus.core.config import PermissionConfig
        from nexus.core.nexus_fs import NexusFS
        from nexus.core.router import PathRouter

        router = PathRouter(metastore)

        for mp, backend in backends:
            router.add_mount(mp, backend)

        kernel = NexusFS(
            metadata_store=metastore,
            permissions=PermissionConfig(enforce=False),
            router=router,
            init_cred=OperationContext(
                user_id="local", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True
            ),
        )

        # Persist mount entries so playground/fsspec/cp can auto-discover them.
        # Merges with existing entries so repeated `mount` calls accumulate.
        try:
            from nexus.fs._paths import save_persisted_mounts

            new_entries = [
                {"uri": uri, "at": overrides.get(uri) or (at if i == 0 else None)}
                for i, uri in enumerate(uris)
            ]
            save_persisted_mounts(new_entries)
        except OSError as exc:
            logger.warning(
                "Could not write mounts.json (%s). "
                "fsspec auto-discovery and playground will not find these mounts.",
                exc,
            )

        # Create DT_MOUNT metadata entries for each mount point
        for mp, backend in backends:
            metastore.put(_make_mount_entry(mp, backend.name))
    except Exception:
        for _, be in backends:
            _close_backend(be)
        metastore.close()
        raise

    return SlimNexusFS(kernel)


def mount_sync(
    *uris: str,
    at: str | None = None,
    mount_overrides: dict[str, str] | None = None,
) -> Any:
    """Synchronous version of mount().

    Returns a SyncNexusFS wrapper. See mount() for full documentation.

    Examples:
        fs = nexus.fs.mount_sync("s3://my-bucket")
        content = fs.read("/s3/my-bucket/file.txt")
    """
    from nexus.fs._sync import SyncNexusFS, run_sync

    async_fs = run_sync(mount(*uris, at=at, mount_overrides=mount_overrides))
    return SyncNexusFS(async_fs)


def _close_backend(backend: Any) -> None:
    """Best-effort close on a backend instance."""
    import contextlib

    close = getattr(backend, "close", None)
    if close is not None:
        with contextlib.suppress(Exception):
            close()


def _make_mount_entry(path: str, backend_name: str) -> Any:
    """Create a DT_MOUNT FileMetadata entry for a mount point.

    Shared by mount() and tests to avoid repeating the 13-field construction.
    """
    from datetime import UTC, datetime

    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.metadata import DT_MOUNT, FileMetadata
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
        entry_type=DT_MOUNT,
    )
