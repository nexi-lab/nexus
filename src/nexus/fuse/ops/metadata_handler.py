"""Metadata operations: getattr, readdir."""

import logging
import stat
import time
from typing import Any, cast

from fuse import FuseOSError

from nexus.fuse.filters import is_os_metadata_file
from nexus.fuse.ops._shared import (
    FUSESharedContext,
    build_dir_attrs,
    cache_file_attrs_from_list,
    check_namespace_visible,
    dir_cache_key,
    get_metadata,
    parse_virtual_path_for_fuse,
    resolve_owner_group_to_uid_gid,
    resolve_uid_gid,
    stat_size_fallback,
    try_rust,
)
from nexus.lib.virtual_views import should_add_virtual_views

logger = logging.getLogger(__name__)

# --- getattr size-estimation constants (Issue #1568) ---
_VIRTUAL_VIEW_DEFAULT_SIZE = 10 * 1024 * 1024  # 10 MiB safe over-report
_PARSED_SIZE_MULTIPLIER = 4  # Raw size × 4 estimates parsed output


class MetadataHandler:
    """Handles getattr and readdir operations."""

    def __init__(self, ctx: FUSESharedContext) -> None:
        self._ctx = ctx

    async def getattr(self, path: str, _fh: int | None = None) -> dict[str, Any]:
        """Get file attributes.

        Issue #3397: Integrates lease-based cache coherence inline.
        Flow: validity check → cache hit → lease validate/acquire → backend fetch → cache
        """
        ctx = self._ctx
        coordinator = ctx.cache
        start_time = time.time()

        # Step 1: Hot path — validity cache + L1 attr cache (~100ns)
        if coordinator._check_validity(path):
            cached = coordinator.get_attr(path)
            if cached is not None:
                return cached

        # Step 2: Validate/acquire lease if lease manager present
        has_lease = False
        if coordinator.lease_manager is not None:
            lease = coordinator._validate_lease(path)
            if lease is not None:
                coordinator._set_validity(path, lease.expires_at)
                cached = coordinator.get_attr(path)
                if cached is not None:
                    return cached
                has_lease = True
            else:
                lease = coordinator._acquire_read_lease(path)
                has_lease = lease is not None
        else:
            # No lease manager — serve from cache if available (backward compat)
            cached = coordinator.get_attr(path)
            if cached is not None:
                return cached
            has_lease = True  # always cache when no lease manager

        # Step 3: Backend fetch
        attrs = await self._fetch_attrs(path)

        # Only cache if we hold a lease (Decision 11A)
        if has_lease:
            coordinator.cache_attr(path, attrs)

        elapsed = time.time() - start_time
        if elapsed > 0.01:
            logger.info(f"[FUSE-PERF] getattr UNCACHED: path={path}, {elapsed:.3f}s")
        return attrs

    async def _fetch_attrs(self, path: str) -> dict[str, Any]:
        """Fetch attrs from backend."""
        ctx = self._ctx

        # Handle virtual views (.raw, .txt, .md)
        original_path, view_type = parse_virtual_path_for_fuse(ctx, path)

        # Special case: root directory always exists
        if original_path == "/":
            return build_dir_attrs()

        # Check if it's the .raw directory itself
        if path == "/.raw":
            return build_dir_attrs()

        # Issue #1569/8B: Delegate to Rust daemon for stat
        if not view_type:
            ok, rust_meta = try_rust(ctx, "GETATTR", "stat", original_path)
            if ok:
                if rust_meta.is_directory:
                    return build_dir_attrs()
                else:
                    return self._build_file_attrs(rust_meta.size)

        # Check if it's a directory
        if ctx.nexus_fs.is_directory(original_path, context=ctx.context):
            metadata = await get_metadata(ctx, original_path)
            return build_dir_attrs(metadata)

        # Validate namespace visibility
        await check_namespace_visible(ctx, original_path)

        if not ctx.nexus_fs.access(original_path):
            import errno

            raise FuseOSError(errno.ENOENT)

        # Get file metadata
        metadata = await get_metadata(ctx, original_path)

        # Resolve file size
        file_size = self._resolve_file_size(original_path, metadata, view_type)

        # Build file attrs
        uid, gid = resolve_uid_gid()

        file_mode = 0o644
        if metadata and metadata.mode is not None:
            file_mode = metadata.mode

        uid, gid = resolve_owner_group_to_uid_gid(metadata, uid, gid)

        now = time.time()
        return {
            "st_mode": stat.S_IFREG | file_mode,
            "st_nlink": 1,
            "st_size": file_size,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
            "st_uid": uid,
            "st_gid": gid,
        }

    def _build_file_attrs(self, file_size: int) -> dict[str, Any]:
        """Construct file attrs dict for Rust-provided metadata."""
        now = time.time()
        uid, gid = resolve_uid_gid()
        return {
            "st_mode": stat.S_IFREG | 0o644,
            "st_nlink": 1,
            "st_size": file_size,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
            "st_uid": uid,
            "st_gid": gid,
        }

    def _resolve_file_size(self, path: str, metadata: Any, view_type: str | None) -> int:
        """Resolve file size for getattr without reading the full file.

        For virtual views (parsed .md/.txt), uses a 5-tier estimation strategy
        that avoids the expensive full-file read + parse that previously happened
        on every getattr call (Issue #1568).

        Over-reporting st_size is safe: FUSE read() returns EOF naturally when
        actual content is shorter than the reported size.

        Tiers (virtual views only):
            1. Parsed cache size — exact, O(1)
            2. Raw content cache size × multiplier — estimate, O(1)
            3. Metadata size × multiplier — estimate, O(1)
            4. stat() RPC size × multiplier — lightweight RPC
            5. Constant default (10 MiB) — no I/O at all
        """
        ctx = self._ctx

        if view_type and view_type != "raw":
            # Tier 1: exact size from parsed cache
            parsed_size = ctx.cache.get_parsed_size(path, view_type)
            if parsed_size is not None:
                return parsed_size

            # Tier 2: estimate from raw content cache
            raw_content = ctx.cache.get_content(path)
            if raw_content is not None and len(raw_content) > 0:
                return len(raw_content) * _PARSED_SIZE_MULTIPLIER

            # Tier 3: estimate from metadata (already fetched by caller)
            if metadata:
                meta_size = (
                    metadata.get("size")
                    if isinstance(metadata, dict)
                    else getattr(metadata, "size", 0)
                )
                if meta_size and meta_size > 0:
                    return cast("int", meta_size) * _PARSED_SIZE_MULTIPLIER

            # Tier 4: estimate from stat() RPC
            stat_size = stat_size_fallback(ctx, path)
            if stat_size > 0:
                return stat_size * _PARSED_SIZE_MULTIPLIER

            # Tier 5: safe constant default
            return _VIRTUAL_VIEW_DEFAULT_SIZE

        # Raw file path — use metadata or stat directly (no multiplier)
        if metadata:
            meta_size = (
                metadata.get("size") if isinstance(metadata, dict) else getattr(metadata, "size", 0)
            )
            if meta_size and meta_size > 0:
                return cast("int", meta_size)
            return stat_size_fallback(ctx, path)

        return stat_size_fallback(ctx, path)

    async def readdir(self, path: str, _fh: int | None = None) -> list[str]:
        """Read directory contents."""
        ctx = self._ctx
        start_time = time.time()

        # Check readdir cache first
        cache_key = dir_cache_key(ctx, path)
        with ctx.dir_cache_lock:
            cached_entries = ctx.dir_cache.get(cache_key)
        if cached_entries is not None:
            logger.info(
                f"[FUSE-PERF] readdir CACHE HIT: path={path}, {len(cached_entries)} entries"
            )
            return cast("list[str]", cached_entries)

        logger.info(f"[FUSE-PERF] readdir START: path={path}")

        entries = [".", ".."]

        if path == "/":
            entries.append(".raw")

        # Rust delegation
        ok, file_entries = try_rust(ctx, "READDIR", "sys_readdir", path)
        if ok:
            for f in file_entries:
                name = f.name
                if name and name not in entries:
                    if is_os_metadata_file(name):
                        continue
                    entries.append(name)

                    if (
                        ctx.mode.value != "binary"
                        and should_add_virtual_views(name)
                        and f.entry_type != "directory"
                    ):
                        last_dot = name.rfind(".")
                        if last_dot != -1:
                            base_name = name[:last_dot]
                            extension = name[last_dot:]
                            parsed_name = f"{base_name}_parsed{extension}.md"
                            entries.append(parsed_name)

            elapsed = time.time() - start_time
            logger.info(
                f"[FUSE-PERF] readdir DONE via RUST: path={path}, "
                f"{len(entries)} entries, {elapsed:.3f}s"
            )
            with ctx.dir_cache_lock:
                ctx.dir_cache[cache_key] = entries
            return entries

        # Python path: list from filesystem
        list_start = time.time()
        files_raw = ctx.nexus_fs.sys_readdir(
            path, recursive=False, details=True, context=ctx.context
        )
        list_elapsed = time.time() - list_start
        files = files_raw if isinstance(files_raw, list) else []
        logger.info(
            f"[FUSE-PERF] readdir list() took {list_elapsed:.3f}s, returned {len(files)} items"
        )

        for file_info in files:
            if isinstance(file_info, str):
                file_path = file_info
                is_dir = ctx.nexus_fs.is_directory(file_path, context=ctx.context)
            else:
                file_path = str(file_info.get("path", ""))
                is_dir = file_info.get("is_directory", False)
                cache_file_attrs_from_list(ctx, file_path, file_info, is_dir)

            name = file_path.rstrip("/").split("/")[-1]
            if name and name not in entries:
                if is_os_metadata_file(name):
                    continue

                entries.append(name)

                if ctx.mode.value != "binary" and should_add_virtual_views(name) and not is_dir:
                    last_dot = name.rfind(".")
                    if last_dot != -1:
                        base_name = name[:last_dot]
                        extension = name[last_dot:]
                        parsed_name = f"{base_name}_parsed{extension}.md"
                        entries.append(parsed_name)

        entries = [e for e in entries if not is_os_metadata_file(e)]

        # Directory-level content prefetch (Issue 14A: configurable, opt-in)
        prefetch_enabled = ctx.cache_config.get("prefetch_enabled", False)
        prefetch_max_files = ctx.cache_config.get("prefetch_max_files", 100)
        prefetch_max_file_size = ctx.cache_config.get("prefetch_max_file_size", 256_000)

        if prefetch_enabled and ctx.context is None and len(files) <= prefetch_max_files:
            small_files: list[str] = [
                p
                for f in files
                if not (isinstance(f, dict) and f.get("is_directory", False))
                and (not isinstance(f, dict) or f.get("size", 0) < prefetch_max_file_size)
                for p in [f.get("path") if isinstance(f, dict) else f]
                if isinstance(p, str)
            ]
            logger.info(
                f"[FUSE-PERF] readdir prefetch check: {len(small_files)} small files, "
                f"has_read_bulk={hasattr(ctx.nexus_fs, 'read_bulk')}, "
                f"sample_paths={small_files[:3] if small_files else []}"
            )
            if small_files and hasattr(ctx.nexus_fs, "read_bulk"):
                try:
                    prefetch_start = time.time()
                    bulk_content = ctx.nexus_fs.read_bulk(small_files[:500])
                    for fpath, content in bulk_content.items():
                        if isinstance(content, bytes):
                            ctx.cache.cache_content(fpath, content)
                    prefetch_elapsed = time.time() - prefetch_start
                    logger.info(
                        f"[FUSE-PERF] readdir content prefetch: "
                        f"{len(bulk_content)} files in {prefetch_elapsed:.3f}s"
                    )
                except Exception as e:
                    logger.warning(f"[FUSE-PERF] readdir content prefetch failed: {e}")

        total_elapsed = time.time() - start_time
        logger.info(
            f"[FUSE-PERF] readdir DONE: path={path}, {len(entries)} entries, "
            f"{total_elapsed:.3f}s total"
        )

        with ctx.dir_cache_lock:
            ctx.dir_cache[cache_key] = entries

        return entries
