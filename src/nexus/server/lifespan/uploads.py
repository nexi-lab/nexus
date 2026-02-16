"""Upload startup: ChunkedUploadService (TUS protocol, Issue #788).

Extracted from fastapi_server.py (#1602).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def startup_uploads(app: FastAPI) -> list[asyncio.Task]:
    """Initialize chunked upload service and return background tasks."""
    bg_tasks: list[asyncio.Task] = []

    # Prefer the pre-configured service from factory (reads NEXUS_UPLOAD_* env vars)
    _factory_upload_svc = (
        app.state.nexus_fs._service_extras.get("chunked_upload_service")
        if app.state.nexus_fs
        else None
    )

    if _factory_upload_svc is not None:
        app.state.chunked_upload_service = _factory_upload_svc
        cleanup_task = asyncio.create_task(app.state.chunked_upload_service.start_cleanup_loop())
        bg_tasks.append(cleanup_task)
        logger.info("[TUS] ChunkedUploadService initialized from factory with background cleanup")
    elif app.state.nexus_fs and getattr(app.state.nexus_fs, "SessionLocal", None):
        try:
            from nexus.services.chunked_upload_service import (
                ChunkedUploadConfig,
                ChunkedUploadService,
            )

            _backend = getattr(app.state.nexus_fs, "backend", None)
            _session_factory = app.state.nexus_fs.SessionLocal
            if _backend and _session_factory:
                _upload_kwargs: dict = {}
                for _env, _key in {
                    "NEXUS_UPLOAD_MIN_CHUNK_SIZE": "min_chunk_size",
                    "NEXUS_UPLOAD_MAX_CHUNK_SIZE": "max_chunk_size",
                    "NEXUS_UPLOAD_MAX_CONCURRENT": "max_concurrent_uploads",
                    "NEXUS_UPLOAD_SESSION_TTL_HOURS": "session_ttl_hours",
                    "NEXUS_UPLOAD_CLEANUP_INTERVAL": "cleanup_interval_seconds",
                    "NEXUS_UPLOAD_MAX_SIZE": "max_upload_size",
                }.items():
                    _v = os.getenv(_env)
                    if _v is not None:
                        _upload_kwargs[_key] = int(_v)

                app.state.chunked_upload_service = ChunkedUploadService(
                    session_factory=_session_factory,
                    backend=_backend,
                    metadata_store=getattr(app.state.nexus_fs, "metadata", None),
                    config=ChunkedUploadConfig(**_upload_kwargs),
                )
                cleanup_task = asyncio.create_task(
                    app.state.chunked_upload_service.start_cleanup_loop()
                )
                bg_tasks.append(cleanup_task)
                logger.info("[TUS] ChunkedUploadService initialized with background cleanup")
        except Exception as e:
            logger.warning(f"[TUS] Failed to initialize ChunkedUploadService: {e}")
            app.state.chunked_upload_service = None
    else:
        app.state.chunked_upload_service = None

    return bg_tasks
