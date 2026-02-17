"""Search Grep Mixin - Extracted from SearchService (Issue #1287, 8A).

This mixin provides all content searching (grep) functionality:
- Regex-based content search with 5 adaptive strategies
- Zoekt index integration
- Trigram index integration (Issue #954)
- Parallel grep with thread pool
- Mmap-accelerated grep

Extracted from: search_service.py (2,265 lines -> ~660 remaining)
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import os
import re
from typing import TYPE_CHECKING, Any, cast

from nexus.core import glob_fast, grep_fast, trigram_fast
from nexus.core.rpc_decorator import rpc_expose
from nexus.search.strategies import (
    GREP_PARALLEL_WORKERS,
    SearchStrategy,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


class SearchGrepMixin:
    """Mixin providing content search (grep) capabilities for SearchService.

    Accesses SearchService attributes via ``self``:
    - metadata, _permission_enforcer, _enforce_permissions
    - _get_thread_pool(), _get_routing_params(), _extract_zone_info()
    - _read(), _read_bulk(), _validate_path()
    - list(), glob()  (from SearchListingMixin / SearchService)
    - _select_grep_strategy()  (from SearchService)
    """

    # ------------------------------------------------------------------
    # Public API: Content Searching (Grep)
    # ------------------------------------------------------------------

    @rpc_expose(description="Search file contents")
    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        search_mode: str = "auto",  # noqa: ARG002
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        r"""Search file contents using regex patterns.

        Uses adaptive algorithm selection (Issue #929) with 5 strategies.

        Args:
            pattern: Regex pattern to search for
            path: Base path to search from (default: "/")
            file_pattern: Optional glob pattern to filter files (e.g., "*.py")
            ignore_case: If True, case-insensitive search
            max_results: Maximum number of results (default: 100)
            search_mode: Deprecated, kept for backward compat
            context: Operation context for permission filtering
        """
        from .search_service import _filter_ignored_paths  # noqa: F811

        if path and path != "/":
            path = self._validate_path(path)

        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        # Phase 1: Get files to search
        if file_pattern:
            files = self.glob(file_pattern, path, context=context)
        else:
            files = cast(list[str], self.list(path, recursive=True, context=context))
            pre_filter_count = len(files)
            files = _filter_ignored_paths(files)
            if pre_filter_count != len(files):
                logger.debug(f"[GREP] Issue #538: Filtered {pre_filter_count - len(files)} paths")

        if not files:
            return []

        # Phase 2: Bulk fetch searchable text
        searchable_texts = self.metadata.get_searchable_text_bulk(files)
        cached_text_ratio = len(searchable_texts) / len(files) if files else 0.0
        files_needing_raw = [f for f in files if f not in searchable_texts]

        # Phase 3: Select strategy (Issue #929, #954)
        zone_id, _, _ = self._get_routing_params(context)
        strategy = self._select_grep_strategy(
            file_count=len(files),
            cached_text_ratio=cached_text_ratio,
            zone_id=zone_id,
        )

        # Phase 4: Execute strategy-specific search
        results: list[dict[str, Any]] = []

        # Strategy: TRIGRAM_INDEX (Issue #954)
        if strategy == SearchStrategy.TRIGRAM_INDEX and zone_id:
            trigram_results = self._try_grep_with_trigram(
                pattern=pattern,
                ignore_case=ignore_case,
                max_results=max_results,
                zone_id=zone_id,
                context=context,
            )
            if trigram_results is not None:
                return trigram_results
            strategy = SearchStrategy.RUST_BULK  # Fallback

        # Strategy: ZOEKT_INDEX
        if strategy == SearchStrategy.ZOEKT_INDEX:
            zoekt_results = self._try_grep_with_zoekt(
                pattern=pattern,
                path=path,
                file_pattern=file_pattern,
                ignore_case=ignore_case,
                max_results=max_results,
                context=context,
            )
            if zoekt_results is not None:
                return zoekt_results
            strategy = SearchStrategy.RUST_BULK

        # Strategy: CACHED_TEXT or opportunistic cached text search
        if strategy == SearchStrategy.CACHED_TEXT or searchable_texts:
            for file_path, text in searchable_texts.items():
                if len(results) >= max_results:
                    break
                for line_num, line in enumerate(text.splitlines(), start=1):
                    if len(results) >= max_results:
                        break
                    match_obj = regex.search(line)
                    if match_obj:
                        results.append(
                            {
                                "file": file_path,
                                "line": line_num,
                                "content": line,
                                "match": match_obj.group(0),
                            }
                        )
            if strategy == SearchStrategy.CACHED_TEXT and len(results) >= max_results:
                return results[:max_results]

        if len(results) >= max_results:
            return results[:max_results]

        # Process remaining files needing raw content
        if not files_needing_raw:
            return results

        remaining_results = max_results - len(results)

        if strategy == SearchStrategy.PARALLEL_POOL:
            results.extend(
                self._grep_parallel(
                    regex=regex,
                    files=files_needing_raw,
                    max_results=remaining_results,
                    context=context,
                )
            )
        elif strategy in (SearchStrategy.RUST_BULK, SearchStrategy.SEQUENTIAL):
            results.extend(
                self._grep_raw_content(
                    regex=regex,
                    pattern=pattern,
                    files_needing_raw=files_needing_raw,
                    strategy=strategy,
                    ignore_case=ignore_case,
                    remaining_results=remaining_results,
                    context=context,
                )
            )

        return results[:max_results]

    # ------------------------------------------------------------------
    # Grep Helpers
    # ------------------------------------------------------------------

    def _grep_raw_content(
        self,
        regex: re.Pattern[str],
        pattern: str,
        files_needing_raw: builtins.list[str],
        strategy: SearchStrategy,
        ignore_case: bool,
        remaining_results: int,
        context: Any,
    ) -> builtins.list[dict[str, Any]]:
        """Process files needing raw content read (mmap, Rust bulk, sequential)."""
        results: builtins.list[dict[str, Any]] = []
        mmap_used = False

        # Try mmap-accelerated grep first (Issue #893)
        if grep_fast.is_mmap_available():
            try:
                from nexus.storage.file_cache import get_file_cache

                zone_id, _, _ = self._extract_zone_info(context)
                if zone_id:
                    file_cache = get_file_cache()
                    disk_paths = file_cache.get_disk_paths_bulk(zone_id, files_needing_raw)
                    if disk_paths:
                        disk_to_virtual = {dp: vp for vp, dp in disk_paths.items()}
                        mmap_results = grep_fast.grep_files_mmap(
                            pattern,
                            list(disk_paths.values()),
                            ignore_case=ignore_case,
                            max_results=remaining_results,
                        )
                        if mmap_results is not None:
                            for match in mmap_results:
                                disk_path = match.get("file", "")
                                match["file"] = disk_to_virtual.get(disk_path, disk_path)
                            results.extend(mmap_results)
                            mmap_used = True
                            files_needing_raw = [
                                f for f in files_needing_raw if f not in disk_paths
                            ]
                            remaining_results = remaining_results - len(results)
            except Exception as e:
                logger.debug(f"[GREP] Mmap optimization failed: {e}")

        # Rust-accelerated grep for remaining
        if (
            strategy == SearchStrategy.RUST_BULK
            and grep_fast.is_available()
            and remaining_results > 0
            and files_needing_raw
        ):
            bulk_results = self._read_bulk(files_needing_raw, context=context, skip_errors=True)
            file_contents: dict[str, bytes] = {
                fp: content
                for fp, content in bulk_results.items()
                if content is not None and isinstance(content, bytes)
            }
            rust_results = grep_fast.grep_bulk(
                pattern,
                file_contents,
                ignore_case=ignore_case,
                max_results=remaining_results,
            )
            if rust_results is not None:
                results.extend(rust_results)

        # Python sequential fallback
        elif not mmap_used and files_needing_raw:
            for file_path in files_needing_raw:
                if len(results) >= remaining_results:
                    break
                try:
                    read_result = self._read(file_path, context=context)
                    if not isinstance(read_result, bytes):
                        continue
                    try:
                        text = read_result.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    for line_num, line in enumerate(text.splitlines(), start=1):
                        if len(results) >= remaining_results:
                            break
                        match_obj = regex.search(line)
                        if match_obj:
                            results.append(
                                {
                                    "file": file_path,
                                    "line": line_num,
                                    "content": line,
                                    "match": match_obj.group(0),
                                }
                            )
                except Exception:
                    continue

        return results

    def _try_grep_with_zoekt(
        self,
        pattern: str,
        path: str,
        file_pattern: str | None,
        ignore_case: bool,
        max_results: int,
        context: Any,
    ) -> builtins.list[dict[str, Any]] | None:
        """Try Zoekt for accelerated grep. Returns None if not available."""
        try:
            from nexus.search.zoekt_client import get_zoekt_client
        except ImportError:
            return None

        client = get_zoekt_client()
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return None
            is_available = loop.run_until_complete(client.is_available())
        except RuntimeError:
            is_available = asyncio.run(client.is_available())

        if not is_available:
            return None

        try:
            zoekt_query = pattern
            if ignore_case:
                zoekt_query = f"(?i){pattern}"
            if path and path != "/":
                zoekt_query = f"file:{path.lstrip('/')}/ {zoekt_query}"

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    return None
                matches = loop.run_until_complete(client.search(zoekt_query, num=max_results * 3))
            except RuntimeError:
                matches = asyncio.run(client.search(zoekt_query, num=max_results * 3))

            if not matches:
                return None

            if file_pattern:
                matches = [m for m in matches if glob_fast.glob_match(m.file, [file_pattern])]

            unique_files = list({m.file for m in matches})
            if self._permission_enforcer and context:
                permitted_files = set(self._permission_enforcer.filter_list(unique_files, context))
            else:
                permitted_files = set(unique_files)

            results = []
            for match in matches:
                if match.file in permitted_files:
                    results.append(
                        {
                            "file": match.file,
                            "line": match.line,
                            "content": match.content,
                            "match": match.match,
                        }
                    )
                    if len(results) >= max_results:
                        break
            return results
        except Exception as e:
            logger.warning(f"[GREP] Zoekt search failed: {e}")
            return None

    def _try_grep_with_trigram(
        self,
        pattern: str,
        ignore_case: bool,
        max_results: int,
        zone_id: str,
        context: Any = None,
    ) -> builtins.list[dict[str, Any]] | None:
        """Try trigram index for accelerated grep (Issue #954).

        Uses trigram index for O(1) candidate lookup, then verifies candidates
        by reading content through NexusFS (supporting CAS backends).

        Returns None if trigram index is not available or on error,
        allowing fallback to other strategies.
        """
        if not trigram_fast.is_available():
            return None

        index_path = trigram_fast.get_index_path(zone_id)
        if not os.path.isfile(index_path):
            return None

        # Phase 1: Get candidate file paths from trigram index (sub-ms).
        candidates = trigram_fast.search_candidates(
            index_path=index_path,
            pattern=pattern,
            ignore_case=ignore_case,
        )

        if candidates is None:
            logger.warning("[GREP] Trigram candidate search failed, falling back")
            return None

        if not candidates:
            logger.debug("[GREP] Issue #954: Trigram index found 0 candidates for zone=%s", zone_id)
            return []

        logger.debug(
            "[GREP] Issue #954: Trigram index found %d candidates for zone=%s",
            len(candidates),
            zone_id,
        )

        # Phase 2: Verify candidates by reading content through NexusFS.
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            return None

        results: builtins.list[dict[str, Any]] = []
        for file_path in candidates:
            if len(results) >= max_results:
                break
            try:
                content = self._read(file_path, context=context)
                if not isinstance(content, bytes):
                    continue
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                for line_num, line in enumerate(text.splitlines(), start=1):
                    if len(results) >= max_results:
                        break
                    match_obj = regex.search(line)
                    if match_obj:
                        results.append(
                            {
                                "file": file_path,
                                "line": line_num,
                                "content": line,
                                "match": match_obj.group(0),
                            }
                        )
            except Exception:
                continue

        return results

    def build_trigram_index_for_zone(
        self,
        zone_id: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Build trigram index for all files in a zone (Issue #954).

        Reads file content through NexusFS (supporting CAS backends) and
        builds the index using (virtual_path, content) pairs.

        Args:
            zone_id: Zone identifier.
            context: Operation context for permission filtering.

        Returns:
            Dict with status, file_count, trigram_count, index_size_bytes.
        """
        if not trigram_fast.is_available():
            return {"status": "unavailable", "reason": "Rust extension not available"}

        # List all files in the zone.
        files = cast(list[str], self.list("/", recursive=True, context=context))

        index_path = trigram_fast.get_index_path(zone_id)
        os.makedirs(os.path.dirname(index_path), exist_ok=True)

        # Read content through NexusFS and build index from (path, content) pairs.
        # This works with any backend (CAS, S3, etc.) since we read through the
        # NexusFS abstraction rather than directly from disk.
        entries: builtins.list[tuple[str, bytes]] = []
        for file_path in files:
            try:
                content = self._read(file_path, context=context)
                if isinstance(content, bytes):
                    entries.append((file_path, content))
            except Exception:
                continue  # Skip unreadable files.

        success = trigram_fast.build_index_from_entries(entries, index_path)
        if not success:
            return {"status": "error", "reason": "Index build failed"}

        stats = trigram_fast.get_stats(index_path)
        return {
            "status": "ok",
            "index_path": index_path,
            **(stats or {}),
        }

    def get_trigram_index_status(self, zone_id: str) -> dict[str, Any]:
        """Get trigram index status for a zone (Issue #954)."""
        if not trigram_fast.is_available():
            return {"status": "unavailable"}

        index_path = trigram_fast.get_index_path(zone_id)
        if not os.path.isfile(index_path):
            return {"status": "not_built", "index_path": index_path}

        stats = trigram_fast.get_stats(index_path)
        if stats is None:
            return {"status": "error", "index_path": index_path}

        return {"status": "ok", "index_path": index_path, **stats}

    def invalidate_trigram_index(self, zone_id: str) -> None:
        """Delete trigram index for a zone and clear cache (Issue #954)."""
        index_path = trigram_fast.get_index_path(zone_id)
        trigram_fast.invalidate_cache(index_path)
        if os.path.isfile(index_path):
            os.remove(index_path)

    def _grep_parallel(
        self,
        regex: re.Pattern[str],
        files: builtins.list[str],
        max_results: int,
        context: Any,
    ) -> builtins.list[dict[str, Any]]:
        """Parallel grep using ThreadPoolExecutor (Issue #929).

        Each worker searches its chunk independently. Results are merged and
        truncated to ``max_results`` in the main thread.
        """
        from nexus.utils.timing import Timer

        timer = Timer()
        timer.__enter__()

        chunk_size = max(1, len(files) // GREP_PARALLEL_WORKERS)
        file_chunks = [files[i : i + chunk_size] for i in range(0, len(files), chunk_size)]

        def search_chunk(chunk_files: builtins.list[str]) -> builtins.list[dict[str, Any]]:
            chunk_results: builtins.list[dict[str, Any]] = []
            for file_path in chunk_files:
                try:
                    read_result = self._read(file_path, context=context)
                    if not isinstance(read_result, bytes):
                        continue
                    try:
                        text = read_result.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    for line_num, line in enumerate(text.splitlines(), start=1):
                        match_obj = regex.search(line)
                        if match_obj:
                            chunk_results.append(
                                {
                                    "file": file_path,
                                    "line": line_num,
                                    "content": line,
                                    "match": match_obj.group(0),
                                }
                            )
                            if len(chunk_results) >= max_results:
                                break
                except Exception:
                    continue
            return chunk_results

        all_results: builtins.list[dict[str, Any]] = []
        executor = self._get_thread_pool()
        futures = [executor.submit(search_chunk, chunk) for chunk in file_chunks]
        for future in futures:
            try:
                chunk_results = future.result(timeout=30)
                all_results.extend(chunk_results)
                if len(all_results) >= max_results:
                    break
            except Exception as e:
                logger.debug(f"[GREP-PARALLEL] Chunk failed: {e}")

        timer.__exit__(None, None, None)
        logger.debug(
            f"[GREP-PARALLEL] {len(files)} files, {len(all_results)} results "
            f"in {timer.elapsed:.3f}s"
        )
        return all_results[:max_results]
