"""Data Catalog Brick — schema extraction and discovery (Issue #2929).

Zero-core-import brick following the ``pay/`` gold standard pattern.
Provides schema extraction from structured data files (CSV, Parquet, JSON)
and stores results as aspects via constructor-injected AspectServiceProtocol.

Architecture:
    CatalogService(aspect_service) → extractors → ExtractionResult → aspect store
"""

from nexus.bricks.catalog.extractors import (
    CSVExtractor,
    ExtractionResult,
    JSONExtractor,
    ParquetExtractor,
    SchemaExtractor,
)
from nexus.bricks.catalog.protocol import CatalogProtocol, CatalogService

__all__ = [
    "CSVExtractor",
    "CatalogProtocol",
    "CatalogService",
    "ExtractionResult",
    "JSONExtractor",
    "ParquetExtractor",
    "SchemaExtractor",
]
