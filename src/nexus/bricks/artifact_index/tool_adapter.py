"""Tool indexer adapter (Issue #1861).

Parses DataPart content for tool schemas and indexes them into
ToolIndex for discovery.  Uses an injected ``tool_info_factory``
to construct tool objects without cross-brick imports.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from nexus.bricks.artifact_index.protocol import ArtifactContent, ArtifactIndexerProtocol

logger = logging.getLogger(__name__)

# Minimal required keys for a valid tool schema
_TOOL_SCHEMA_KEYS = frozenset({"name", "description"})


class ToolIndexerAdapter:
    """Indexes tool definitions from artifact DataParts into ToolIndex.

    Satisfies ``ArtifactIndexerProtocol`` via duck typing.

    Tool schemas are detected by checking for ``name`` and ``description``
    keys in the top-level JSON object.  A ``server`` field defaults to
    ``"artifact:<artifact_id>"``.

    Args:
        tool_index: A ToolIndex instance with ``add_tool(tool)`` method.
        tool_info_factory: Callable ``(name, description, server, input_schema) -> ToolInfo``.
            Injected by brick_factory to avoid cross-brick imports.
    """

    def __init__(
        self,
        tool_index: Any,
        tool_info_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._tool_index = tool_index
        self._tool_info_factory = tool_info_factory

    async def index(self, content: ArtifactContent) -> None:
        """Parse text for tool schemas and add to the tool index.

        Expects content.text to be JSON (from DataPart serialization).
        Non-JSON content or content without tool schema keys is silently
        skipped.  Errors are logged and suppressed.
        """
        if not content.text:
            return

        if self._tool_info_factory is None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[ARTIFACT-INDEX:tool] No tool_info_factory, skipping artifact %s",
                    content.artifact_id,
                )
            return

        try:
            parsed = json.loads(content.text)
        except (json.JSONDecodeError, ValueError):
            # Not JSON — not a tool schema, skip silently
            return

        tools = self._extract_tool_defs(parsed)
        if not tools:
            return

        try:
            count = 0
            for tool_def in tools:
                tool = self._tool_info_factory(
                    name=tool_def["name"],
                    description=tool_def["description"],
                    server=tool_def.get("server", f"artifact:{content.artifact_id}"),
                    input_schema=tool_def.get("input_schema", {}),
                )
                self._tool_index.add_tool(tool)
                count += 1

            if count > 0:
                logger.info(
                    "[ARTIFACT-INDEX:tool] Indexed %d tools from artifact %s",
                    count,
                    content.artifact_id,
                )
        except Exception:
            logger.exception(
                "[ARTIFACT-INDEX:tool] Failed to index tools from artifact %s",
                content.artifact_id,
            )

    @staticmethod
    def _extract_tool_defs(data: Any) -> list[dict[str, Any]]:
        """Extract tool definitions from parsed JSON.

        Handles both single tool objects and lists of tools.
        Returns only dicts that contain both ``name`` and ``description``.
        """
        candidates: list[Any]
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            # Check if top-level is a tool schema itself
            if _TOOL_SCHEMA_KEYS.issubset(data.keys()):
                return [data]
            # Check for a nested "tools" array
            nested = data.get("tools")
            if isinstance(nested, list):
                candidates = nested
            else:
                return []
        else:
            return []

        return [
            c for c in candidates if isinstance(c, dict) and _TOOL_SCHEMA_KEYS.issubset(c.keys())
        ]


# Ensure duck-type conformance at import time
assert isinstance(ToolIndexerAdapter.__new__(ToolIndexerAdapter), ArtifactIndexerProtocol)
