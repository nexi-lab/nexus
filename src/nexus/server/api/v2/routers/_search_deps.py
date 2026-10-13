"""Request-scoped Search transport for document indexing."""

from typing import Any

from fastapi import HTTPException, Request


def _get_search_daemon(request: Request) -> Any:
    """Get SearchDaemon from app.state, raising 503 if not enabled."""
    daemon = getattr(request.app.state, "search_daemon", None)
    if daemon is None:
        raise HTTPException(
            status_code=503,
            detail="Search daemon unavailable (configure NEXUS_SEARCH_PLUGIN_TARGET)",
        )
    return daemon
