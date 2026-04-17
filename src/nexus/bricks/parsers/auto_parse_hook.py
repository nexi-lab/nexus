"""AutoParseWriteHook — fire-and-forget background parsing (INTERCEPT write).

Issue #625: Lives in parsers/ (service-layer, not kernel).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nexus.contracts.vfs_hooks import WriteHookContext

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec

logger = logging.getLogger(__name__)


class AutoParseWriteHook:
    """Post-write hook that auto-parses files in background threads.

    Uses non-daemon threads to prevent DB corruption on shutdown.
    Also invalidates cached parsed_text on every write (Issue #1383).

    Dependencies injected at construction:
      - get_parser:  (path) -> parser | raises if unsupported
      - parse_fn:    (content: bytes, path: str) -> bytes | None
      - metadata:    MetastoreABC (optional, for cache invalidation)
    """

    # ── Hook spec (duck-typed, Issue #1612) ─────────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(write_hooks=(self,))

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
        # Invalidate cached parsed_text so ContentParserEngine re-parses.
        # ``parsed_text_hash`` MUST be cleared alongside ``parsed_text`` —
        # the search indexer treats a matching hash as proof the parser
        # ran successfully against the current bytes and takes the empty-
        # replace path if the read produces "".  A lingering stale hash
        # after a revert-to-previous-revision (same bytes, same hash)
        # could otherwise convince the indexer to zero out a document
        # that actually has text, just because the parser is temporarily
        # unavailable.
        if self._metadata is not None:
            try:
                self._metadata.set_file_metadata(ctx.path, "parsed_text", None)
                self._metadata.set_file_metadata(ctx.path, "parsed_text_hash", None)
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
            args=(ctx.content, ctx.path),
            daemon=False,
            name=f"parser-{ctx.path}",
        )
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _run_parse(self, content: bytes, path: str) -> None:
        try:
            self._parse_fn(content, path)
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
