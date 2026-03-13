"""Catalog protocol and service — schema extraction pipeline (Issue #2929).

CatalogService receives AspectServiceProtocol via constructor injection
(zero core imports). Extractors return ExtractionResult (never raise).

Architecture:
    CatalogService.extract_schema(urn, content, mime_type)
        → detect extractor by mime_type
        → extractor.extract(content) → ExtractionResult
        → store as schema_metadata aspect if confidence > threshold
"""

import logging
from typing import Any, Protocol, runtime_checkable

from nexus.bricks.catalog.extractors import (
    CSVExtractor,
    ExtractionResult,
    JSONExtractor,
    ParquetExtractor,
    SchemaExtractor,
)

logger = logging.getLogger(__name__)

# Minimum confidence to auto-store extracted schema
DEFAULT_CONFIDENCE_THRESHOLD = 0.5

# Max file size for auto-extraction (100MB)
DEFAULT_MAX_AUTO_EXTRACT_BYTES = 100 * 1024 * 1024

# Default read limit for CSV/JSON inference
DEFAULT_INFERENCE_READ_BYTES = 10 * 1024 * 1024  # 10MB
DEFAULT_INFERENCE_MAX_ROWS = 10_000


@runtime_checkable
class CatalogProtocol(Protocol):
    """Contract for data catalog operations."""

    def extract_schema(
        self,
        entity_urn: str,
        content: bytes,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
        zone_id: str | None = None,
        created_by: str = "system",
    ) -> ExtractionResult:
        """Extract schema from file content and store as aspect.

        Args:
            entity_urn: URN of the file entity.
            content: Raw file content (or first N bytes for large files).
            mime_type: MIME type hint.
            filename: Filename hint for format detection.
            zone_id: Zone scope for aspect storage.
            created_by: User/agent performing extraction.

        Returns:
            ExtractionResult with schema, confidence, warnings.
        """
        ...

    def get_schema(
        self,
        entity_urn: str,
    ) -> dict[str, Any] | None:
        """Get stored schema aspect for an entity.

        Returns:
            Schema metadata dict, or None if no schema stored.
        """
        ...

    def search_by_column(
        self,
        column_name: str,
        *,
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for entities containing a column name.

        Returns:
            List of matching entities with their schema metadata.
        """
        ...


class CatalogService:
    """Schema extraction service with pluggable extractors.

    Structurally satisfies ``CatalogProtocol``.

    Constructor injection: receives AspectServiceProtocol so this brick
    has zero core imports.
    """

    def __init__(
        self,
        aspect_service: Any,
        *,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        max_auto_extract_bytes: int = DEFAULT_MAX_AUTO_EXTRACT_BYTES,
        inference_read_bytes: int = DEFAULT_INFERENCE_READ_BYTES,
        inference_max_rows: int = DEFAULT_INFERENCE_MAX_ROWS,
    ) -> None:
        self._aspect_service = aspect_service
        self._confidence_threshold = confidence_threshold
        self._max_auto_extract_bytes = max_auto_extract_bytes
        self._inference_read_bytes = inference_read_bytes
        self._inference_max_rows = inference_max_rows

        # Extractor registry: mime_type → extractor
        self._extractors: dict[str, SchemaExtractor] = {}
        self._register_default_extractors()

    def _register_default_extractors(self) -> None:
        """Register built-in extractors."""
        csv_ext = CSVExtractor(max_rows=self._inference_max_rows)
        json_ext = JSONExtractor(max_bytes=self._inference_read_bytes)
        parquet_ext = ParquetExtractor()

        for mime in ("text/csv", "application/csv"):
            self._extractors[mime] = csv_ext
        for mime in ("application/json", "text/json"):
            self._extractors[mime] = json_ext
        for mime in ("application/parquet", "application/x-parquet"):
            self._extractors[mime] = parquet_ext

    def register_extractor(
        self,
        mime_type: str,
        extractor: SchemaExtractor,
    ) -> None:
        """Register a custom extractor for a MIME type."""
        self._extractors[mime_type] = extractor

    def _detect_extractor(
        self,
        mime_type: str | None,
        filename: str | None,
    ) -> SchemaExtractor | None:
        """Detect appropriate extractor from MIME type or filename."""
        if mime_type and mime_type in self._extractors:
            return self._extractors[mime_type]

        if filename:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            mime_map = {
                "csv": "text/csv",
                "tsv": "text/csv",
                "json": "application/json",
                "jsonl": "application/json",
                "ndjson": "application/json",
                "parquet": "application/parquet",
                "pq": "application/parquet",
            }
            mapped_mime = mime_map.get(ext)
            if mapped_mime and mapped_mime in self._extractors:
                return self._extractors[mapped_mime]

        return None

    def extract_schema(
        self,
        entity_urn: str,
        content: bytes,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
        zone_id: str | None = None,
        created_by: str = "system",
    ) -> ExtractionResult:
        """Extract schema from file content and store as aspect."""
        # Size gate
        if len(content) > self._max_auto_extract_bytes:
            return ExtractionResult(
                schema=None,
                format="unknown",
                confidence=0.0,
                warnings=[
                    f"File size ({len(content)} bytes) exceeds auto-extraction "
                    f"limit ({self._max_auto_extract_bytes} bytes). "
                    f"Use manual extraction."
                ],
                error="File too large for auto-extraction",
            )

        extractor = self._detect_extractor(mime_type, filename)
        if extractor is None:
            return ExtractionResult(
                schema=None,
                format="unknown",
                confidence=0.0,
                warnings=["No extractor available for this file type"],
                error=f"Unsupported format: mime_type={mime_type}, filename={filename}",
            )

        # Extract (extractors never raise)
        result = extractor.extract(content)

        # Store as aspect if confidence meets threshold
        if result.schema is not None and result.confidence >= self._confidence_threshold:
            try:
                self._aspect_service.put_aspect(
                    entity_urn=entity_urn,
                    aspect_name="schema_metadata",
                    payload={
                        "columns": result.schema,
                        "format": result.format,
                        "row_count": result.row_count,
                        "confidence": result.confidence,
                        "warnings": result.warnings,
                    },
                    created_by=created_by,
                    zone_id=zone_id,
                )
            except Exception as e:
                logger.warning("Failed to store schema aspect: %s", e)
                result = ExtractionResult(
                    schema=result.schema,
                    format=result.format,
                    confidence=result.confidence,
                    row_count=result.row_count,
                    warnings=[*result.warnings, f"Schema extracted but storage failed: {e}"],
                    error=None,
                )

        return result

    def get_schema(
        self,
        entity_urn: str,
    ) -> dict[str, Any] | None:
        """Get stored schema aspect for an entity."""
        result: dict[str, Any] | None = self._aspect_service.get_aspect(
            entity_urn, "schema_metadata"
        )
        return result

    def search_by_column(
        self,
        column_name: str,
        *,
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for entities containing a column name.

        Naive scan-based implementation: loads all schema_metadata aspects
        and filters in Python. Production should use a search index built
        from MCL events for O(1) column lookups.
        """
        results: list[dict[str, Any]] = []
        all_schemas = self._aspect_service.find_entities_with_aspect("schema_metadata")

        for entity_urn, payload in all_schemas.items():
            # Skip if zone filter doesn't match (check URN zone component)
            if zone_id is not None and f":{zone_id}:" not in entity_urn:
                continue

            columns = payload.get("columns", [])
            for col in columns:
                col_name = col.get("name", "")
                if column_name.lower() in col_name.lower():
                    results.append(
                        {
                            "entity_urn": entity_urn,
                            "column_name": col_name,
                            "column_type": col.get("type", "unknown"),
                            "schema": payload,
                        }
                    )
                    break  # One match per entity is sufficient

        return results
