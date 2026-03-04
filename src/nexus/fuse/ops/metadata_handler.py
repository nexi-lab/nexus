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

    def getattr(self, path: str, _fh: int | None = None) -> dict[str, Any]:
        """Get file attributes."""
        ctx = self._ctx
        start_time = time.time()

        # Check cache first
        cached_attrs = ctx.cache.get_attr(path)
        if cached_attrs is not None:
            elapsed = time.time() - start_time
            if elapsed > 0.001:
                logger.debug("[FUSE-PERF] getattr CACHED: path=%s, %.3fs", path, elapsed)
            return cached_attrs

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
                    attrs = build_dir_attrs()
                else:
                    attrs = self._build_file_attrs(rust_meta.size)
                ctx.cache.cache_attr(path, attrs)
                elapsed = time.time() - start_time
                logger.debug("[FUSE-PERF] getattr via RUST: path=%s, %.3fs", path, elapsed)
                return attrs

        # Check if it's a directory
        if ctx.nexus_fs.sys_is_directory(original_path, context=ctx.context):
            metadata = get_metadata(ctx, original_path)
            return build_dir_attrs(metadata)

        # Validate namespace visibility
        check_namespace_visible(ctx, original_path)

        if not ctx.nexus_fs.sys_access(original_path):
            import errno

            raise FuseOSError(errno.ENOENT)

        # Get file metadata
        metadata = get_metadata(ctx, original_path)

        # Resolve file size
        file_size = self._resolve_file_size(original_path, metadata, view_type)

        # Build file attrs
        uid, gid = resolve_uid_gid()

        file_mode = 0o644
        if metadata and metadata.mode is not None:
            file_mode = metadata.mode

        uid, gid = resolve_owner_group_to_uid_gid(metadata, uid, gid)

        now = time.time()
        attrs = {
            "st_mode": stat.S_IFREG | file_mode,
            "st_nlink": 1,
            "st_size": file_size,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
            "st_uid": uid,
            "st_gid": gid,
        }

        ctx.cache.cache_attr(path, attrs)

        elapsed = time.time() - start_time
        if elapsed > 0.01:
            logger.info("[FUSE-PERF] getattr UNCACHED: path=%s, %.3fs", path, elapsed)
        return attrs

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

    def readdir(self, path: str, _fh: int | None = None) -> list[str]:
        """Read directory contents."""
        ctx = self._ctx
        start_time = time.time()

        # Check readdir cache first
        cache_key = dir_cache_key(ctx, path)
        with ctx.dir_cache_lock:
            cached_entries = ctx.dir_cache.get(cache_key)
        if cached_entries is not None:
            logger.info(
                "[FUSE-PERF] readdir CACHE HIT: path=%s, %d entries", path, len(cached_entries)
            )
            return cast("list[str]", cached_entries)

        logger.info("[FUSE-PERF] readdir START: path=%s", path)

        entries = [".", ".."]

        if path == "/":
            entries.append(".raw")

        # Rust delegation
        ok, file_entries = try_rust(ctx, "READDIR", "list", path)
        if ok:
            entries.extend([f.name for f in file_entries])
            elapsed = time.time() - start_time
            logger.info(
                "[FUSE-PERF] readdir DONE via RUST: path=%s, %d entries, %.3fs",
                path,
                len(entries),
                elapsed,
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
            "[FUSE-PERF] readdir list() took %.3fs, returned %d items", list_elapsed, len(files)
        )

        for file_info in files:
            if isinstance(file_info, str):
                file_path = file_info
                is_dir = ctx.nexus_fs.sys_is_directory(file_path, context=ctx.context)
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
            small_files = [
                f.get("path") if isinstance(f, dict) else f
                for f in files
                if not (isinstance(f, dict) and f.get("is_directory", False))
                and (not isinstance(f, dict) or f.get("size", 0) < prefetch_max_file_size)
            ]
            logger.info(
                "[FUSE-PERF] readdir prefetch check: %d small files, "
                "has_read_bulk=%s, sample_paths=%s",
                len(small_files),
                hasattr(ctx.nexus_fs, "read_bulk"),
                small_files[:3] if small_files else [],
            )
            if small_files and hasattr(ctx.nexus_fs, "read_bulk"):
                try:
                    prefetch_start = time.time()
                    bulk_content = ctx.nexus_fs.read_bulk(small_files[:500])
                    for fpath, content in bulk_content.items():
                        if content is not None:
                            ctx.cache.cache_content(fpath, content)
                    prefetch_elapsed = time.time() - prefetch_start
                    logger.info(
                        "[FUSE-PERF] readdir content prefetch: %d files in %.3fs",
                        len(bulk_content),
                        prefetch_elapsed,
                    )
                except Exception as e:
                    logger.warning("[FUSE-PERF] readdir content prefetch failed: %s", e)

        total_elapsed = time.time() - start_time
        logger.info(
            "[FUSE-PERF] readdir DONE: path=%s, %d entries, %.3fs total",
            path,
            len(entries),
            total_elapsed,
        )

        with ctx.dir_cache_lock:
            ctx.dir_cache[cache_key] = entries

        return entries
