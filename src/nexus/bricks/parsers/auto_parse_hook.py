"""AutoParseWriteHook — fire-and-forget background parsing (INTERCEPT write).

Issue #625: Lives in parsers/ (service-layer, not kernel).
"""

import logging
import threading
from collections.abc import Callable
from typing import Any

from nexus.contracts.vfs_hooks import WriteHookContext

logger = logging.getLogger(__name__)


class AutoParseWriteHook:
    """Post-write hook that auto-parses files in background threads.

    Uses non-daemon threads to prevent DB corruption on shutdown.
    Also invalidates cached parsed_text on every write (Issue #1383).

    Dependencies injected at construction:
      - get_parser:  (path) -> parser | raises if unsupported
      - parse_fn:    async (path, store_result=True) -> result
      - metadata:    MetastoreABC (optional, for cache invalidation)
    """

    def __init__(
        self,
        get_parser: Callable[[str], Any],
        parse_fn: Callable[..., Any],
        metadata: Any = None,
    ) -> None:
        self._get_parser = get_parser
        self._parse_fn = parse_fn
        self._metadata = metadata
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "auto_parse"

    def on_post_write(self, ctx: WriteHookContext) -> None:
        # Invalidate cached parsed_text so ContentParserEngine re-parses
        if self._metadata is not None:
            try:
                self._metadata.set_file_metadata(ctx.path, "parsed_text", None)
                self._metadata.set_file_metadata(ctx.path, "parsed_at", None)
                self._metadata.set_file_metadata(ctx.path, "parser_name", None)
            except Exception:
                pass  # Best-effort cache invalidation

        try:
            self._get_parser(ctx.path)
        except Exception:
            return  # No parser available for this file type — skip background parse

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
                    "Auto-parse FAILED for %s: Disk error - %s: %s", path, error_type, error_msg
                )
            elif "database" in error_msg.lower() or "connection" in error_msg.lower():
                logger.error(
                    "Auto-parse FAILED for %s: DB error - %s: %s", path, error_type, error_msg
                )
            elif "memory" in error_msg.lower() or isinstance(e, MemoryError):
                logger.error(
                    "Auto-parse FAILED for %s: Memory error - %s: %s", path, error_type, error_msg
                )
            elif "permission" in error_msg.lower() or isinstance(e, PermissionError | OSError):
                logger.warning(
                    "Auto-parse FAILED for %s: Permission error - %s: %s",
                    path,
                    error_type,
                    error_msg,
                )
            elif (
                "unsupported" in error_msg.lower()
                or "not supported" in error_msg.lower()
                or error_type == "UnsupportedFormatException"
            ):
                logger.debug("Auto-parse skipped for %s: Unsupported format - %s", path, error_msg)
            else:
                import traceback

                logger.warning(
                    "Auto-parse FAILED for %s: %s: %s\nStack trace:\n%s",
                    path,
                    error_type,
                    error_msg,
                    traceback.format_exc(),
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

        logger.info("Waiting for %d parser threads to complete (timeout: %ss)...", total, timeout)

        completed = 0
        timed_out = 0
        timeout_threads: list[str] = []

        for thread in threads_to_wait:
            thread.join(timeout=timeout)
            if thread.is_alive():
                timed_out += 1
                timeout_threads.append(thread.name)
                logger.warning(
                    "Parser thread '%s' did not complete within %ss.", thread.name, timeout
                )
            else:
                completed += 1

        with self._lock:
            self._threads.clear()

        logger.info("Parser thread shutdown: %d completed, %d timed out", completed, timed_out)
        return {
            "total_threads": total,
            "completed": completed,
            "timed_out": timed_out,
            "timeout_threads": timeout_threads,
        }
