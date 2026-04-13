"""Shared context, data types, and helpers for FUSE operation handlers.

This module contains:
- FUSESharedContext: dataclass bundling all shared state for handlers
- MetadataObj: immutable metadata container
- fuse_operation(): decorator for FUSE error handling
- Shared helper functions (standalone, taking ctx: FUSESharedContext)
"""

import errno
import functools
import logging
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn, cast

from cachetools import TTLCache as _TTLCache
from fuse import FuseOSError

from nexus.contracts.exceptions import (
    NexusFileNotFoundError,
    NexusPermissionError,
    RemoteConnectionError,
    RemoteFilesystemError,
    RemoteTimeoutError,
)
from nexus.fuse.lease_coordinator import FUSELeaseCoordinator
from nexus.lib.virtual_views import parse_virtual_path

if TYPE_CHECKING:
    from nexus.bricks.rebac.namespace_manager import NamespaceManager
    from nexus.contracts.types import OperationContext
    from nexus.core.nexus_fs import NexusFS
    from nexus.fuse.mount import MountMode
    from nexus.fuse.ops._events import FUSEEventDispatcher

# Import readahead for sequential read optimization (Issue #1073)
try:
    from nexus.fuse.readahead import ReadaheadManager

    HAS_READAHEAD = True
except ImportError:
    HAS_READAHEAD = False
    ReadaheadManager = None  # type: ignore[misc,assignment]

# Import LocalDiskCache for L2 caching (Issue #1072)
try:
    from nexus.storage.local_disk_cache import LocalDiskCache

    HAS_LOCAL_DISK_CACHE = True
except ImportError:
    HAS_LOCAL_DISK_CACHE = False
    LocalDiskCache = None  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)

# ============================================================
# Data types
# ============================================================


@dataclass(frozen=True)
class MetadataObj:
    """Immutable metadata container for FUSE attribute responses (C2-B).

    Converts a raw metadata dict (from NexusFS.get_metadata() in REMOTE profile) into
    a typed, immutable object with named attributes.
    """

    path: str | None = None
    size: int | None = None
    owner: str | None = None
    group: str | None = None
    mode: int | None = None
    is_directory: bool | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MetadataObj":
        return cls(
            path=d.get("path"),
            size=d.get("size"),
            owner=d.get("owner"),
            group=d.get("group"),
            mode=d.get("mode"),
            is_directory=d.get("is_directory"),
        )


# ============================================================
# Shared context
# ============================================================


@dataclass
class FUSESharedContext:
    """Bundles all shared state that operation handlers need.

    This is a pure-data container — no methods beyond __init__.
    Handlers receive this in their constructor and pass it to
    module-level helper functions.
    """

    nexus_fs: "NexusFS"
    mode: "MountMode"
    context: "OperationContext | None"
    namespace_manager: "NamespaceManager | None"
    cache: FUSELeaseCoordinator
    local_disk_cache: Any | None  # LocalDiskCache | None
    readahead: Any | None  # ReadaheadManager | None
    rust_client: Any | None
    use_rust: bool
    events: "FUSEEventDispatcher"
    cache_config: dict[str, Any]

    # Mutable state (shared across handlers, protected by locks)
    fd_counter: int = 0
    open_files: dict[int, dict[str, Any]] = field(default_factory=dict)
    files_lock: "threading.RLock" = field(default_factory=threading.RLock)
    dir_cache: _TTLCache = field(default_factory=lambda: _TTLCache(maxsize=1024, ttl=5))
    dir_cache_lock: "threading.RLock" = field(default_factory=threading.RLock)


# ============================================================
# Decorator
# ============================================================


def _handle_remote_exception(e: Exception, operation: str, path: str, **context: Any) -> NoReturn:
    """Handle remote-specific exceptions with better error messages.

    Raises:
        FuseOSError: With appropriate errno based on exception type
    """
    context_str = ", ".join(f"{k}={v}" for k, v in context.items()) if context else ""

    if isinstance(e, RemoteTimeoutError):
        logger.error(f"[FUSE-{operation}] Timeout: {path} - {e} ({context_str})")
        raise FuseOSError(errno.ETIMEDOUT) from e
    if isinstance(e, RemoteConnectionError):
        logger.error(f"[FUSE-{operation}] Connection error: {path} - {e}")
        raise FuseOSError(errno.ECONNREFUSED) from e
    if isinstance(e, RemoteFilesystemError):
        logger.error(f"[FUSE-{operation}] Remote error: {path} - {e}")
        raise FuseOSError(errno.EIO) from e

    logger.exception(f"[FUSE-{operation}] Unexpected error: {path} ({context_str})")
    raise FuseOSError(errno.EIO) from e


def fuse_operation(op_name: str) -> Callable[..., Any]:
    """Decorator for FUSE operations that standardizes error handling (C1-C).

    Mapping:
        FuseOSError           -> re-raise
        NexusFileNotFoundError -> ENOENT
        NexusPermissionError   -> EACCES
        Exception              -> delegated to _handle_remote_exception
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(self: Any, path_arg: str, *args: Any, **kwargs: Any) -> Any:
            try:
                return func(self, path_arg, *args, **kwargs)
            except FuseOSError:
                raise
            except NexusFileNotFoundError:
                raise FuseOSError(errno.ENOENT) from None
            except NexusPermissionError as e:
                logger.error(f"[FUSE-{op_name}] Permission denied: {path_arg} - {e}")
                raise FuseOSError(errno.EACCES) from e
            except Exception as e:
                _handle_remote_exception(e, op_name, path_arg)

        return wrapper

    return decorator


# ============================================================
# Shared helper functions
# ============================================================


def rust_available(ctx: FUSESharedContext) -> bool:
    """Check if Rust daemon is available for delegation (Issue 8B DRY).

    IMPORTANT — Single-zone limitation (Issue #1569, Decision 1B):
    The Rust daemon operates with ONE API key set at startup. It has no
    per-request zone_id or subject context. Therefore Rust delegation is
    ONLY safe for global/admin mounts (no namespace context). When a
    context is present (agent mount with ReBAC), we MUST fall back to
    Python which passes the correct zone_id and subject to the kernel.

    DO NOT remove the ``not ctx.context`` guard without first adding
    per-request zone_id + subject metadata to the JSON-RPC protocol
    (tracked as a future enhancement).
    """
    return bool(ctx.use_rust and ctx.rust_client and not ctx.context)


def try_rust(
    ctx: FUSESharedContext, op_name: str, method_name: str, *args: Any, **kwargs: Any
) -> tuple[bool, Any]:
    """Try a Rust operation with standard fallback handling.

    Returns (success, result). ENOENT from Rust is re-raised immediately.
    """
    if not rust_available(ctx):
        return (False, None)
    assert ctx.rust_client is not None
    try:
        rust_fn = getattr(ctx.rust_client, method_name)
        result = rust_fn(*args, **kwargs)
        return (True, result)
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise FuseOSError(errno.ENOENT) from None
        logger.warning(f"[FUSE-{op_name}] Rust failed: {e}, using Python")
        return (False, None)
    except Exception as e:
        logger.warning(f"[FUSE-{op_name}] Rust failed: {e}, using Python")
        return (False, None)


def dir_cache_key(ctx: FUSESharedContext, path: str) -> str | tuple[str, str, str]:
    """Build context-aware dir cache key (Issue #1305)."""
    if ctx.context is not None:
        subj_type, subj_id = ctx.context.get_subject()
        return (path, subj_type, subj_id)
    return path


def invalidate_dir_cache(ctx: FUSESharedContext, path: str) -> None:
    """Invalidate readdir cache for the parent directory of a mutated path.

    Mutations (create/unlink/mkdir/rmdir/rename) change directory contents
    but FUSECacheManager.invalidate_path() only clears attr/content/parsed.
    This clears the separate dir_cache (TTLCache on FUSESharedContext).
    """
    parent = path.rsplit("/", 1)[0] or "/"
    with ctx.dir_cache_lock:
        # Invalidate both the plain key and any context-keyed variants
        ctx.dir_cache.pop(parent, None)
        key = dir_cache_key(ctx, parent)
        if key != parent:
            ctx.dir_cache.pop(key, None)


async def check_namespace_visible(ctx: FUSESharedContext, path: str) -> None:
    """Pre-flight namespace visibility check for mutating operations (Issue #1305).

    Raises:
        FuseOSError(ENOENT): If the path is invisible to this agent
    """
    if ctx.context is None:
        return

    if ctx.namespace_manager is not None:
        subject = ctx.context.get_subject()
        zone_id = getattr(ctx.context, "zone_id", None)
        if not ctx.namespace_manager.is_visible(subject, path, zone_id):
            raise FuseOSError(errno.ENOENT)
        return

    parent = path.rsplit("/", 1)[0] or "/"
    try:
        ctx.nexus_fs.is_directory(parent, context=ctx.context)
    except NexusFileNotFoundError:
        raise FuseOSError(errno.ENOENT) from None


def parse_virtual_path_for_fuse(ctx: FUSESharedContext, path: str) -> tuple[str, str | None]:
    """Parse virtual path to extract original path and view type."""
    if path.startswith("/.raw/"):
        original_path = path[5:]
        return (original_path, None)

    def _sync_access(p: str) -> bool:
        return ctx.nexus_fs.access(p)

    original_path, view_type, _ = parse_virtual_path(path, _sync_access)
    return original_path, view_type


async def get_file_content(
    ctx: FUSESharedContext,
    path: str,
    view_type: str | None,
    *,
    cache_priority: int = 0,
) -> bytes:
    """Get file content with appropriate view transformation.

    Issue #3397: Integrates lease-based cache coherence inline (rather than
    via the sync lease_gated_get()) because this function is already async
    and called from within asyncio.run().

    Flow: validity check → L1 hit → lease validate/acquire → L2/L3 fetch → cache

    Cache hierarchy: L1 (memory) -> L2 (disk) -> L3/L4 (backend).
    """
    coordinator = ctx.cache

    # For parsed views, check parsed cache first
    if view_type and (ctx.mode.value == "text" or ctx.mode.value == "smart"):
        cached_parsed = coordinator.get_parsed(path, view_type)
        if cached_parsed is not None and coordinator._check_validity(path):
            logger.debug(f"[FUSE-CONTENT] PARSED CACHE HIT: {path}")
            return cached_parsed

    # Lease-gated content read (Issue #3397)
    # Step 1: Hot path — validity cache + L1
    if coordinator._check_validity(path):
        cached = coordinator.get_content(path)
        if cached is not None:
            logger.info(f"[FUSE-CONTENT] L1 MEMORY HIT (leased): {path} ({len(cached)} bytes)")
            return _maybe_parse(ctx, path, view_type, cached)

    # Step 2: Validate/acquire lease (if lease manager present)
    has_lease = False
    if coordinator.lease_manager is not None:
        lease = coordinator._validate_lease(path)
        if lease is not None:
            coordinator._set_validity(path, lease.expires_at)
            cached = coordinator.get_content(path)
            if cached is not None:
                return _maybe_parse(ctx, path, view_type, cached)
            has_lease = True
        else:
            lease = coordinator._acquire_read_lease(path)
            has_lease = lease is not None
    else:
        # No lease manager — check L1 directly (backward compat)
        cached = coordinator.get_content(path)
        if cached is not None:
            logger.info(f"[FUSE-CONTENT] L1 MEMORY HIT: {path} ({len(cached)} bytes)")
            return _maybe_parse(ctx, path, view_type, cached)
        has_lease = True  # no lease manager = always "has lease" for caching

    # Step 3: L2/L3 fetch
    content = get_from_local_disk_cache(ctx, path)
    if content is not None:
        logger.info(f"[FUSE-CONTENT] L2 DISK HIT: {path} ({len(content)} bytes)")
    else:
        read_ctx = ctx.context
        logger.info(f"[FUSE-CONTENT] L3 BACKEND FETCH: {path}")
        fetch_start = time.time()
        raw_content = ctx.nexus_fs.sys_read(path, context=read_ctx)
        fetch_time = time.time() - fetch_start
        assert isinstance(raw_content, bytes), "Expected bytes from read()"
        content = raw_content
        logger.info(
            f"[FUSE-CONTENT] L3 BACKEND GOT: {path} ({len(content)} bytes) in {fetch_time:.3f}s"
        )
        put_to_local_disk_cache(ctx, path, content, priority=cache_priority)

    # Only cache if we hold a lease (Decision 11A: no caching without lease)
    if has_lease:
        coordinator.cache_content(path, content)

    return _maybe_parse(ctx, path, view_type, content)


def _maybe_parse(ctx: FUSESharedContext, path: str, view_type: str | None, content: bytes) -> bytes:
    """Apply view transformation if needed, caching the parsed result."""
    if ctx.mode.value == "binary" or view_type is None:
        return content

    if ctx.mode.value == "text" or (ctx.mode.value == "smart" and view_type):
        import importlib as _il

        create_default_parse_fn = _il.import_module("nexus.bricks.parsers").create_default_parse_fn
        from nexus.lib.virtual_views import get_parsed_content

        if not hasattr(ctx, "_parse_fn"):
            ctx._parse_fn = create_default_parse_fn()  # type: ignore[attr-defined]
        parsed_content = get_parsed_content(
            content,
            path,
            view_type or "txt",
            parse_fn=ctx._parse_fn,  # type: ignore[attr-defined]
        )
        ctx.cache.cache_parsed(path, view_type, parsed_content)
        return parsed_content

    return content


def get_content_hash(ctx: FUSESharedContext, path: str) -> str | None:
    """Get content hash for a file from metadata."""
    from nexus.lib.sync_bridge import run_sync as _run_sync

    try:
        metadata = _run_sync(get_metadata(ctx, path))
        if metadata is None:
            return None
        if isinstance(metadata, dict):
            return metadata.get("content_hash") or metadata.get("hash")
        return getattr(metadata, "content_hash", None) or getattr(metadata, "hash", None)
    except Exception:
        return None  # FUSE hot path — no logging to avoid perf impact


def get_zone_id(ctx: FUSESharedContext) -> str | None:
    """Get zone ID from the nexus_fs context."""
    try:
        return getattr(ctx.nexus_fs, "zone_id", None)
    except Exception:
        return None  # FUSE hot path — no logging to avoid perf impact


def read_range_from_backend(ctx: FUSESharedContext, path: str, offset: int, size: int) -> bytes:
    """Read a specific range of bytes from the backend.

    Used by ReadaheadManager for prefetching blocks.
    """
    from nexus.lib.sync_bridge import run_sync as _run_sync

    try:
        content = _run_sync(get_file_content(ctx, path, None))
        end = min(offset + size, len(content))
        return content[offset:end]
    except Exception as e:
        logger.warning(f"[FUSE-READAHEAD] Failed to read {path}:{offset}+{size}: {e}")
        return b""


def get_from_local_disk_cache(ctx: FUSESharedContext, path: str) -> bytes | None:
    """Get content from L2 local disk cache."""
    if ctx.local_disk_cache is None:
        return None

    try:
        content_hash = get_content_hash(ctx, path)
        if content_hash is None:
            return None
        zone_id = get_zone_id(ctx)
        content: bytes | None = cast(
            "bytes | None", ctx.local_disk_cache.get(content_hash, zone_id=zone_id)
        )
        if content is not None:
            logger.debug(f"[FUSE-L2] HIT: {path} (zone={zone_id})")
        return content
    except Exception as e:
        logger.debug(f"[FUSE-L2] Error reading {path}: {e}")
        return None


def put_to_local_disk_cache(
    ctx: FUSESharedContext, path: str, content: bytes, *, priority: int = 0
) -> None:
    """Store content in L2 local disk cache."""
    if ctx.local_disk_cache is None:
        return

    try:
        content_hash = get_content_hash(ctx, path)
        if content_hash is None:
            from nexus.core.hash_fast import hash_content

            content_hash = hash_content(content)

        zone_id = get_zone_id(ctx)
        store_blocks = len(content) > ctx.local_disk_cache.block_size
        ctx.local_disk_cache.put(
            content_hash,
            content,
            zone_id=zone_id,
            store_blocks=store_blocks,
            priority=priority,
        )
        logger.debug(f"[FUSE-L2] CACHED: {path} ({len(content)} bytes, zone={zone_id})")
    except Exception as e:
        logger.debug(f"[FUSE-L2] Error caching {path}: {e}")


async def get_metadata(ctx: FUSESharedContext, path: str) -> Any:
    """Get file/directory metadata from filesystem."""
    if hasattr(ctx.nexus_fs, "sys_stat"):
        metadata_dict = ctx.nexus_fs.sys_stat(path)
        if metadata_dict:
            return MetadataObj.from_dict(metadata_dict)
    return None


def stat_size_fallback(ctx: FUSESharedContext, path: str) -> int:
    """Get file size without reading the full file content.

    Uses stat() RPC as a lightweight alternative. Returns 0 as last resort
    instead of reading the entire file (Issue 15A perf fix).
    """
    if hasattr(ctx.nexus_fs, "stat"):
        try:
            stat_result = ctx.nexus_fs.stat(path)
            if stat_result:
                stat_size = stat_result.get("st_size") or stat_result.get("size", 0)
                if stat_size and stat_size > 0:
                    return int(stat_size)
        except Exception:
            pass  # stat fallback — handled below with default return 0

    # Issue 15A: Return 0 instead of reading full content
    logger.debug(f"[FUSE-PERF] stat fallback: returning 0 for {path}")
    return 0


def resolve_uid_gid() -> tuple[int, int]:
    """Resolve current user's uid/gid with Windows compatibility (Issue 2A DRY)."""
    try:
        return (os.getuid(), os.getgid())
    except AttributeError:
        return (0, 0)


def resolve_owner_group_to_uid_gid(metadata: Any, uid: int, gid: int) -> tuple[int, int]:
    """Map owner/group strings from metadata to uid/gid using pwd/grp modules."""
    if metadata is None:
        return (uid, gid)
    try:
        import grp
        import pwd

        owner = getattr(metadata, "owner", None)
        if owner:
            try:
                uid = pwd.getpwnam(owner).pw_uid
            except KeyError:
                import contextlib

                with contextlib.suppress(ValueError):
                    uid = int(owner)

        group = getattr(metadata, "group", None)
        if group:
            try:
                gid = grp.getgrnam(group).gr_gid
            except KeyError:
                import contextlib

                with contextlib.suppress(ValueError):
                    gid = int(group)

    except (ModuleNotFoundError, AttributeError):
        pass

    return (uid, gid)


def build_dir_attrs(metadata: Any = None) -> dict[str, Any]:
    """Get standard directory attributes."""
    now = time.time()
    uid, gid = resolve_uid_gid()

    dir_mode = 0o755
    if metadata and hasattr(metadata, "mode") and metadata.mode is not None:
        dir_mode = metadata.mode

    uid, gid = resolve_owner_group_to_uid_gid(metadata, uid, gid)

    return {
        "st_mode": stat.S_IFDIR | dir_mode,
        "st_nlink": 2,
        "st_size": 4096,
        "st_ctime": now,
        "st_mtime": now,
        "st_atime": now,
        "st_uid": uid,
        "st_gid": gid,
    }


def cache_file_attrs_from_list(
    ctx: FUSESharedContext, file_path: str, file_info: dict[str, Any], is_dir: bool
) -> None:
    """Cache file attributes from list() results to avoid N+1 queries."""
    now = time.time()
    uid, gid = resolve_uid_gid()

    if is_dir:
        attrs: dict[str, Any] = {
            "st_mode": stat.S_IFDIR | 0o755,
            "st_nlink": 2,
            "st_size": 4096,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
            "st_uid": uid,
            "st_gid": gid,
        }
    else:
        file_size = file_info.get("size", 0)
        attrs = {
            "st_mode": stat.S_IFREG | 0o644,
            "st_nlink": 1,
            "st_size": file_size,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
            "st_uid": uid,
            "st_gid": gid,
        }

    ctx.cache.cache_attr(file_path, attrs)


def resolve_io_profile(ctx: FUSESharedContext, path: str) -> str:
    """Resolve the I/O profile for a file path based on its mount (Issue #1413).

    Results are cached with bounded LRU (Issue #13A: prevent unbounded growth).
    """
    _IO_PROFILE_CACHE_MAXSIZE = 10000  # noqa: N806

    if not hasattr(ctx, "_io_profile_mounts"):
        ctx._io_profile_mounts = []  # type: ignore[attr-defined]
        ctx._io_profile_loaded = False  # type: ignore[attr-defined]
        ctx._io_profile_cache = {}  # type: ignore[attr-defined]

    if not ctx._io_profile_loaded:  # type: ignore[attr-defined]
        ctx._io_profile_loaded = True  # type: ignore[attr-defined]
        try:
            mount_svc = ctx.nexus_fs.service("mount")
            if mount_svc is None:
                raise AttributeError("mount service not available")
            from nexus.lib.sync_bridge import run_sync as _run_sync

            mounts = _run_sync(mount_svc.list_mounts())
            ctx._io_profile_mounts = sorted(  # type: ignore[attr-defined]
                [(m["mount_point"], m.get("io_profile", "balanced")) for m in mounts],
                key=lambda x: len(x[0]),
                reverse=True,
            )
        except Exception:
            pass  # mount list unavailable — IO profile resolution will use defaults

    cached = ctx._io_profile_cache.get(path)  # type: ignore[attr-defined]
    if cached is not None:
        return cast("str", cached)

    profile = "balanced"
    for mount_point, mp_profile in ctx._io_profile_mounts:  # type: ignore[attr-defined]
        if path == mount_point or path.startswith(mount_point.rstrip("/") + "/"):
            profile = mp_profile
            break

    # Bounded cache: evict oldest entries when full
    if len(ctx._io_profile_cache) >= _IO_PROFILE_CACHE_MAXSIZE:  # type: ignore[attr-defined]
        evict_count = _IO_PROFILE_CACHE_MAXSIZE // 10
        keys_to_remove = list(ctx._io_profile_cache.keys())[:evict_count]  # type: ignore[attr-defined]
        for key in keys_to_remove:
            del ctx._io_profile_cache[key]  # type: ignore[attr-defined]

    ctx._io_profile_cache[path] = profile  # type: ignore[attr-defined]
    return profile
