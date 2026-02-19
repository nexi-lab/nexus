"""Upload startup: ChunkedUploadService (TUS protocol, Issue #788).

Extracted from fastapi_server.py (#1602).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def startup_uploads(app: FastAPI) -> list[asyncio.Task]:
    """Initialize chunked upload service and return background tasks."""
    bg_tasks: list[asyncio.Task] = []

    # Get the pre-configured service from factory (reads NEXUS_UPLOAD_* env vars)
    _brk = getattr(app.state.nexus_fs, "_brick_services", None) if app.state.nexus_fs else None
    _factory_upload_svc = getattr(_brk, "chunked_upload_service", None)

    if _factory_upload_svc is not None:
        app.state.chunked_upload_service = _factory_upload_svc
        cleanup_task = asyncio.create_task(app.state.chunked_upload_service.start_cleanup_loop())
        bg_tasks.append(cleanup_task)
        logger.info("[TUS] ChunkedUploadService initialized from factory with background cleanup")
    else:
        app.state.chunked_upload_service = None

    return bg_tasks
