"""FUSE operation handlers for Nexus filesystem.

Thin assembler that delegates to per-concern handler modules in `ops/`.

Hybrid Python/Rust mode (--use-rust):
    When enabled, hot-path I/O (read, write, readdir, stat, mkdir, unlink,
    rename) is delegated to a Rust daemon via Unix-socket JSON-RPC IPC.
    Python retains orchestration duties: permissions, events, namespace
    resolution, and virtual views.
"""

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from cachetools import TTLCache as _TTLCache
from fuse import Operations

from nexus.fuse.cache import FUSECacheManager
from nexus.fuse.lease_coordinator import FUSELeaseCoordinator
from nexus.fuse.ops._events import FUSEEventDispatcher
from nexus.fuse.ops._shared import (
    FUSESharedContext,
    check_namespace_visible,
    dir_cache_key,
    fuse_operation,
    get_content_hash,
    get_file_content,
    get_zone_id,
    parse_virtual_path_for_fuse,
    read_range_from_backend,
    rust_available,
    try_rust,
)
from nexus.fuse.ops.attr_handler import AttrHandler
from nexus.fuse.ops.io_handler import IOHandler
from nexus.fuse.ops.metadata_handler import MetadataHandler
from nexus.fuse.ops.mutation_handler import MutationHandler

# Import readahead for sequential read optimization (Issue #1073)
try:
    from nexus.fuse.readahead import ReadaheadConfig, ReadaheadManager

    HAS_READAHEAD = True
except ImportError:
    HAS_READAHEAD = False
    ReadaheadConfig = None  # type: ignore[misc,assignment]
    ReadaheadManager = None  # type: ignore[misc,assignment]

# Import LocalDiskCache for L2 caching (Issue #1072)
try:
    from nexus.storage.local_disk_cache import LocalDiskCache

    HAS_LOCAL_DISK_CACHE = True
except ImportError:
    HAS_LOCAL_DISK_CACHE = False
    LocalDiskCache = None  # type: ignore[misc,assignment]

# Import event system (Issue #1115)
try:
    from nexus.core.file_events import FileEvent, FileEventType

    HAS_EVENT_BUS = True
except ImportError:
    HAS_EVENT_BUS = False
    FileEvent = None  # type: ignore[misc,assignment]
    FileEventType = None  # type: ignore[misc,assignment]

if TYPE_CHECKING:
    from nexus.bricks.rebac.namespace_manager import NamespaceManager
    from nexus.contracts.filesystem.filesystem_abc import NexusFilesystem
    from nexus.contracts.types import OperationContext
    from nexus.fuse.mount import MountMode

logger = logging.getLogger(__name__)


class NexusFUSEOperations(Operations):
    """FUSE operations implementation for Nexus filesystem.

    Thin assembler that creates a FUSESharedContext and delegates each
    FUSE callback to a per-concern handler class.
    """

    def __init__(
        self,
        nexus_fs: "NexusFilesystem",
        mode: "MountMode",
        cache_config: dict[str, Any] | None = None,
        context: "OperationContext | None" = None,
        namespace_manager: "NamespaceManager | None" = None,
        use_rust: bool = False,
        event_bus: Any | None = None,
        subscription_manager: Any | None = None,
        lease_manager: Any | None = None,
        mount_id: str | None = None,
        file_cache: Any | None = None,
    ) -> None:
        self._context = context
        cache_config = cache_config or {}

        # Initialize Rust client
        rust_client = None
        if use_rust:
            try:
                from nexus.fuse.rust_client import RustFUSEClient

                if hasattr(nexus_fs, "_base_url") and hasattr(nexus_fs, "_api_key"):
                    nexus_url = nexus_fs._base_url  # noqa: SLF001
                    api_key = nexus_fs._api_key  # noqa: SLF001
                    agent_id = getattr(nexus_fs, "_agent_id", None)

                    logger.info("[FUSE] Initializing Rust FUSE daemon (10-100x faster)")
                    rust_client = RustFUSEClient(
                        nexus_url=nexus_url, api_key=api_key, agent_id=agent_id
                    )
                    logger.info("[FUSE] Rust daemon ready")
                else:
                    logger.warning(
                        "[FUSE] --use-rust requires REMOTE profile NexusFS. Falling back to Python."
                    )
                    use_rust = False
            except Exception as e:
                logger.error(f"[FUSE] Failed to initialize Rust client: {e}")
                logger.warning("[FUSE] Falling back to Python client")
                use_rust = False

        # Initialize cache with lease coordinator (Issue #3397)
        bare_cache = FUSECacheManager(
            attr_cache_size=cache_config.get("attr_cache_size", 1024),
            attr_cache_ttl=cache_config.get("attr_cache_ttl", 60),
            content_cache_size=cache_config.get("content_cache_size", 10000),
            parsed_cache_size=cache_config.get("parsed_cache_size", 50),
            enable_metrics=cache_config.get("enable_metrics", False),
        )

        # Wrap in lease coordinator for cross-mount cache coherence
        holder_id = mount_id or "default-mount"
        _zone_id = getattr(nexus_fs, "zone_id", None)
        cache = FUSELeaseCoordinator(
            cache=bare_cache,
            lease_manager=lease_manager,
            holder_id=holder_id,
            file_cache=file_cache,
            zone_id=_zone_id,
        )

        # Initialize L2 local disk cache (Issue #1072)
        local_disk_cache = None
        enable_local_disk_cache = cache_config.get("enable_local_disk_cache", True)
        if enable_local_disk_cache and HAS_LOCAL_DISK_CACHE and LocalDiskCache is not None:
            try:
                ldc_kwargs: dict[str, Any] = {}
                if cache_config.get("local_disk_cache_dir") is not None:
                    ldc_kwargs["cache_dir"] = cache_config["local_disk_cache_dir"]
                if cache_config.get("local_disk_cache_size_gb") is not None:
                    ldc_kwargs["max_size_gb"] = cache_config["local_disk_cache_size_gb"]
                local_disk_cache = LocalDiskCache(**ldc_kwargs)
                logger.info("[FUSE] L2 LocalDiskCache enabled for faster reads")
            except Exception as e:
                logger.warning(f"[FUSE] Failed to initialize LocalDiskCache: {e}")

        # Initialize event dispatcher
        enable_events = cache_config.get("events_enabled", True) and HAS_EVENT_BUS
        event_loop = None
        if enable_events:
            with suppress(RuntimeError):
                event_loop = asyncio.get_running_loop()
            logger.info("[FUSE] Event firing enabled")

        events = FUSEEventDispatcher(
            event_bus=event_bus,
            subscription_manager=subscription_manager,
            zone_id_fn=lambda: getattr(nexus_fs, "zone_id", None),
            enable_events=enable_events,
            event_loop=event_loop,
        )

        # Initialize readdir cache
        dir_cache_ttl = cache_config.get("dir_cache_ttl", 5)

        # Build shared context
        self._ctx = FUSESharedContext(
            nexus_fs=nexus_fs,
            mode=mode,
            context=context,
            namespace_manager=namespace_manager,
            cache=cache,
            local_disk_cache=local_disk_cache,
            readahead=None,  # set below
            rust_client=rust_client,
            use_rust=use_rust,
            events=events,
            cache_config=cache_config,
            dir_cache=_TTLCache(maxsize=1024, ttl=dir_cache_ttl),
        )

        # Initialize readahead manager (needs context for read_range_from_backend)
        readahead = None
        enable_readahead = cache_config.get("readahead_enabled", True)
        if enable_readahead and HAS_READAHEAD and ReadaheadConfig is not None:
            try:
                readahead_config = ReadaheadConfig.from_dict(cache_config)
                readahead = ReadaheadManager(
                    config=readahead_config,
                    read_func=lambda path, offset, size: read_range_from_backend(
                        self._ctx, path, offset, size
                    ),
                    local_disk_cache=local_disk_cache,
                    content_hash_func=lambda path: get_content_hash(self._ctx, path),
                    zone_id=get_zone_id(self._ctx),
                )
                logger.info(
                    f"[FUSE] Readahead enabled: buffer={readahead_config.buffer_pool_mb}MB, "
                    f"workers={readahead_config.prefetch_workers}"
                )
            except Exception as e:
                logger.warning(f"[FUSE] Failed to initialize ReadaheadManager: {e}")
        self._ctx.readahead = readahead

        # Expose shared state for backward compatibility (tests access these directly)
        self.nexus_fs = nexus_fs
        self.mode = mode

        # Instantiate handlers
        self._meta = MetadataHandler(self._ctx)
        self._io = IOHandler(self._ctx)
        self._mut = MutationHandler(self._ctx)
        self._attr = AttrHandler(self._ctx)

    # ------------------------------------------------------------------
    # Backward-compat properties (tests use these directly)
    # ------------------------------------------------------------------

    @property
    def cache(self) -> Any:
        return self._ctx.cache

    @cache.setter
    def cache(self, value: Any) -> None:
        self._ctx.cache = value

    @property
    def fd_counter(self) -> int:
        return self._ctx.fd_counter

    @fd_counter.setter
    def fd_counter(self, value: int) -> None:
        self._ctx.fd_counter = value

    @property
    def open_files(self) -> dict[int, dict[str, Any]]:
        return self._ctx.open_files

    @open_files.setter
    def open_files(self, value: dict[int, dict[str, Any]]) -> None:
        self._ctx.open_files = value

    @property
    def _files_lock(self) -> Any:
        return self._ctx.files_lock

    @property
    def _dir_cache(self) -> Any:
        return self._ctx.dir_cache

    @property
    def _dir_cache_lock(self) -> Any:
        return self._ctx.dir_cache_lock

    @property
    def _use_rust(self) -> bool:
        return self._ctx.use_rust

    @_use_rust.setter
    def _use_rust(self, value: bool) -> None:
        self._ctx.use_rust = value

    @property
    def _rust_client(self) -> Any:
        return self._ctx.rust_client

    @_rust_client.setter
    def _rust_client(self, value: Any) -> None:
        self._ctx.rust_client = value

    @property
    def _readahead(self) -> Any:
        return self._ctx.readahead

    @_readahead.setter
    def _readahead(self, value: Any) -> None:
        self._ctx.readahead = value

    @property
    def _local_disk_cache(self) -> Any:
        return self._ctx.local_disk_cache

    @_local_disk_cache.setter
    def _local_disk_cache(self, value: Any) -> None:
        self._ctx.local_disk_cache = value

    @property
    def _rust_available(self) -> bool:
        return rust_available(self._ctx)

    def _try_rust(
        self, op_name: str, method_name: str, *args: Any, **kwargs: Any
    ) -> tuple[bool, Any]:
        return try_rust(self._ctx, op_name, method_name, *args, **kwargs)

    def _dir_cache_key(self, path: str) -> str | tuple[str, str, str]:
        return dir_cache_key(self._ctx, path)

    def _check_namespace_visible(self, path: str) -> None:
        return asyncio.run(check_namespace_visible(self._ctx, path))

    def _parse_virtual_path(self, path: str) -> tuple[str, str | None]:
        return parse_virtual_path_for_fuse(self._ctx, path)

    def _get_file_content(self, path: str, view_type: str | None, **kwargs: Any) -> bytes:
        return asyncio.run(get_file_content(self._ctx, path, view_type, **kwargs))

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the event loop for async event dispatching."""
        self._event_loop = loop
        self._ctx.events.set_event_loop(loop)

    # ------------------------------------------------------------------
    # FUSE operation stubs — delegation to handlers
    # ------------------------------------------------------------------

    @fuse_operation("GETATTR")
    def getattr(self, path: str, fh: int | None = None) -> dict[str, Any]:
        return asyncio.run(self._meta.getattr(path, fh))

    @fuse_operation("READDIR")
    def readdir(self, path: str, fh: int | None = None) -> list[str]:
        return asyncio.run(self._meta.readdir(path, fh))

    @fuse_operation("OPEN")
    def open(self, path: str, flags: int) -> int:
        return asyncio.run(self._io.open(path, flags))

    @fuse_operation("READ")
    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        return asyncio.run(self._io.read(path, size, offset, fh))

    @fuse_operation("WRITE")
    def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
        return asyncio.run(self._io.write(path, data, offset, fh))

    def release(self, path: str, fh: int) -> None:
        return self._io.release(path, fh)

    @fuse_operation("CREATE")
    def create(self, path: str, mode: int, fi: Any = None) -> int:
        return asyncio.run(self._mut.create(path, mode, fi))

    @fuse_operation("UNLINK")
    def unlink(self, path: str) -> None:
        return asyncio.run(self._mut.unlink(path))

    @fuse_operation("MKDIR")
    def mkdir(self, path: str, mode: int) -> None:
        return asyncio.run(self._mut.mkdir(path, mode))

    @fuse_operation("RMDIR")
    def rmdir(self, path: str) -> None:
        return asyncio.run(self._mut.rmdir(path))

    @fuse_operation("RENAME")
    def rename(self, old: str, new: str) -> None:
        return asyncio.run(self._mut.rename(old, new))

    @fuse_operation("CHMOD")
    def chmod(self, path: str, mode: int) -> None:
        return asyncio.run(self._attr.chmod(path, mode))

    @fuse_operation("CHOWN")
    def chown(self, path: str, uid: int, gid: int) -> None:
        return asyncio.run(self._attr.chown(path, uid, gid))

    @fuse_operation("TRUNCATE")
    def truncate(self, path: str, length: int, fh: int | None = None) -> None:
        return asyncio.run(self._attr.truncate(path, length, fh))

    def utimens(self, path: str, times: tuple[float, float] | None = None) -> None:
        return self._attr.utimens(path, times)
