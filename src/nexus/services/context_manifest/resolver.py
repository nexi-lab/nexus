"""Manifest resolver with parallel execution and Protocol-based DI (Issue #1341).

Implements the Stripe Minions "deterministic pre-execution" pattern:
all sources are resolved in parallel before the agent starts reasoning.

Key design decisions (from plan):
    - Decision #1: Self-contained asyncio.gather (no external batch endpoint)
    - Decision #6: Per-source ``required`` field + SourceResult status tracking
    - Decision #9: Protocol-based SourceExecutor injection for testability
    - Decision #13: Per-source timeout + global max_resolve_seconds
    - Decision #14: Per-source max_result_bytes with truncation
    - Decision #16: _index.json written last as sentinel

References:
    - Issue #1341: Context manifest with deterministic pre-execution
    - Stripe Minions: https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiofiles

from nexus.services.context_manifest.models import (
    ContextSourceProtocol,
    ManifestResolutionError,
    ManifestResult,
    SourceResult,
)
from nexus.services.context_manifest.template import resolve_template

logger = logging.getLogger(__name__)


# ===========================================================================
# SourceExecutor Protocol
# ===========================================================================


@runtime_checkable
class SourceExecutor(Protocol):
    """Protocol for executing a single context source.

    Implementations handle the actual work: calling MCP tools, reading files,
    querying memory, loading snapshots, etc. Each source type gets its own
    executor, injected into the resolver at construction time.

    The ``variables`` dict contains pre-resolved template values. Executors
    should use these to substitute any template references in source fields
    (e.g., ``source.query`` may contain ``{{task.description}}``).
    """

    async def execute(
        self,
        source: ContextSourceProtocol,
        variables: dict[str, str],
    ) -> SourceResult: ...


@runtime_checkable
class MetricsObserver(Protocol):
    """Protocol for observing manifest resolution metrics.

    Implementations receive hooks during resolution lifecycle:
    - ``on_resolution_start()``: called once at the start of each resolution.
    - ``on_source_complete()``: called per source after execution and truncation.
    - ``on_resolution_end()``: called in finally block (always fires, even on error).

    Note: If template pre-resolution fails (``ValueError``), ``on_source_complete``
    is never called because no sources were executed. ``on_resolution_end`` is still
    called with ``error=True``.
    """

    def on_resolution_start(self) -> None: ...

    def on_source_complete(
        self,
        source_type: str,
        source_name: str,
        status: str,
        elapsed_ms: float,
    ) -> None: ...

    def on_resolution_end(
        self,
        elapsed_ms: float,
        source_count: int,
        error: bool = False,
    ) -> None: ...


# ===========================================================================
# ManifestResolver
# ===========================================================================


class ManifestResolver:
    """Resolves a context manifest by executing all sources in parallel.

    Pipeline:
        1. Pre-resolve template variables in all sources (fail-fast, single pass).
        2. Execute sources in parallel with ``asyncio.gather``.
        3. Apply per-source timeout via ``asyncio.wait_for()``.
        4. Apply global timeout via ``asyncio.timeout()``.
        5. Truncate results exceeding ``max_result_bytes``.
        6. Write individual result files to ``output_dir/`` (async via aiofiles).
        7. Write ``_index.json`` last (sentinel for crash safety).
        8. Return ``ManifestResult``.

    Note:
        Output files are written even when ``ManifestResolutionError`` is
        raised, allowing callers to inspect partial results on disk.

    Args:
        executors: Mapping of source type name → executor implementation.
            If a source type has no registered executor, it is skipped (or
            raises ``ManifestResolutionError`` if required).
        max_resolve_seconds: Global timeout for the entire resolution.
            Whichever fires first (per-source or global) wins. Must be positive.
    """

    def __init__(
        self,
        executors: dict[str, SourceExecutor],
        *,
        max_resolve_seconds: float = 5.0,
        metrics_observer: MetricsObserver | None = None,
    ) -> None:
        if max_resolve_seconds <= 0:
            msg = f"max_resolve_seconds must be positive, got {max_resolve_seconds}"
            raise ValueError(msg)
        self._executors = dict(executors)  # defensive copy
        self._max_resolve_seconds = max_resolve_seconds
        self._metrics = metrics_observer

    def with_executors(self, extra: dict[str, SourceExecutor]) -> ManifestResolver:
        """Return a new resolver with additional executors merged in.

        Existing executors are preserved; *extra* executors override on conflict.
        The new resolver shares the same config (timeout, metrics).

        This enables per-request executor injection (e.g., binding a
        MemoryQueryExecutor to the requesting agent's Memory instance)
        without mutating the shared resolver.

        Args:
            extra: Additional executors to merge. Keys are source type names.

        Returns:
            New ManifestResolver with merged executors.
        """
        merged = {**self._executors, **extra}
        return ManifestResolver(
            executors=merged,
            max_resolve_seconds=self._max_resolve_seconds,
            metrics_observer=self._metrics,
        )

    async def resolve(
        self,
        sources: Sequence[ContextSourceProtocol],
        variables: dict[str, str],
        output_dir: Path | None = None,
    ) -> ManifestResult:
        """Resolve all sources and optionally write results to *output_dir*.

        Args:
            sources: Sequence of ContextSource models to resolve.
            variables: Template variable values for substitution.
            output_dir: Directory to write result files into. If None,
                no files are written — results are returned in-memory only.
                This avoids unnecessary I/O when the caller only needs
                the ManifestResult (e.g., API resolve endpoint).

        Returns:
            ManifestResult with all source results.

        Raises:
            ManifestResolutionError: If any required source fails.
                Output files are still written before this is raised,
                allowing callers to inspect partial results on disk.
            ValueError: If template variables are invalid.
        """
        start = time.monotonic()
        logger.info(
            "Starting manifest resolution: %d sources, max_timeout=%.1fs",
            len(sources),
            self._max_resolve_seconds,
        )

        # Metrics: signal resolution start
        if self._metrics is not None:
            self._metrics.on_resolution_start()

        has_error = False
        try:
            # Step 1: Pre-resolve templates in all sources (fail-fast, single pass — ARCH-1)
            resolved_vars = self._pre_resolve_templates(sources, variables)

            # Step 2-4: Execute all sources with per-source + global timeout
            results = await self._execute_all(sources, resolved_vars)

            # Step 5: Apply truncation
            results = self._apply_truncation(sources, results)

            # Metrics: record per-source metrics after truncation
            if self._metrics is not None:
                for r in results:
                    self._metrics.on_source_complete(
                        source_type=r.source_type,
                        source_name=r.source_name,
                        status=r.status,
                        elapsed_ms=r.elapsed_ms,
                    )

            elapsed_ms = (time.monotonic() - start) * 1000
            resolved_at = datetime.now(UTC).isoformat()

            manifest_result = ManifestResult(
                sources=tuple(results),
                resolved_at=resolved_at,
                total_ms=elapsed_ms,
            )

            # Step 6-7: Write result files + _index.json (last) — skip if no output_dir (15A)
            if output_dir is not None:
                output_dir.mkdir(parents=True, exist_ok=True)
                await self._write_results(output_dir, sources, results, manifest_result)

            # Step 8: Check for required failures
            failed_required = [
                r
                for r, s in zip(results, sources, strict=True)
                if s.required and r.status in ("error", "timeout", "skipped")
            ]
            if failed_required:
                has_error = True
                raise ManifestResolutionError(failed_sources=tuple(failed_required))

            success_count = sum(1 for r in results if r.status in ("ok", "truncated"))
            logger.info(
                "Manifest resolved: %d/%d sources succeeded in %.2fms",
                success_count,
                len(sources),
                elapsed_ms,
            )

            return manifest_result
        except ManifestResolutionError:
            raise
        except Exception:
            has_error = True
            raise
        finally:
            # Metrics: always signal resolution end
            if self._metrics is not None:
                final_ms = (time.monotonic() - start) * 1000
                self._metrics.on_resolution_end(
                    elapsed_ms=final_ms,
                    source_count=len(sources),
                    error=has_error,
                )

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _pre_resolve_templates(
        self,
        sources: Sequence[ContextSourceProtocol],
        variables: dict[str, str],
    ) -> dict[str, str]:
        """Validate and resolve all template variables in a single pass (ARCH-1).

        Scans all source fields that may contain templates, validates them
        against the whitelist, and returns the validated variables dict.
        Raises ValueError on any invalid or missing template variable.

        Returns:
            The validated variables dict (same reference if all is well).
        """
        for source in sources:
            name = source.source_name
            if name and "{{" in name:
                resolve_template(name, variables)
        return variables

    async def _execute_all(
        self,
        sources: Sequence[ContextSourceProtocol],
        variables: dict[str, str],
    ) -> list[SourceResult]:
        """Execute all sources in parallel with timeouts."""
        if not sources:
            return []

        # Use asyncio.create_task() so cancellation works when global timeout fires.
        tasks = [asyncio.create_task(self._execute_one(source, variables)) for source in sources]

        try:
            async with asyncio.timeout(self._max_resolve_seconds):
                results = await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            # Global timeout hit — collect completed results, mark remainder as timeout
            return self._collect_partial_results(sources, tasks)

        # Process gather results
        processed: list[SourceResult] = []
        for i, result in enumerate(results):
            if isinstance(result, SourceResult):
                processed.append(result)
            elif isinstance(result, Exception):
                processed.append(
                    SourceResult.error(
                        source_type=sources[i].type,
                        source_name=sources[i].source_name,
                        error_message=str(result),
                    )
                )
            else:
                processed.append(
                    SourceResult.error(
                        source_type=sources[i].type,
                        source_name=sources[i].source_name,
                        error_message=f"Unexpected result type: {type(result)}",
                    )
                )
        return processed

    async def _execute_one(
        self, source: ContextSourceProtocol, variables: dict[str, str]
    ) -> SourceResult:
        """Execute a single source with its per-source timeout."""
        source_type = source.type
        name = source.source_name

        executor = self._executors.get(source_type)
        if executor is None:
            logger.warning("No executor registered for source type %r", source_type)
            return SourceResult.skipped(
                source_type=source_type,
                source_name=name,
                error_message=f"No executor for source type '{source_type}'",
            )

        timeout = source.timeout_seconds
        start = time.monotonic()

        try:
            result = await asyncio.wait_for(
                executor.execute(source, variables),
                timeout=timeout,
            )
            return result
        except TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000
            return SourceResult.timeout(
                source_type=source_type,
                source_name=name,
                error_message=f"Source timed out after {timeout}s",
                elapsed_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return SourceResult.error(
                source_type=source_type,
                source_name=name,
                error_message=str(exc),
                elapsed_ms=elapsed_ms,
            )

    def _collect_partial_results(
        self,
        sources: Sequence[ContextSourceProtocol],
        tasks: list[asyncio.Task[SourceResult]],
    ) -> list[SourceResult]:
        """Collect results after global timeout, preserving completed tasks (TEST-4 fix).

        Tasks that completed before the global timeout keep their results.
        Tasks that are still running are cancelled and marked as timeout.
        """
        results: list[SourceResult] = []
        for source, task in zip(sources, tasks, strict=True):
            if task.done() and not task.cancelled():
                try:
                    result = task.result()
                    if isinstance(result, SourceResult):
                        results.append(result)
                    else:
                        results.append(
                            SourceResult.error(
                                source_type=source.type,
                                source_name=source.source_name,
                                error_message=f"Unexpected result type: {type(result)}",
                            )
                        )
                except Exception as exc:
                    results.append(
                        SourceResult.error(
                            source_type=source.type,
                            source_name=source.source_name,
                            error_message=str(exc),
                        )
                    )
            else:
                if not task.done():
                    task.cancel()
                results.append(
                    SourceResult.timeout(
                        source_type=source.type,
                        source_name=source.source_name,
                        error_message="Global resolution timeout exceeded",
                    )
                )
        return results

    def _apply_truncation(
        self,
        sources: Sequence[ContextSourceProtocol],
        results: list[SourceResult],
    ) -> list[SourceResult]:
        """Truncate results that exceed max_result_bytes (PERF-1: json.dumps + single encode)."""
        truncated: list[SourceResult] = []
        for source, result in zip(sources, results, strict=True):
            max_bytes = source.max_result_bytes
            if result.status == "ok" and result.data is not None:
                serialized = json.dumps(result.data, default=str).encode("utf-8")
                if len(serialized) > max_bytes:
                    cut_data = serialized[:max_bytes].decode("utf-8", errors="ignore")
                    truncated.append(
                        SourceResult.truncated(
                            source_type=result.source_type,
                            source_name=result.source_name,
                            data=cut_data,
                            error_message=f"Result truncated from {len(serialized)} to {max_bytes} bytes",
                            elapsed_ms=result.elapsed_ms,
                        )
                    )
                    continue
            truncated.append(result)
        return truncated

    async def _write_results(
        self,
        output_dir: Path,
        sources: Sequence[ContextSourceProtocol],
        results: list[SourceResult],
        manifest_result: ManifestResult,
    ) -> None:
        """Write individual result files and _index.json to output_dir (async via aiofiles)."""
        index_entries: list[dict[str, Any]] = []
        existing_files: set[str] = set()

        for source, result in zip(sources, results, strict=True):
            # Generate a safe filename
            safe_name = _sanitize_filename(source.source_name)
            base_filename = f"{source.type}__{safe_name}"

            # Robust duplicate detection with counter loop
            filename = base_filename
            counter = 1
            while f"{filename}.json" in existing_files:
                filename = f"{base_filename}_{counter}"
                counter += 1
            filename = f"{filename}.json"
            existing_files.add(filename)

            # Write result file
            result_data = {
                "source_type": result.source_type,
                "source_name": result.source_name,
                "status": result.status,
                "data": result.data,
                "error_message": result.error_message,
                "elapsed_ms": result.elapsed_ms,
            }
            async with aiofiles.open(output_dir / filename, "w", encoding="utf-8") as f:
                await f.write(json.dumps(result_data, indent=2, default=str))

            index_entries.append(
                {
                    "source_type": result.source_type,
                    "source_name": result.source_name,
                    "status": result.status,
                    "file": filename,
                    "elapsed_ms": result.elapsed_ms,
                }
            )

        # Write _index.json LAST (sentinel — Decision #16)
        index_data = {
            "resolved_at": manifest_result.resolved_at,
            "total_ms": manifest_result.total_ms,
            "source_count": len(results),
            "sources": index_entries,
        }
        async with aiofiles.open(output_dir / "_index.json", "w", encoding="utf-8") as f:
            await f.write(json.dumps(index_data, indent=2))


def _sanitize_filename(name: str) -> str:
    """Convert a source name to a safe filename component.

    Hardened against path traversal: normalizes Unicode,
    strips all dangerous characters, allows only alphanumeric + underscore + hyphen.
    """
    # Normalize Unicode to prevent lookalike attacks
    safe = unicodedata.normalize("NFKC", name)
    # Replace path separators and dangerous characters
    safe = safe.replace("..", "_")
    # Only allow alphanumeric, underscore, hyphen
    safe = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in safe)
    # Strip leading/trailing underscores and dots
    safe = safe.strip("_.")
    # Limit length
    if len(safe) > 50:
        safe = safe[:50]
    return safe or "unnamed"
