"""nexus-fs: Slim filesystem abstraction for cloud storage.

Two lines to mount any combination of S3, GCS, and local storage:

    import nexus.fs

    fs = await nexus.fs.mount("s3://my-bucket", "local://./data")
    readme = fs.read("/s3/my-bucket/README.md")

Sync usage:

    fs = nexus.fs.mount_sync("s3://my-bucket")
    content = fs.read("/s3/my-bucket/file.txt")

All imports are lazy to keep ``import nexus.fs`` under 200ms.
"""

from __future__ import annotations

__version__ = "0.4.8"

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
    "generate_auth_url": ("nexus.fs._oauth_support", "generate_auth_url"),
    "exchange_auth_code": ("nexus.fs._oauth_support", "exchange_auth_code"),
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
    skip_unavailable: bool = False,
    ephemeral: bool = False,
    name: str | None = None,
) -> Any:
    """Mount one or more backends and return a SlimNexusFS facade.

    Args:
        *uris: One or more backend URIs (e.g., "s3://bucket", "local://./data").
        at: Optional mount point override (only valid with a single URI).
        mount_overrides: Optional mapping of URI → custom mount point.
            Allows per-URI mount points when mounting multiple backends.
            Takes precedence over ``at``.
        skip_unavailable: If True, backends that fail to connect (expired
            credentials, missing bucket, network error) are skipped with a
            warning instead of aborting the entire mount.  Useful for CLI
            commands that should not fail because of an unrelated broken mount.
        ephemeral: If True, skip writing to mounts.json entirely.  The mount
            is active for the lifetime of the returned SlimNexusFS object only.
            Use this in tests and one-shot scripts to avoid accumulating stale
            entries.  See also ``nexus.fs.testing.ephemeral_mount``.
        name: Optional human label for the mount (single-URI only).  Stored
            in mounts.json as ``"name"`` and usable with ``nexus-fs mount rm``.

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
            content = fs.read("/s3/my-bucket/file.txt")
    """
    from nexus.fs._uri import derive_mount_point, parse_uri, validate_mount_collision

    if not uris:
        raise ValueError("At least one URI is required")
    if at is not None and len(uris) > 1:
        raise ValueError("'at' override is only valid with a single URI")
    if name is not None and len(uris) > 1:
        raise ValueError("'name' is only valid with a single URI")

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

    # Create all backends with cleanup on partial failure.
    # Store spec alongside each backend so _resolve_entry_type() can use it
    # during metadata registration without re-zipping against resolved_mounts
    # (which would break when backends are skipped via skip_unavailable=True).
    backends: list[tuple[str, Any, Any]] = []  # (mount_point, backend, spec)
    skipped: list[tuple[str, str]] = []  # (uri, error_msg)
    try:
        for (spec, mp), uri in zip(resolved_mounts, uris, strict=True):
            try:
                backend = create_backend(spec)
                backends.append((mp, backend, spec))
            except Exception as exc:
                if skip_unavailable:
                    skipped.append((uri, str(exc)))
                    logger.warning("Skipping unavailable backend %s: %s", uri, exc)
                else:
                    raise
    except Exception:
        # Clean up any already-created backends and the metastore
        for _, be, _ in backends:
            _close_backend(be)
        metastore.close()
        raise

    if not backends:
        metastore.close()
        skipped_summary = "; ".join(f"{u}: {e}" for u, e in skipped)
        raise ValueError(
            f"All mounts failed. Run 'nexus-fs doctor' to diagnose.\n{skipped_summary}"
        )

    # Slim kernel boot — direct construction, no factory dependency.
    # Wrapped in try/except so backends and metastore are cleaned up if
    # PathRouter, NexusFS, or the mount-entry writes fail.
    try:
        from nexus.contracts.constants import ROOT_ZONE_ID
        from nexus.contracts.types import OperationContext
        from nexus.core.config import PermissionConfig
        from nexus.core.nexus_fs import NexusFS

        kernel = NexusFS(
            metadata_store=metastore,
            permissions=PermissionConfig(enforce=False),
            init_cred=OperationContext(
                user_id="local", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True
            ),
        )

        for mp, backend, _ in backends:
            kernel._driver_coordinator.mount(mp, backend)

        # Persist mount entries so playground/fsspec/cp can auto-discover them.
        # Merges with existing entries so repeated `mount` calls accumulate.
        # Only persist URIs whose backends were successfully created.
        # Skip entirely when ephemeral=True — caller explicitly opted out.
        skipped_uris = {u for u, _ in skipped}
        if not ephemeral:
            try:
                from nexus.fs._paths import save_persisted_mounts

                new_entries = [
                    {
                        "uri": uri,
                        "at": overrides.get(uri) or (at if i == 0 else None),
                        "name": name if i == 0 else None,
                    }
                    for i, uri in enumerate(uris)
                    if uri not in skipped_uris
                ]
                save_persisted_mounts(new_entries)
            except OSError as exc:
                logger.warning(
                    "Could not write mounts.json (%s). "
                    "fsspec auto-discovery and playground will not find these mounts.",
                    exc,
                )

        # Create DT_MOUNT or DT_EXTERNAL_STORAGE metadata entries for each mount point.
        # Non-storage connectors (oauth/api backends like gdrive) must be registered as
        # DT_EXTERNAL_STORAGE so the router returns ExternalRouteResult and reads go
        # directly to backend.read_content() instead of through the kernel.
        # Mirrors the logic in nexus.bricks.mount.mount_service (mount_service.py:608).
        for mp, backend, spec in backends:
            metastore.put(_make_mount_entry(mp, backend.name, entry_type=_resolve_entry_type(spec)))
    except Exception:
        for _, be, _ in backends:
            _close_backend(be)
        metastore.close()
        raise

    return SlimNexusFS(kernel)


def mount_sync(
    *uris: str,
    at: str | None = None,
    mount_overrides: dict[str, str] | None = None,
    skip_unavailable: bool = False,
    ephemeral: bool = False,
    name: str | None = None,
) -> Any:
    """Synchronous version of mount().

    Returns a SyncNexusFS wrapper. See mount() for full documentation.

    Examples:
        fs = nexus.fs.mount_sync("s3://my-bucket")
        content = fs.read("/s3/my-bucket/file.txt")
    """
    from nexus.fs._sync import SyncNexusFS, run_sync

    async_fs = run_sync(
        mount(
            *uris,
            at=at,
            mount_overrides=mount_overrides,
            skip_unavailable=skip_unavailable,
            ephemeral=ephemeral,
            name=name,
        )
    )
    return SyncNexusFS(async_fs)


def _close_backend(backend: Any) -> None:
    """Best-effort close on a backend instance."""
    import contextlib

    close = getattr(backend, "close", None)
    if close is not None:
        with contextlib.suppress(Exception):
            close()


def _resolve_entry_type(spec: Any) -> int:
    """Return DT_EXTERNAL_STORAGE for non-storage connectors, DT_MOUNT otherwise.

    Built-in storage schemes (s3, gcs, local) are always DT_MOUNT.
    Connector schemes look up the ConnectorRegistry category — oauth/api/cli
    connectors (e.g. gdrive) get DT_EXTERNAL_STORAGE so the router bypasses
    the kernel and dispatches reads directly to backend.read_content().
    """
    from nexus.contracts.metadata import DT_EXTERNAL_STORAGE, DT_MOUNT

    if spec.scheme in ("s3", "gcs", "local"):
        return DT_MOUNT

    try:
        from nexus.backends.base.registry import ConnectorRegistry

        for candidate in [
            f"{spec.scheme}_{spec.authority}" if spec.authority else None,
            f"{spec.scheme}_connector",
        ]:
            if candidate is None:
                continue
            try:
                info = ConnectorRegistry.get_info(candidate)
                return DT_EXTERNAL_STORAGE if info.category != "storage" else DT_MOUNT
            except KeyError:
                continue
    except Exception:
        pass

    return DT_MOUNT


def _make_mount_entry(path: str, backend_name: str, *, entry_type: int | None = None) -> Any:
    """Create a FileMetadata entry for a mount point.

    Shared by mount() and tests to avoid repeating the 13-field construction.
    entry_type defaults to DT_MOUNT; pass DT_EXTERNAL_STORAGE for non-storage
    connectors (e.g. gdrive) so the router uses the ExternalRouteResult path.
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
        entry_type=entry_type if entry_type is not None else DT_MOUNT,
    )
