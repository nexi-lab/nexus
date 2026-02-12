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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from nexus.core.context_manifest.models import (
    ManifestResolutionError,
    ManifestResult,
    SourceResult,
)
from nexus.core.context_manifest.template import resolve_template

logger = logging.getLogger(__name__)

# Field names used to extract a human-readable name from source models.
# Shared between _validate_templates() and _get_source_name() to avoid
# DRY violations (review HIGH-2).
_SOURCE_NAME_FIELDS = ("tool_name", "snapshot_id", "pattern", "query")


# ===========================================================================
# SourceExecutor Protocol
# ===========================================================================


@runtime_checkable
class SourceExecutor(Protocol):
    """Protocol for executing a single context source.

    Implementations handle the actual work: calling MCP tools, reading files,
    querying memory, loading snapshots, etc. Each source type gets its own
    executor, injected into the resolver at construction time.
    """

    async def execute(
        self,
        source: Any,
        variables: dict[str, str],
    ) -> SourceResult: ...


# ===========================================================================
# ManifestResolver
# ===========================================================================


class ManifestResolver:
    """Resolves a context manifest by executing all sources in parallel.

    Pipeline:
        1. Validate template variables in all sources (fail-fast).
        2. Execute sources in parallel with ``asyncio.gather``.
        3. Apply per-source timeout via ``asyncio.wait_for()``.
        4. Apply global timeout via ``asyncio.timeout()``.
        5. Truncate results exceeding ``max_result_bytes``.
        6. Write individual result files to ``output_dir/``.
        7. Write ``_index.json`` last (sentinel for crash safety).
        8. Return ``ManifestResult``.

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
    ) -> None:
        if max_resolve_seconds <= 0:
            msg = f"max_resolve_seconds must be positive, got {max_resolve_seconds}"
            raise ValueError(msg)
        self._executors = dict(executors)  # defensive copy
        self._max_resolve_seconds = max_resolve_seconds

    async def resolve(
        self,
        sources: Sequence[Any],
        variables: dict[str, str],
        output_dir: Path,
    ) -> ManifestResult:
        """Resolve all sources and write results to *output_dir*.

        Args:
            sources: Sequence of ContextSource models to resolve.
            variables: Template variable values for substitution.
            output_dir: Directory to write result files into.

        Returns:
            ManifestResult with all source results.

        Raises:
            ManifestResolutionError: If any required source fails.
            ValueError: If template variables are invalid.
        """
        start = time.monotonic()
        logger.info(
            "Starting manifest resolution: %d sources, max_timeout=%.1fs",
            len(sources),
            self._max_resolve_seconds,
        )

        # Step 1: Validate templates in all sources (fail-fast before execution)
        self._validate_templates(sources, variables)

        # Step 2-4: Execute all sources with per-source + global timeout
        results = await self._execute_all(sources, variables)

        # Step 5: Apply truncation
        results = self._apply_truncation(sources, results)

        elapsed_ms = (time.monotonic() - start) * 1000
        resolved_at = datetime.now(timezone.utc).isoformat()

        manifest_result = ManifestResult(
            sources=tuple(results),
            resolved_at=resolved_at,
            total_ms=elapsed_ms,
        )

        # Step 6-7: Write result files + _index.json (last)
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_results(output_dir, sources, results, manifest_result)

        # Step 8: Check for required failures
        failed_required = [
            r
            for r, s in zip(results, sources)
            if getattr(s, "required", True)
            and r.status in ("error", "timeout", "skipped")
        ]
        if failed_required:
            raise ManifestResolutionError(failed_sources=tuple(failed_required))

        success_count = sum(1 for r in results if r.status in ("ok", "truncated"))
        logger.info(
            "Manifest resolved: %d/%d sources succeeded in %.2fms",
            success_count,
            len(sources),
            elapsed_ms,
        )

        return manifest_result

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _validate_templates(
        self, sources: Sequence[Any], variables: dict[str, str]
    ) -> None:
        """Validate template variables in all sources before execution."""
        for source in sources:
            for field_name in _SOURCE_NAME_FIELDS:
                value = getattr(source, field_name, None)
                if value and "{{" in str(value):
                    # This will raise ValueError if invalid
                    resolve_template(str(value), variables)

    async def _execute_all(
        self,
        sources: Sequence[Any],
        variables: dict[str, str],
    ) -> list[SourceResult]:
        """Execute all sources in parallel with timeouts."""
        if not sources:
            return []

        # CRITICAL-1 fix: Use asyncio.create_task() so cancellation works
        # when global timeout fires. Plain coroutines can't be cancelled.
        tasks = [
            asyncio.create_task(self._execute_one(source, variables))
            for source in sources
        ]

        try:
            async with asyncio.timeout(self._max_resolve_seconds):
                results = await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            # Global timeout hit — cancel remaining tasks
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Wait for cancellations to complete gracefully
            await asyncio.gather(*tasks, return_exceptions=True)
            return await self._collect_timeout_results(sources)

        # Process gather results
        processed: list[SourceResult] = []
        for i, result in enumerate(results):
            if isinstance(result, SourceResult):
                processed.append(result)
            elif isinstance(result, Exception):
                name = self._get_source_name(sources[i])
                processed.append(
                    SourceResult(
                        source_type=sources[i].type,
                        source_name=name,
                        status="error",
                        data=None,
                        error_message=str(result),
                    )
                )
            else:
                name = self._get_source_name(sources[i])
                processed.append(
                    SourceResult(
                        source_type=sources[i].type,
                        source_name=name,
                        status="error",
                        data=None,
                        error_message=f"Unexpected result type: {type(result)}",
                    )
                )
        return processed

    async def _execute_one(
        self, source: Any, variables: dict[str, str]
    ) -> SourceResult:
        """Execute a single source with its per-source timeout."""
        source_type = source.type
        name = self._get_source_name(source)

        executor = self._executors.get(source_type)
        if executor is None:
            logger.warning("No executor registered for source type %r", source_type)
            return SourceResult(
                source_type=source_type,
                source_name=name,
                status="skipped",
                data=None,
                error_message=f"No executor for source type '{source_type}'",
            )

        timeout = getattr(source, "timeout_seconds", 30.0)
        start = time.monotonic()

        try:
            result = await asyncio.wait_for(
                executor.execute(source, variables),
                timeout=timeout,
            )
            return result
        except TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000
            return SourceResult(
                source_type=source_type,
                source_name=name,
                status="timeout",
                data=None,
                error_message=f"Source timed out after {timeout}s",
                elapsed_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return SourceResult(
                source_type=source_type,
                source_name=name,
                status="error",
                data=None,
                error_message=str(exc),
                elapsed_ms=elapsed_ms,
            )

    async def _collect_timeout_results(
        self, sources: Sequence[Any]
    ) -> list[SourceResult]:
        """Build timeout results for all sources when global timeout fires."""
        return [
            SourceResult(
                source_type=s.type,
                source_name=self._get_source_name(s),
                status="timeout",
                data=None,
                error_message="Global resolution timeout exceeded",
            )
            for s in sources
        ]

    def _apply_truncation(
        self, sources: Sequence[Any], results: list[SourceResult]
    ) -> list[SourceResult]:
        """Truncate results that exceed max_result_bytes."""
        truncated: list[SourceResult] = []
        for source, result in zip(sources, results):
            max_bytes = getattr(source, "max_result_bytes", 1_048_576)
            if result.status == "ok" and result.data is not None:
                data_str = str(result.data)
                data_size = len(data_str.encode("utf-8"))
                if data_size > max_bytes:
                    cut_data = data_str.encode("utf-8")[:max_bytes].decode(
                        "utf-8", errors="ignore"
                    )
                    truncated.append(
                        SourceResult(
                            source_type=result.source_type,
                            source_name=result.source_name,
                            status="truncated",
                            data=cut_data,
                            error_message=f"Result truncated from {data_size} to {max_bytes} bytes",
                            elapsed_ms=result.elapsed_ms,
                        )
                    )
                    continue
            truncated.append(result)
        return truncated

    def _write_results(
        self,
        output_dir: Path,
        sources: Sequence[Any],
        results: list[SourceResult],
        manifest_result: ManifestResult,
    ) -> None:
        """Write individual result files and _index.json to output_dir."""
        index_entries: list[dict[str, Any]] = []

        for _i, (source, result) in enumerate(zip(sources, results)):
            # Generate a safe filename
            name = self._get_source_name(source)
            safe_name = _sanitize_filename(name)
            base_filename = f"{source.type}__{safe_name}"

            # MEDIUM-5 fix: Robust duplicate detection with counter loop
            filename = base_filename
            existing = {e["file"] for e in index_entries}
            counter = 1
            while f"{filename}.json" in existing:
                filename = f"{base_filename}_{counter}"
                counter += 1
            filename = f"{filename}.json"

            # Write result file
            result_data = {
                "source_type": result.source_type,
                "source_name": result.source_name,
                "status": result.status,
                "data": result.data,
                "error_message": result.error_message,
                "elapsed_ms": result.elapsed_ms,
            }
            (output_dir / filename).write_text(
                json.dumps(result_data, indent=2, default=str), encoding="utf-8"
            )

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
        (output_dir / "_index.json").write_text(
            json.dumps(index_data, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _get_source_name(source: Any) -> str:
        """Extract the human-readable name from a source model."""
        for attr in _SOURCE_NAME_FIELDS:
            value = getattr(source, attr, None)
            if value is not None:
                return str(value)
        return "unknown"


def _sanitize_filename(name: str) -> str:
    """Convert a source name to a safe filename component.

    Hardened against path traversal (CRITICAL-2): normalizes Unicode,
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
