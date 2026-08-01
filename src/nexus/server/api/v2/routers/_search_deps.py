"""Search-router DI helpers shared across the split router modules.

Extracted from ``search.py`` at the router-split (#4553 follow-up) so
sibling modules (``_search_locate``, ``_search_indexed_dirs``) can
import a search-daemon dependency without going THROUGH the parent
``search.py``.  Straight ``search.py`` &lt;-&gt; sub-module imports would
form a runtime-tolerant but mypy-hostile cycle (``search`` imports
sub-modules at file end, sub-modules import ``_get_search_daemon``
from ``search`` at their top — legal Python, but mypy reports
"Cannot determine type" on every symbol resolved through the cycle).
Parking the helper here breaks the cycle for both mypy and future
readers.

This module has zero project-side imports beyond FastAPI itself,
so it never grows a fresh cycle no matter which module reaches it.
"""

from typing import Any

from fastapi import HTTPException, Request


def _get_search_daemon(request: Request) -> Any:
    """Get SearchDaemon from app.state, raising 503 if not enabled."""
    daemon = getattr(request.app.state, "search_daemon", None)
    if daemon is None:
        raise HTTPException(
            status_code=503,
            detail="Search daemon unavailable (set NEXUS_SEARCH_DAEMON=false to disable)",
        )
    return daemon
