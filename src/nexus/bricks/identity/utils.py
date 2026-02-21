"""Shared utilities for identity layer (Decision #5B — DRY JSON metadata helpers).

Centralizes JSON metadata serialization/deserialization with consistent error handling
and immutable return types. Used by identity models and services, and available for
backfill into existing code (agent_registry.py, workspace_registry.py) in future PRs.
"""

from __future__ import annotations

import json
import logging
import types
from typing import Any

logger = logging.getLogger(__name__)


def parse_metadata(raw: str | None, context: str = "unknown") -> types.MappingProxyType[str, Any]:
    """Safely parse a JSON metadata string to an immutable dict.

    Args:
        raw: JSON string to parse, or None.
        context: Human-readable context for error logging (e.g. agent_id).

    Returns:
        Immutable MappingProxyType wrapping the parsed dict.
        Returns empty MappingProxyType if raw is None or invalid JSON.
    """
    if raw is None:
        return types.MappingProxyType({})

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("[IDENTITY] Corrupt JSON metadata for %s", context)
        return types.MappingProxyType({})

    if not isinstance(parsed, dict):
        logger.warning("[IDENTITY] Metadata is not a dict for %s", context)
        return types.MappingProxyType({})

    return types.MappingProxyType(parsed)


def serialize_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Serialize a metadata dict to a JSON string.

    Args:
        metadata: Dict to serialize, or None.

    Returns:
        JSON string, or None if metadata is None or empty.
    """
    if metadata is None:
        return None

    if not metadata:
        return None

    return json.dumps(metadata, separators=(",", ":"), sort_keys=True)
