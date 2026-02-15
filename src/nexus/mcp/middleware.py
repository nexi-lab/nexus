"""MCP tool namespace middleware — per-tool ReBAC filtering (Issue #1272).

Implements two-layer defense (MiniScope pattern):
    1. ``on_list_tools``: Filter ``tools/list`` response — invisible tools are
       removed BEFORE the LLM sees them.
    2. ``on_call_tool``: Validate ``tools/call`` invocation — reject calls to
       tools the agent has no grant for (defense-in-depth).

Architecture:
    Session start → ``rebac_list_objects()`` filtered to ``/tools/`` →
    ``frozenset[str]`` cached with revision quantization →
    ``on_list_tools`` / ``on_call_tool`` do O(1) set membership checks.

Design decisions (approved in review):
    - Unified ``/tools/`` namespace prefix (Decision 1C)
    - Middleware-only enforcement (Decision 4B)
    - Session-start batch + local cache (Decision 13C)
    - Coarse mount table + fine-grained grant check (Decision 11A)
    - Invisible tools return "not found" (namespace-as-security)

References:
    - Issue #1272: MCP tool-level namespace granularity
    - MiniScope (arXiv 2512.11147): mechanical enforcement > prompt-based
    - Cerbos: batch authorization at session start
    - Kong: tool filtering at gateway level
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from cachetools import TTLCache
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool, ToolResult

from nexus.mcp.profiles import TOOL_PATH_PREFIX
from nexus.mcp.tool_utils import tool_error

if TYPE_CHECKING:
    import mcp.types as mt

    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager

logger = logging.getLogger(__name__)


class ToolNamespaceMiddleware(Middleware):
    """Filter MCP tools by per-subject ReBAC grants.

    Intercepts ``tools/list`` to remove invisible tools and ``tools/call``
    to reject unauthorized invocations. Uses a TTL-cached tool set per
    subject, refreshed via zone revision quantization.

    Args:
        rebac_manager: ReBAC manager for querying tool grants.
        zone_id: Default zone ID for multi-zone isolation.
        cache_maxsize: Maximum cached subjects (default: 10,000).
        cache_ttl: Cache TTL in seconds (default: 300).
        revision_window: Zone revision quantization bucket size (default: 10).
        enabled: Master switch (default: True). When False, all tools are visible.
    """

    def __init__(
        self,
        rebac_manager: EnhancedReBACManager,
        zone_id: str | None = None,
        cache_maxsize: int = 10_000,
        cache_ttl: int = 300,
        revision_window: int = 10,
        enabled: bool = True,
    ) -> None:
        self._rebac_manager = rebac_manager
        self._zone_id = zone_id
        self._revision_window = revision_window
        self._enabled = enabled

        # Cache: (subject_type, subject_id, zone_id, revision_bucket) → frozenset[tool_name]
        self._tool_cache: TTLCache[tuple[str, str, str | None, int], frozenset[str]] = TTLCache(
            maxsize=cache_maxsize, ttl=cache_ttl
        )
        self._lock = threading.Lock()

        # Metrics
        self._cache_hits = 0
        self._cache_misses = 0
        self._tools_filtered = 0
        self._calls_rejected = 0
        self._rebac_errors = 0

    # ------------------------------------------------------------------
    # Middleware hooks
    # ------------------------------------------------------------------

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Filter tools/list response to only include visible tools."""
        all_tools = await call_next(context)

        if not self._enabled:
            return all_tools

        subject = self._extract_subject(context)
        if subject is None:
            # No subject identity → return all tools (backward compat / admin)
            return all_tools

        visible_tool_names = self._get_visible_tools(subject)
        filtered = [t for t in all_tools if t.name in visible_tool_names]

        removed_count = len(all_tools) - len(filtered)
        if removed_count > 0:
            self._tools_filtered += removed_count
            logger.debug(
                "[TOOL-NS] Filtered %d/%d tools for %s:%s (visible: %d)",
                removed_count,
                len(all_tools),
                subject[0],
                subject[1],
                len(filtered),
            )

        return filtered

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Validate tool invocation — reject invisible tools."""
        if not self._enabled:
            return await call_next(context)

        subject = self._extract_subject(context)
        if subject is None:
            # No subject identity → allow (backward compat / admin)
            return await call_next(context)

        tool_name = context.message.name
        visible_tool_names = self._get_visible_tools(subject)

        if tool_name not in visible_tool_names:
            self._calls_rejected += 1
            logger.warning(
                "[TOOL-NS] Rejected tool call '%s' for %s:%s — not in visible set",
                tool_name,
                subject[0],
                subject[1],
            )
            # Return "not found" error — namespace-as-security principle
            # The agent must not learn the tool exists but is restricted.
            error_msg = tool_error(
                "not_found",
                f"Tool '{tool_name}' not found.",
                f"subject={subject[0]}:{subject[1]}",
            )
            return ToolResult(content=[_text_content(error_msg)])

        return await call_next(context)

    # ------------------------------------------------------------------
    # Tool visibility cache
    # ------------------------------------------------------------------

    def _get_visible_tools(self, subject: tuple[str, str]) -> frozenset[str]:
        """Get the set of visible tool names for a subject.

        Batch lookup: ``rebac_list_objects()`` filtered to ``/tools/`` prefix.
        Cached with revision quantization.

        Args:
            subject: (subject_type, subject_id) tuple.

        Returns:
            frozenset of tool names (without ``/tools/`` prefix).
        """
        cache_key = self._cache_key(subject)

        with self._lock:
            cached = self._tool_cache.get(cache_key)
        if cached is not None:
            self._cache_hits += 1
            return cached

        self._cache_misses += 1
        tools = self._rebuild_tool_set(subject)

        with self._lock:
            self._tool_cache[cache_key] = tools

        return tools

    def _rebuild_tool_set(self, subject: tuple[str, str]) -> frozenset[str]:
        """Rebuild the visible tool set from ReBAC grants.

        Calls ``rebac_list_objects(subject, "read", "file")`` and extracts
        entries starting with ``/tools/``.

        Args:
            subject: (subject_type, subject_id) tuple.

        Returns:
            frozenset of tool names.
        """
        try:
            objects = self._rebac_manager.rebac_list_objects(
                subject=subject,
                permission="read",
                object_type="file",
                zone_id=self._zone_id,
                limit=10_000,
            )
        except Exception:
            self._rebac_errors += 1
            logger.exception(
                "[TOOL-NS] Failed to rebuild tool set for %s:%s (error #%d), returning empty",
                subject[0],
                subject[1],
                self._rebac_errors,
            )
            return frozenset()

        tool_names: set[str] = set()
        for _obj_type, obj_id in objects:
            if obj_id.startswith(TOOL_PATH_PREFIX):
                # Extract tool name from path: "/tools/nexus_read_file" → "nexus_read_file"
                tool_name = obj_id[len(TOOL_PATH_PREFIX) :]
                if tool_name:
                    tool_names.add(tool_name)

        logger.debug(
            "[TOOL-NS] Rebuilt tool set for %s:%s: %d tools",
            subject[0],
            subject[1],
            len(tool_names),
        )

        return frozenset(tool_names)

    def _cache_key(
        self,
        subject: tuple[str, str],
    ) -> tuple[str, str, str | None, int]:
        """Build cache key with revision quantization."""
        revision_bucket = self._get_revision_bucket()
        return (subject[0], subject[1], self._zone_id, revision_bucket)

    def _get_revision_bucket(self) -> int:
        """Get current zone revision bucket."""
        try:
            revision = self._rebac_manager._get_zone_revision(self._zone_id)
        except Exception:
            return 0
        return revision // self._revision_window

    # ------------------------------------------------------------------
    # Subject extraction
    # ------------------------------------------------------------------

    def _extract_subject(
        self,
        context: MiddlewareContext[Any],
    ) -> tuple[str, str] | None:
        """Extract subject identity from middleware context.

        Tries FastMCP context state (set by APIKeyExtractionMiddleware),
        then falls back to None (no filtering).

        Args:
            context: FastMCP middleware context.

        Returns:
            (subject_type, subject_id) tuple, or None if not available.
        """
        if context.fastmcp_context is None:
            return None

        try:
            subject_type = context.fastmcp_context.get_state("subject_type")
            subject_id = context.fastmcp_context.get_state("subject_id")
            if subject_type and subject_id:
                return (subject_type, subject_id)
        except Exception:
            pass

        # Fallback: try API key as subject_id
        try:
            api_key = context.fastmcp_context.get_state("api_key")
            if api_key:
                return ("api_key", api_key)
        except Exception:
            pass

        return None

    def _extract_zone_id(
        self,
        context: MiddlewareContext[Any],
    ) -> str | None:
        """Extract zone ID from context state, falling back to default.

        Args:
            context: FastMCP middleware context.

        Returns:
            Zone ID string, or the middleware's default zone_id.
        """
        if context.fastmcp_context is not None:
            try:
                zone_id: str | None = context.fastmcp_context.get_state("zone_id")
                if zone_id:
                    return zone_id
            except Exception:
                pass
        return self._zone_id

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def invalidate(self, subject: tuple[str, str] | None = None) -> None:
        """Invalidate cached tool sets.

        Args:
            subject: If provided, invalidate only this subject's cache.
                If None, clear entire cache.
        """
        with self._lock:
            if subject is None:
                self._tool_cache.clear()
            else:
                # Remove all entries for this subject (any zone/revision)
                keys_to_remove = [
                    k for k in self._tool_cache if k[0] == subject[0] and k[1] == subject[1]
                ]
                for k in keys_to_remove:
                    self._tool_cache.pop(k, None)

    @property
    def metrics(self) -> dict[str, Any]:
        """Return middleware metrics for monitoring."""
        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_size": len(self._tool_cache),
            "tools_filtered": self._tools_filtered,
            "calls_rejected": self._calls_rejected,
            "rebac_errors": self._rebac_errors,
            "enabled": self._enabled,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_content(text: str) -> Any:
    """Create an MCP TextContent object.

    Uses lazy import to avoid hard dependency on mcp.types at module level.
    """
    import mcp.types as mt

    return mt.TextContent(type="text", text=text)
