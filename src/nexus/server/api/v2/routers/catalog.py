"""Catalog REST API endpoints (Issue #2930).

Provides endpoints for data catalog operations:
- GET /api/v2/catalog/schema/{path:path} -- Get extracted schema for a file
- GET /api/v2/catalog/search -- Search for files by column name
"""

import logging
import mimetypes
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from nexus.server.api.v2.dependencies import get_catalog_service, get_nexus_fs
from nexus.server.api.v2.models.aspects import (
    CatalogSchemaResponse,
    ColumnSearchResponse,
    ColumnSearchResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/catalog", tags=["catalog"])


@router.get("/schema/{path:path}")
async def get_catalog_schema(
    path: str = Path(..., description="File path"),
    catalog_and_zone: tuple[Any, str] = Depends(get_catalog_service),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> CatalogSchemaResponse:
    """Get extracted schema for a data file.

    Returns the stored schema_metadata aspect if available, or
    extracts it on-the-fly from file content.
    """
    catalog_svc, zone_id = catalog_and_zone
    full_path = f"/{path}" if not path.startswith("/") else path

    try:
        from nexus.contracts.urn import NexusURN

        urn = str(NexusURN.for_file(zone_id, full_path))

        # Try stored schema first
        schema = catalog_svc.get_schema(urn)
        if schema is not None:
            # Verify caller has file access before returning cached schema
            # (prevents bypassing permission checks via cache)
            try:
                nexus_fs.sys_stat(full_path)
            except PermissionError as perm_err:
                raise HTTPException(
                    status_code=403, detail=f"Access denied: {full_path}"
                ) from perm_err
            except Exception as stat_err:
                raise HTTPException(
                    status_code=404, detail=f"File not found: {full_path}"
                ) from stat_err
            return CatalogSchemaResponse(entity_urn=urn, path=full_path, schema=schema)

        # Extract on-the-fly
        try:
            content = nexus_fs.read(full_path)
            if isinstance(content, str):
                content = content.encode()
        except Exception as read_err:
            raise HTTPException(
                status_code=404, detail=f"File not found: {full_path}"
            ) from read_err

        mime_type, _ = mimetypes.guess_type(full_path)
        filename = full_path.rsplit("/", 1)[-1] if "/" in full_path else full_path

        result = catalog_svc.extract_schema(
            entity_urn=urn,
            content=content,
            mime_type=mime_type,
            filename=filename,
            zone_id=zone_id,
        )

        if result.schema is None:
            return CatalogSchemaResponse(entity_urn=urn, path=full_path, schema=None)

        stored = catalog_svc.get_schema(urn)
        return CatalogSchemaResponse(entity_urn=urn, path=full_path, schema=stored)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_catalog_schema error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get schema") from e


@router.get("/search")
async def search_by_column(
    column: str = Query(..., min_length=1, description="Column name to search for"),
    catalog_and_zone: tuple[Any, str] = Depends(get_catalog_service),
) -> ColumnSearchResponse:
    """Search for data files containing a specific column name."""
    catalog_svc, zone_id = catalog_and_zone
    try:
        results = catalog_svc.search_by_column(column, zone_id=zone_id)
        items = [
            ColumnSearchResult(
                entity_urn=r["entity_urn"],
                column_name=r["column_name"],
                column_type=r["column_type"],
                schema=r.get("schema", {}),
            )
            for r in results
        ]
        return ColumnSearchResponse(
            results=items,
            total=len(items),
            capped=len(items) >= 1000,
        )
    except Exception as e:
        logger.error("search_by_column error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to search by column") from e
