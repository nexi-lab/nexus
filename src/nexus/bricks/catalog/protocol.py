"""Catalog protocol and service — schema + document extraction pipeline.

CatalogService receives AspectServiceProtocol via constructor injection
(zero core imports). Extractors return ExtractionResult (never raise).

Architecture (Issue #2929, #2978):
    CatalogService.extract_schema(urn, content, mime_type)
        → detect schema extractor by mime_type
        → extractor.extract(content) → ExtractionResult
        → store as schema_metadata aspect if confidence > threshold

    CatalogService.extract_document(urn, content, mime_type)
        → detect document extractor by mime_type
        → extractor.extract(content) → DocumentExtractionResult
        → store as document_structure aspect if confidence > threshold

    CatalogService.extract_auto(urn, content, mime_type, filename)
        → detect schema or document extractor, call the right method

Self-registering extractors (Issue #2978): each extractor declares
mime_types and extensions as class attributes. CatalogService builds
its registries from these, eliminating dual-dict maintenance.
"""

import logging
from typing import Any, Protocol, runtime_checkable

from nexus.bricks.catalog.extractors import (
    AvroExtractor,
    CSVExtractor,
    DocumentExtractionResult,
    ExtractionResult,
    JSONExtractor,
    MarkdownExtractor,
    ParquetExtractor,
    SchemaExtractor,
)

logger = logging.getLogger(__name__)

# Minimum confidence to auto-store extracted schema/document
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
        """Extract schema from file content and store as aspect."""
        ...

    def extract_document(
        self,
        entity_urn: str,
        content: bytes,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
        zone_id: str | None = None,
        created_by: str = "system",
    ) -> DocumentExtractionResult:
        """Extract document structure and store as aspect."""
        ...

    def get_schema(
        self,
        entity_urn: str,
    ) -> dict[str, Any] | None:
        """Get stored schema aspect for an entity."""
        ...

    def search_by_column(
        self,
        column_name: str,
        *,
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for entities containing a column name."""
        ...


class CatalogService:
    """Schema + document extraction service with pluggable extractors.

    Structurally satisfies ``CatalogProtocol``.

    Constructor injection: receives AspectServiceProtocol so this brick
    has zero core imports.

    Self-registering extractors (Issue #2978): each extractor class declares
    ``mime_types`` and ``extensions`` as class attributes. Registration loops
    over these instead of maintaining separate MIME/extension dicts.
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

        # Dual registries: schema extractors + document extractors
        self._schema_extractors: dict[str, SchemaExtractor] = {}
        self._schema_ext_map: dict[str, str] = {}  # extension → first mime_type
        self._document_extractors: dict[str, Any] = {}
        self._document_ext_map: dict[str, str] = {}  # extension → first mime_type

        # Backward-compat alias (used by register_extractor)
        self._extractors = self._schema_extractors

        self._register_default_extractors()

    def _register_default_extractors(self) -> None:
        """Register built-in extractors via self-registration metadata."""
        # Schema extractors
        schema_instances = [
            CSVExtractor(max_rows=self._inference_max_rows),
            JSONExtractor(max_bytes=self._inference_read_bytes),
            ParquetExtractor(),
            AvroExtractor(),
        ]
        for ext in schema_instances:
            for mime in ext.mime_types:
                self._schema_extractors[mime] = ext
            for extension in ext.extensions:
                if extension not in self._schema_ext_map:
                    self._schema_ext_map[extension] = ext.mime_types[0]

        # Document extractors
        document_instances = [
            MarkdownExtractor(),
        ]
        for ext in document_instances:
            for mime in ext.mime_types:
                self._document_extractors[mime] = ext
            for extension in ext.extensions:
                if extension not in self._document_ext_map:
                    self._document_ext_map[extension] = ext.mime_types[0]

    def register_extractor(
        self,
        mime_type: str,
        extractor: SchemaExtractor,
    ) -> None:
        """Register a custom schema extractor for a MIME type."""
        self._schema_extractors[mime_type] = extractor

    def _detect_schema_extractor(
        self,
        mime_type: str | None,
        filename: str | None,
    ) -> SchemaExtractor | None:
        """Detect appropriate schema extractor from MIME type or filename."""
        if mime_type and mime_type in self._schema_extractors:
            return self._schema_extractors[mime_type]

        if filename:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            # NOTE: Extension parsing uses last segment only. Compressed files
            # like data.csv.gz will match "gz", not "csv". This is intentional
            # for the current scope — compressed format detection is future work.
            mapped_mime = self._schema_ext_map.get(ext)
            if mapped_mime and mapped_mime in self._schema_extractors:
                return self._schema_extractors[mapped_mime]

        return None

    def _detect_document_extractor(
        self,
        mime_type: str | None,
        filename: str | None,
    ) -> Any | None:
        """Detect appropriate document extractor from MIME type or filename."""
        if mime_type and mime_type in self._document_extractors:
            return self._document_extractors[mime_type]

        if filename:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            mapped_mime = self._document_ext_map.get(ext)
            if mapped_mime and mapped_mime in self._document_extractors:
                return self._document_extractors[mapped_mime]

        return None

    # Keep backward-compat alias
    def _detect_extractor(
        self,
        mime_type: str | None,
        filename: str | None,
    ) -> SchemaExtractor | None:
        """Detect appropriate extractor from MIME type or filename (backward compat)."""
        return self._detect_schema_extractor(mime_type, filename)

    def has_extractor(
        self,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
    ) -> bool:
        """Check if any extractor (schema or document) is available for a file.

        Used by the post-flush hook to format-gate content reads (Issue #2978).
        """
        return (
            self._detect_schema_extractor(mime_type, filename) is not None
            or self._detect_document_extractor(mime_type, filename) is not None
        )

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

        extractor = self._detect_schema_extractor(mime_type, filename)
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

    def extract_schema_from_path(
        self,
        entity_urn: str,
        path: str,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
        zone_id: str | None = None,
        created_by: str = "system",
    ) -> ExtractionResult:
        """Extract schema using path-based I/O for header-only formats (Avro, Parquet).

        Falls back to content-based extraction if the extractor doesn't
        support extract_from_path.
        """
        extractor = self._detect_schema_extractor(mime_type, filename)
        if extractor is None:
            return ExtractionResult(
                schema=None,
                format="unknown",
                confidence=0.0,
                error=f"Unsupported format: mime_type={mime_type}, filename={filename}",
            )

        # Prefer path-based extraction for header-only formats
        extract_from_path = getattr(extractor, "extract_from_path", None)
        if extract_from_path is not None:
            result = extract_from_path(path)
        else:
            # Fall back to content-based
            with open(path, "rb") as f:
                content = f.read(self._max_auto_extract_bytes + 1)
            if len(content) > self._max_auto_extract_bytes:
                return ExtractionResult(
                    schema=None,
                    format="unknown",
                    confidence=0.0,
                    error="File too large for auto-extraction",
                )
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

        return result

    def extract_document(
        self,
        entity_urn: str,
        content: bytes,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
        zone_id: str | None = None,
        created_by: str = "system",
    ) -> DocumentExtractionResult:
        """Extract document structure from content and store as aspect."""
        # Size gate
        if len(content) > self._max_auto_extract_bytes:
            return DocumentExtractionResult(
                title=None,
                headings=[],
                front_matter=None,
                word_count=0,
                link_count=0,
                code_languages=[],
                format="unknown",
                confidence=0.0,
                error="File too large for auto-extraction",
            )

        extractor = self._detect_document_extractor(mime_type, filename)
        if extractor is None:
            return DocumentExtractionResult(
                title=None,
                headings=[],
                front_matter=None,
                word_count=0,
                link_count=0,
                code_languages=[],
                format="unknown",
                confidence=0.0,
                error=f"Unsupported document format: mime_type={mime_type}, filename={filename}",
            )

        result = extractor.extract(content)

        # Store as document_structure aspect if confidence meets threshold
        if result.confidence >= self._confidence_threshold and result.error is None:
            try:
                self._aspect_service.put_aspect(
                    entity_urn=entity_urn,
                    aspect_name="document_structure",
                    payload={
                        "title": result.title,
                        "headings": result.headings,
                        "front_matter": result.front_matter,
                        "word_count": result.word_count,
                        "link_count": result.link_count,
                        "code_languages": result.code_languages,
                        "format": result.format,
                        "confidence": result.confidence,
                    },
                    created_by=created_by,
                    zone_id=zone_id,
                )
            except Exception as e:
                logger.warning("Failed to store document_structure aspect: %s", e)

        return result

    def extract_auto(
        self,
        entity_urn: str,
        content: bytes,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
        zone_id: str | None = None,
        created_by: str = "system",
    ) -> ExtractionResult | DocumentExtractionResult:
        """Auto-detect format and extract schema or document structure.

        Tries schema extraction first, then document extraction.
        Used by the post-flush extraction hook (Issue #2978).
        """
        # Try schema extraction
        if self._detect_schema_extractor(mime_type, filename) is not None:
            return self.extract_schema(
                entity_urn,
                content,
                mime_type=mime_type,
                filename=filename,
                zone_id=zone_id,
                created_by=created_by,
            )

        # Try document extraction
        if self._detect_document_extractor(mime_type, filename) is not None:
            return self.extract_document(
                entity_urn,
                content,
                mime_type=mime_type,
                filename=filename,
                zone_id=zone_id,
                created_by=created_by,
            )

        return ExtractionResult(
            schema=None,
            format="unknown",
            confidence=0.0,
            error=f"No extractor for: mime_type={mime_type}, filename={filename}",
        )

    def get_schema(
        self,
        entity_urn: str,
    ) -> dict[str, Any] | None:
        """Get stored schema aspect for an entity."""
        result: dict[str, Any] | None = self._aspect_service.get_aspect(
            entity_urn, "schema_metadata"
        )
        return result

    def get_document_structure(
        self,
        entity_urn: str,
    ) -> dict[str, Any] | None:
        """Get stored document_structure aspect for an entity."""
        result: dict[str, Any] | None = self._aspect_service.get_aspect(
            entity_urn, "document_structure"
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
            # Skip if zone filter doesn't match (exact zone component check)
            # URN format: urn:nexus:{type}:{zone}:{id}
            if zone_id is not None:
                parts = entity_urn.split(":")
                if len(parts) >= 4 and parts[3] != zone_id:
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
