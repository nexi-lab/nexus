"""JSON parsing helpers for governance metadata (Issue #2129).

Extracts the repeated ``contextlib.suppress(json.JSONDecodeError, TypeError)``
+ ``json.loads()`` pattern used 6+ times across governance services.
"""

import contextlib
import json


def parse_json_metadata(raw: str | None) -> dict[str, object]:
    """Parse a JSON metadata string, returning empty dict on failure.

    Args:
        raw: Raw JSON string, or None.

    Returns:
        Parsed dict, or ``{}`` if *raw* is None, empty, or malformed.
    """
    if not raw:
        return {}
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
    return {}
