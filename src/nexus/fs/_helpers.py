"""Mount lifecycle and discovery clients for kernel-direct nexus-fs callers."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any, cast

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


LOCAL_CONTEXT = OperationContext(
    user_id="local",
    groups=[],
    zone_id=ROOT_ZONE_ID,
    is_admin=True,
)


def list_mounts(kernel: NexusFS) -> list[str]:
    """Return the sorted list of mount-point paths registered in *kernel*."""
    py_kernel = getattr(kernel, "_kernel", None)
    if py_kernel is None:
        return []
    from nexus.core.path_utils import extract_zone_id

    return sorted(extract_zone_id(c)[1] for c in py_kernel.get_mount_points())


def unmount(kernel: NexusFS, mount_point: str) -> None:
    """Remove *mount_point* and clean up runtime + persisted state.

    The runtime tear-down (metastore delete + dcache evict + routing
    remove) is a single ``kernel.sys_unlink`` call — sys_unlink delegates
    to ``dlc::unmount`` when the entry is a DT_MOUNT. Only the
    ``mounts.json`` scrub stays Python-side because the kernel doesn't
    own that config file.
    """
    from nexus.core.path_utils import validate_path

    normalized = validate_path(mount_point, allow_root=False)
    meta = kernel.metadata.get(normalized)
    if meta is None or not meta.is_mount:
        raise ValueError(f"'{normalized}' is not a mount point")

    kernel.sys_unlink(normalized, context=LOCAL_CONTEXT)
    mounted = getattr(kernel, "_mounted_backend_instances", None)
    if isinstance(mounted, dict):
        mounted.pop(normalized, None)

    with contextlib.suppress(OSError):
        from nexus.fs._paths import load_persisted_mounts, save_persisted_mounts
        from nexus.fs._uri import derive_mount_point, parse_uri

        existing = load_persisted_mounts()
        filtered = []
        for entry in existing:
            try:
                spec = parse_uri(entry["uri"])
                mp = derive_mount_point(spec, at=entry.get("at"))
                if mp != normalized:
                    filtered.append(entry)
            except Exception:
                filtered.append(entry)
        if len(filtered) != len(existing):
            save_persisted_mounts(filtered, merge=False)


def close(kernel: NexusFS) -> None:
    """Close the kernel and its metastore. Safe to call repeatedly."""
    try:
        _close = getattr(kernel, "close", None)
        if _close is not None:
            _close()
    finally:
        with contextlib.suppress(Exception):
            kernel.metadata.close()


def grep(
    kernel: NexusFS,
    pattern: str,
    path: str = "/",
    *,
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict[str, Any]]:
    """Search current bytes through the owning Kernel's discovery RPC."""
    response = kernel._kernel.call_rpc(
        "grep",
        {"pattern": pattern, "path": path, "ignore_case": ignore_case, "max_results": max_results},
    )
    return cast(list[dict[str, Any]], response["results"])


def glob(kernel: NexusFS, pattern: str, path: str = "/") -> list[str]:
    """Find files through the owning Kernel's discovery RPC."""
    search = kernel.service("search")
    if search is None:
        raise RuntimeError("Search service is unavailable")
    return cast(list[str], search.glob(pattern, path))
