"""Upload startup: ChunkedUploadService (TUS protocol, Issue #788).

Extracted from fastapi_server.py (#1602).
"""

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


async def startup_uploads(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Initialize chunked upload service and return background tasks."""
    bg_tasks: list[asyncio.Task] = []

    # Get the pre-configured service from factory (reads NEXUS_UPLOAD_* env vars)
    brk = svc.brick_services
    _factory_upload_svc = getattr(brk, "chunked_upload_service", None) if brk else None

    if _factory_upload_svc is not None:
        app.state.chunked_upload_service = _factory_upload_svc
        cleanup_task = asyncio.create_task(app.state.chunked_upload_service.start_cleanup_loop())
        bg_tasks.append(cleanup_task)
        # Enlist with coordinator (Q1 — cleanup loop is a bg task, not PersistentService)
        coord = svc.service_coordinator
        if coord is not None:
            await coord.enlist("chunked_upload", app.state.chunked_upload_service)
        logger.info("[TUS] ChunkedUploadService initialized from factory with background cleanup")
    elif svc.nexus_fs and svc.record_store is not None:
        # Fallback: create from env vars when factory didn't provide the service
        try:
            from nexus.bricks.upload.chunked_upload_service import (
                ChunkedUploadConfig,
                ChunkedUploadService,
            )

            _backend = getattr(svc.nexus_fs, "backend", None)
            _upload_rs = svc.record_store
            if _backend and _upload_rs:
                import os as _os

                _upload_kwargs: dict = {}
                for _env, _key in {
                    "NEXUS_UPLOAD_MIN_CHUNK_SIZE": "min_chunk_size",
                    "NEXUS_UPLOAD_MAX_CHUNK_SIZE": "max_chunk_size",
                    "NEXUS_UPLOAD_MAX_CONCURRENT": "max_concurrent_uploads",
                    "NEXUS_UPLOAD_SESSION_TTL_HOURS": "session_ttl_hours",
                    "NEXUS_UPLOAD_CLEANUP_INTERVAL": "cleanup_interval_seconds",
                    "NEXUS_UPLOAD_MAX_SIZE": "max_upload_size",
                }.items():
                    _v = _os.getenv(_env)
                    if _v is not None:
                        _upload_kwargs[_key] = int(_v)

                app.state.chunked_upload_service = ChunkedUploadService(
                    record_store=_upload_rs,
                    backend=_backend,
                    metadata_store=getattr(svc.nexus_fs, "metadata", None),
                    config=ChunkedUploadConfig(**_upload_kwargs),
                )
                cleanup_task = asyncio.create_task(
                    app.state.chunked_upload_service.start_cleanup_loop()
                )
                bg_tasks.append(cleanup_task)
                # Enlist with coordinator (Q1)
                coord = svc.service_coordinator
                if coord is not None:
                    await coord.enlist("chunked_upload", app.state.chunked_upload_service)
                logger.info("[TUS] ChunkedUploadService initialized with background cleanup")
        except Exception as e:
            logger.warning("[TUS] Failed to initialize ChunkedUploadService: %s", e)
            app.state.chunked_upload_service = None
    else:
        app.state.chunked_upload_service = None

    return bg_tasks
