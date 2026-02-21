"""Auto-discoverable brick factory for Tools brick (Issue #2436).

Provides LangGraph tool integration utilities.
"""

from __future__ import annotations

from typing import Any

BRICK_NAME = "tools"
TIER = "independent"
RESULT_KEY = "tools_service"


def create(_ctx: Any, _system: dict[str, Any]) -> Any:
    """Return tools dict with get_nexus_tools entry point. Lazy import."""
    from nexus.bricks.tools import get_nexus_tools

    return {"get_nexus_tools": get_nexus_tools}
