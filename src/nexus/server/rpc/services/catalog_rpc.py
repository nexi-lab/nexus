"""Catalog RPC Service — data catalog queries.

Issue #2930.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class CatalogRPCService:
    """RPC surface for data catalog operations."""

    def __init__(self, catalog_service: Any) -> None:
        self._catalog = catalog_service

    @rpc_expose(description="Get catalog schema for a path")
    async def catalog_schema(self, path: str) -> dict[str, Any]:
        schema = await self._catalog.get_schema(path)
        if schema is None:
            return {"error": f"No schema found for {path}"}
        return {"path": path, "schema": schema}

    @rpc_expose(description="Search catalog by column name")
    async def catalog_search_column(self, column: str) -> dict[str, Any]:
        results = await self._catalog.search_by_column(column)
        return {
            "column": column,
            "results": [
                {"path": r.path, "column_name": r.column_name, "data_type": r.data_type}
                for r in results
            ],
            "count": len(results),
        }
