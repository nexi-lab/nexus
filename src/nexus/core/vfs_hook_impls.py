"""Concrete VFS hook implementations extracted from NexusFSCoreMixin.

Phase 4 of Issue #2033 (Strangler Fig decomposition):
  - DynamicViewerReadHook  — column-level CSV filtering (was _apply_dynamic_viewer_filter_if_needed)
  - AutoParseWriteHook     — fire-and-forget background parsing (was _auto_parse_file + _parse_in_thread)
  - TigerCacheRenameHook   — bitmap updates on file/directory move (was _update_tiger_cache_on_move)
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nexus.core.vfs_hooks import ReadHookContext, RenameHookContext, WriteHookContext

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 4.4 — Dynamic viewer column filter (post-read)
# ---------------------------------------------------------------------------


class DynamicViewerReadHook:
    """Post-read hook that applies column-level CSV filtering.

    Extracted from NexusFSCoreMixin._apply_dynamic_viewer_filter_if_needed().
    Only activates for .csv files when ReBAC dynamic_viewer grants exist.

    Dependencies injected at construction:
      - get_subject:            (context) -> str | None
      - get_viewer_config:      (subject, file_path) -> dict | None
      - apply_filter:           (data, column_config, file_format) -> dict
    """

    def __init__(
        self,
        get_subject: Callable[[Any], str | None],
        get_viewer_config: Callable[[str, str], dict | None],
        apply_filter: Callable[[str, dict, str], dict[str, Any]],
    ) -> None:
        self._get_subject = get_subject
        self._get_viewer_config = get_viewer_config
        self._apply_filter = apply_filter

    @property
    def name(self) -> str:
        return "dynamic_viewer"

    def on_post_read(self, ctx: ReadHookContext) -> None:
        if ctx.content is None:
            return

        # Only process CSV files
        if not ctx.path.lower().endswith(".csv"):
            return

        subject = self._get_subject(ctx.context)
        if not subject:
            return

        column_config = self._get_viewer_config(subject, ctx.path)
        if not column_config:
            return

        logger.info(
            f"[DynamicViewerHook] Applying filter for {subject} on {ctx.path}: {column_config}"
        )

        content_str = ctx.content.decode("utf-8") if isinstance(ctx.content, bytes) else ctx.content
        result = self._apply_filter(content_str, column_config, "csv")

        filtered = result["filtered_data"]
        if isinstance(filtered, str):
            ctx.content = filtered.encode("utf-8")
        elif isinstance(filtered, bytes):
            ctx.content = filtered
        else:
            ctx.content = str(filtered).encode("utf-8")

        logger.info(f"[DynamicViewerHook] Successfully filtered {ctx.path}")


# ---------------------------------------------------------------------------
# Phase 4.3 — Auto-parse on write (post-write)
# ---------------------------------------------------------------------------


class AutoParseWriteHook:
    """Post-write hook that auto-parses files in background threads.

    Extracted from NexusFSCoreMixin._auto_parse_file() + _parse_in_thread().
    Uses non-daemon threads to prevent DB corruption on shutdown.

    Dependencies injected at construction:
      - get_parser:  (path) -> parser | raises if unsupported
      - parse_fn:    async (path, store_result=True) -> result
    """

    def __init__(
        self,
        get_parser: Callable[[str], Any],
        parse_fn: Callable[..., Any],
    ) -> None:
        self._get_parser = get_parser
        self._parse_fn = parse_fn
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "auto_parse"

    def on_post_write(self, ctx: WriteHookContext) -> None:
        try:
            self._get_parser(ctx.path)
        except Exception:
            return

        thread = threading.Thread(
            target=self._run_parse,
            args=(ctx.path,),
            daemon=False,
            name=f"parser-{ctx.path}",
        )
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _run_parse(self, path: str) -> None:
        try:
            from nexus.lib.sync_bridge import run_sync

            run_sync(self._parse_fn(path, store_result=True))
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)

            if "disk" in error_msg.lower() or "space" in error_msg.lower():
                logger.error(
                    f"Auto-parse FAILED for {path}: Disk error - {error_type}: {error_msg}"
                )
            elif "database" in error_msg.lower() or "connection" in error_msg.lower():
                logger.error(f"Auto-parse FAILED for {path}: DB error - {error_type}: {error_msg}")
            elif "memory" in error_msg.lower() or isinstance(e, MemoryError):
                logger.error(
                    f"Auto-parse FAILED for {path}: Memory error - {error_type}: {error_msg}"
                )
            elif "permission" in error_msg.lower() or isinstance(e, PermissionError | OSError):
                logger.warning(
                    f"Auto-parse FAILED for {path}: Permission error - {error_type}: {error_msg}"
                )
            elif (
                "unsupported" in error_msg.lower()
                or "not supported" in error_msg.lower()
                or error_type == "UnsupportedFormatException"
            ):
                logger.debug(f"Auto-parse skipped for {path}: Unsupported format - {error_msg}")
            else:
                import traceback

                logger.warning(
                    f"Auto-parse FAILED for {path}: {error_type}: {error_msg}\n"
                    f"Stack trace:\n{traceback.format_exc()}"
                )

    def shutdown(self, timeout: float = 10.0) -> dict[str, Any]:
        """Gracefully shutdown background parser threads.

        Returns:
            Stats dict: total_threads, completed, timed_out, timeout_threads.
        """
        with self._lock:
            threads_to_wait = [t for t in self._threads if t.is_alive()]
            total = len(threads_to_wait)

        if total == 0:
            return {"total_threads": 0, "completed": 0, "timed_out": 0, "timeout_threads": []}

        logger.info(f"Waiting for {total} parser threads to complete (timeout: {timeout}s)...")

        completed = 0
        timed_out = 0
        timeout_threads: list[str] = []

        for thread in threads_to_wait:
            thread.join(timeout=timeout)
            if thread.is_alive():
                timed_out += 1
                timeout_threads.append(thread.name)
                logger.warning(f"Parser thread '{thread.name}' did not complete within {timeout}s.")
            else:
                completed += 1

        with self._lock:
            self._threads.clear()

        logger.info(f"Parser thread shutdown: {completed} completed, {timed_out} timed out")
        return {
            "total_threads": total,
            "completed": completed,
            "timed_out": timed_out,
            "timeout_threads": timeout_threads,
        }


# ---------------------------------------------------------------------------
# Phase 4.2 — Tiger cache bitmap update on rename (post-rename)
# ---------------------------------------------------------------------------


class TigerCacheRenameHook:
    """Post-rename hook that updates tiger cache bitmaps on file/directory moves.

    Extracted from NexusFSCoreMixin._update_tiger_cache_on_move() +
    _get_directory_files_for_move().

    Dependencies injected at construction:
      - tiger_cache:   The TigerCache instance (may be None)
      - metadata_list: (prefix, recursive, zone_id) -> Iterator[FileMetadata]
    """

    def __init__(
        self,
        tiger_cache: Any | None,
        metadata_list_iter: Callable[..., Any] | None = None,
    ) -> None:
        self._tiger_cache = tiger_cache
        self._metadata_list_iter = metadata_list_iter

    @property
    def name(self) -> str:
        return "tiger_cache_rename"

    def on_post_rename(self, ctx: RenameHookContext) -> None:
        if self._tiger_cache is None:
            return

        zone_id = ctx.zone_id or "root"
        old_grants = self._tiger_cache.get_directory_grants_for_path(ctx.old_path, zone_id)
        new_grants = self._tiger_cache.get_directory_grants_for_path(ctx.new_path, zone_id)

        def grant_key(g: dict) -> tuple:
            return (g["subject_type"], g["subject_id"], g["permission"])

        old_keys = {grant_key(g) for g in old_grants}
        new_keys = {grant_key(g) for g in new_grants}

        grants_to_remove = old_keys - new_keys
        grants_to_add = new_keys - old_keys

        if not grants_to_remove and not grants_to_add:
            return

        if ctx.is_directory:
            files = self._get_directory_files(ctx.old_path, ctx.new_path, zone_id)
        else:
            files = [(ctx.old_path, ctx.new_path)]

        resource_map = getattr(self._tiger_cache, "_resource_map", None)
        if not resource_map:
            return

        for _old_file, new_file in files:
            int_id = resource_map.get_or_create_int_id("file", new_file)
            if int_id <= 0:
                continue

            for subject_type, subject_id, permission in grants_to_remove:
                try:
                    self._tiger_cache.remove_from_bitmap(
                        subject_type=subject_type,
                        subject_id=subject_id,
                        permission=permission,
                        resource_type="file",
                        zone_id=zone_id,
                        resource_int_id=int_id,
                    )
                except Exception as e:
                    logger.warning(f"[LEOPARD] Failed to remove from bitmap: {e}")

            for grant in new_grants:
                key = grant_key(grant)
                if key not in grants_to_add:
                    continue
                if not grant.get("include_future_files", True):
                    continue
                try:
                    self._tiger_cache.add_to_bitmap(
                        grant["subject_type"],
                        grant["subject_id"],
                        grant["permission"],
                        "file",
                        zone_id,
                        int_id,
                    )
                    self._tiger_cache.persist_single_grant(
                        grant["subject_type"],
                        grant["subject_id"],
                        grant["permission"],
                        "file",
                        new_file,
                        zone_id,
                    )
                except Exception as e:
                    logger.warning(f"[LEOPARD] Failed to add to bitmap: {e}")

    def _get_directory_files(
        self, old_dir: str, new_dir: str, zone_id: str
    ) -> list[tuple[str, str]]:
        if self._metadata_list_iter is None:
            return []

        old_prefix = old_dir.rstrip("/") + "/"
        new_prefix = new_dir.rstrip("/") + "/"

        try:
            result = []
            for file_meta in self._metadata_list_iter(
                prefix=new_prefix, recursive=True, zone_id=zone_id
            ):
                new_file_path = file_meta.path
                if new_file_path:
                    relative_path = new_file_path[len(new_prefix) :]
                    old_file_path = old_prefix + relative_path
                    result.append((old_file_path, new_file_path))
            return result
        except Exception as e:
            logger.warning(f"[LEOPARD] Failed to list directory files: {e}")
            return []
