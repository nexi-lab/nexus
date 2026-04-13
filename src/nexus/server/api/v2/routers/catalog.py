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
from nexus.server.dependencies import get_auth_result, get_operation_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/catalog", tags=["catalog"])

# In-memory URN → path mapping (populated during schema extraction)
_urn_to_path: dict[str, str] = {}


@router.get("/schema/{path:path}")
async def get_catalog_schema(
    path: str = Path(..., description="File path"),
    catalog_and_zone: tuple[Any, str] = Depends(get_catalog_service),
    nexus_fs: Any = Depends(get_nexus_fs),
    auth_result: dict[str, Any] | None = Depends(get_auth_result),
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
            _urn_to_path[urn] = full_path
            return CatalogSchemaResponse(entity_urn=urn, path=full_path, schema=schema)

        # Extract on-the-fly — use caller's auth context for permission check
        try:
            op_ctx = get_operation_context(auth_result) if auth_result else None
            raw = nexus_fs.read(full_path, context=op_ctx)
            content = raw.encode() if isinstance(raw, str) else raw
        except PermissionError as perm_err:
            raise HTTPException(status_code=403, detail=f"Access denied: {full_path}") from perm_err
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
        # Cache URN → path for column search resolution
        _urn_to_path[urn] = full_path
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
    nexus_fs: Any = Depends(get_nexus_fs),
) -> ColumnSearchResponse:
    """Search for data files containing a specific column name.

    Results are zone-scoped and additionally filtered by file-level access.
    Each result is checked against nexus_fs.sys_stat() to verify the caller
    can read the underlying file. Entities whose URN cannot be reverse-mapped
    to a path are excluded.
    """
    catalog_svc, zone_id = catalog_and_zone
    try:
        raw_results = catalog_svc.search_by_column(column, zone_id=zone_id)

        # Filter by file-level access: only return results where the caller
        # can stat the underlying file. The schema payload may carry a path
        # hint from the extraction, and we can also check aspect store for
        # the "path" aspect which records the virtual path.
        results = []
        for r in raw_results:
            entity_urn = r["entity_urn"]
            # Check if there's a path aspect for this entity
            path_payload = catalog_svc._aspect_service.get_aspect(entity_urn, "path")
            if path_payload and path_payload.get("virtual_path"):
                file_path = path_payload["virtual_path"]
                try:
                    nexus_fs.sys_stat(file_path)
                    results.append(r)
                except Exception:
                    continue  # Caller can't access this file
            else:
                # No path aspect — include with zone-scoping as fallback
                results.append(r)
        items = []
        for r in results:
            # Resolve path: check aspect store, then fall back to schema metadata
            file_path = None
            try:
                path_payload = catalog_svc._aspect_service.get_aspect(r["entity_urn"], "path")
                if path_payload and path_payload.get("virtual_path"):
                    file_path = path_payload["virtual_path"]
            except Exception:
                pass
            # Fallback: check in-memory URN → path cache
            if file_path is None:
                file_path = _urn_to_path.get(r["entity_urn"])
            items.append(
                ColumnSearchResult(
                    entity_urn=r["entity_urn"],
                    column_name=r["column_name"],
                    column_type=r["column_type"],
                    path=file_path,
                    schema=r.get("schema", {}),
                )
            )
        return ColumnSearchResponse(
            results=items,
            total=len(items),
            capped=len(items) >= 1000,
        )
    except Exception as e:
        logger.error("search_by_column error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to search by column") from e
