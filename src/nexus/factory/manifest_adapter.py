"""Factory adapter for context manifest MCP tool (Issue #2984).

Builds a callable that the MCP tool invokes without cross-brick imports.
All context_manifest imports are contained in this factory module (not a brick).
"""

import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def build_manifest_resolve_fn(
    resolver: Any,
    nx_instance: Any,
) -> Callable[[str, str], dict[str, Any]]:
    """Build a resolve function for the nexus_resolve_context MCP tool.

    The returned callable accepts (sources_json, variables_json) and returns
    a result dict. All context_manifest imports are encapsulated here so the
    MCP brick never imports from the context_manifest brick directly.

    Args:
        resolver: ManifestResolver instance (from factory boot).
        nx_instance: NexusFS instance (for per-request memory wiring).

    Returns:
        Callable that resolves context manifest sources.
    """
    from pydantic import TypeAdapter, ValidationError

    from nexus.bricks.context_manifest.models import ContextSource
    from nexus.lib.sync_bridge import run_sync

    adapter: TypeAdapter[ContextSource] = TypeAdapter(ContextSource)

    def _wire_memory_executor(base_resolver: Any) -> Any:
        """Wire per-request MemoryQueryExecutor if memory is available."""
        mem_provider = getattr(nx_instance, "_memory_provider", None)
        memory = mem_provider.get_for_context() if mem_provider else None
        if memory is None:
            return base_resolver
        try:
            from nexus.bricks.context_manifest.executors.memory_query import (
                MemoryQueryExecutor,
            )
            from nexus.bricks.context_manifest.executors.memory_search_adapter import (
                MemorySearchAdapter,
            )

            mem_adapter = MemorySearchAdapter(memory=memory)
            mem_executor = MemoryQueryExecutor(memory_search=mem_adapter)
            return base_resolver.with_executors({"memory_query": mem_executor})
        except Exception:
            logger.warning("MemoryQueryExecutor wiring failed", exc_info=True)
            return base_resolver

    def resolve_context(sources_json: str, variables_json: str) -> dict[str, Any]:
        """Resolve context manifest sources.

        Args:
            sources_json: JSON array of context source definitions.
            variables_json: JSON object of template variable values.

        Returns:
            Dict with resolved_at, total_ms, source_count, sources.

        Raises:
            ValueError: On invalid input.
            Exception: On resolution failure.
        """
        # Parse inputs
        try:
            sources_list = json.loads(sources_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in sources: {e}") from e

        if not isinstance(sources_list, list):
            raise ValueError("sources must be a JSON array")

        if not sources_list:
            raise ValueError("sources array must not be empty")

        try:
            variables_dict = json.loads(variables_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in variables: {e}") from e

        if not isinstance(variables_dict, dict):
            raise ValueError("variables must be a JSON object")

        # Validate sources via Pydantic
        pydantic_sources = []
        for i, src in enumerate(sources_list):
            try:
                pydantic_sources.append(adapter.validate_python(src))
            except ValidationError as exc:
                raise ValueError(f"Invalid source at index {i}: {exc.errors()}") from exc

        # Wire memory and resolve
        request_resolver = _wire_memory_executor(resolver)
        result = run_sync(request_resolver.resolve(pydantic_sources, variables_dict))

        return {
            "resolved_at": result.resolved_at,
            "total_ms": result.total_ms,
            "source_count": len(result.sources),
            "sources": [
                {
                    "source_type": sr.source_type,
                    "source_name": sr.source_name,
                    "status": sr.status,
                    "data": sr.data,
                    "error_message": sr.error_message,
                    "elapsed_ms": sr.elapsed_ms,
                }
                for sr in result.sources
            ],
        }

    return resolve_context
