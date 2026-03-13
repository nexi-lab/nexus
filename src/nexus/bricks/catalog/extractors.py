"""Schema extractors — CSV, Parquet, JSON (Issue #2929).

Extractors never raise exceptions. They return ExtractionResult with
error/warnings for graceful degradation. Size-gated: CSV/JSON read
at most N bytes/rows; Parquet reads footer only (always fast).

Design decisions (Code Quality Review #8):
    - ExtractionResult type with confidence score
    - Constructor injection for config (max_rows, max_bytes)
    - Parquet always auto-extracted (O(1) footer read)
    - CSV/JSON bounded reads with confidence reflecting sample quality
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Result of a schema extraction attempt.

    Attributes:
        schema: List of column dicts (name, type, nullable) or None on failure.
        format: File format string (csv, parquet, json, unknown).
        confidence: 0.0-1.0 confidence in the extracted schema.
        row_count: Number of rows detected (if applicable).
        warnings: Non-fatal issues encountered.
        error: Fatal error description, or None on success.
    """

    schema: list[dict[str, str]] | None
    format: str
    confidence: float
    row_count: int | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class SchemaExtractor(Protocol):
    """Protocol for format-specific schema extractors."""

    def extract(self, content: bytes) -> ExtractionResult:
        """Extract schema from raw file content.

        Must never raise. Return ExtractionResult with error on failure.
        """
        ...


class CSVExtractor:
    """CSV/TSV schema extractor with bounded row reading.

    Reads at most ``max_rows`` rows for type inference. Confidence
    reflects sample coverage.
    """

    def __init__(
        self,
        max_rows: int = 10_000,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self._max_rows = max_rows
        self._max_bytes = max_bytes

    def extract(self, content: bytes) -> ExtractionResult:
        """Extract schema from CSV content."""
        try:
            # Bound the read
            sample = content[: self._max_bytes]
            text = sample.decode("utf-8", errors="replace")

            # Detect delimiter
            sniffer = csv.Sniffer()
            try:
                dialect = sniffer.sniff(text[:8192])
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = ","

            reader = csv.reader(io.StringIO(text), delimiter=delimiter)

            # Read header
            try:
                headers = next(reader)
            except StopIteration:
                return ExtractionResult(
                    schema=None,
                    format="csv",
                    confidence=0.0,
                    error="Empty CSV file",
                )

            if not headers or all(h.strip() == "" for h in headers):
                return ExtractionResult(
                    schema=None,
                    format="csv",
                    confidence=0.0,
                    error="CSV has no header row or all headers are empty",
                )

            # Read sample rows for type inference
            rows_read = 0
            col_types: list[set[str]] = [set() for _ in headers]
            warnings: list[str] = []

            for row in reader:
                if rows_read >= self._max_rows:
                    break
                rows_read += 1

                for i, val in enumerate(row):
                    if i >= len(headers):
                        break
                    col_types[i].add(_infer_type(val))

            # Build schema
            columns: list[dict[str, str]] = []
            for i, header in enumerate(headers):
                types = col_types[i] if i < len(col_types) else set()
                inferred = _resolve_types(types)
                columns.append(
                    {
                        "name": header.strip(),
                        "type": inferred,
                        "nullable": str("null" in types or "string" in types),
                    }
                )

            # Compute confidence based on sample coverage
            total_bytes = len(content)
            sample_bytes = len(sample)
            if total_bytes <= sample_bytes:
                confidence = 1.0
            else:
                confidence = round(min(0.95, sample_bytes / total_bytes + 0.3), 2)

            if len(content) > self._max_bytes:
                warnings.append(
                    f"Only first {self._max_bytes} bytes sampled "
                    f"({rows_read} rows of potentially more)"
                )

            return ExtractionResult(
                schema=columns,
                format="csv",
                confidence=confidence,
                row_count=rows_read,
                warnings=warnings,
            )

        except Exception as e:
            return ExtractionResult(
                schema=None,
                format="csv",
                confidence=0.0,
                error=f"CSV extraction failed: {e}",
            )


class ParquetExtractor:
    """Parquet schema extractor — reads footer only (O(1)).

    Always fast regardless of file size. Confidence is always 1.0
    since Parquet embeds the schema in the file footer.
    """

    def extract(self, content: bytes) -> ExtractionResult:
        """Extract schema from Parquet file content."""
        try:
            import pyarrow.parquet as pq

            # Read schema from footer only (no data loaded)
            buf = io.BytesIO(content)
            try:
                parquet_file = pq.ParquetFile(buf)
            except Exception as e:
                return ExtractionResult(
                    schema=None,
                    format="parquet",
                    confidence=0.0,
                    error=f"Invalid Parquet file: {e}",
                )

            arrow_schema = parquet_file.schema_arrow
            row_count = parquet_file.metadata.num_rows

            columns: list[dict[str, str]] = []
            for i in range(len(arrow_schema)):
                field = arrow_schema.field(i)
                columns.append(
                    {
                        "name": field.name,
                        "type": str(field.type),
                        "nullable": str(field.nullable),
                    }
                )

            return ExtractionResult(
                schema=columns,
                format="parquet",
                confidence=1.0,
                row_count=row_count,
            )

        except ImportError:
            return ExtractionResult(
                schema=None,
                format="parquet",
                confidence=0.0,
                error="pyarrow not installed — cannot extract Parquet schema",
            )
        except Exception as e:
            return ExtractionResult(
                schema=None,
                format="parquet",
                confidence=0.0,
                error=f"Parquet extraction failed: {e}",
            )


class JSONExtractor:
    """JSON/NDJSON schema extractor with bounded reading.

    Infers schema from the first N bytes of JSON content. Supports
    both JSON arrays and newline-delimited JSON (NDJSON).
    """

    def __init__(
        self,
        max_bytes: int = 10 * 1024 * 1024,
        max_records: int = 1_000,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_records = max_records

    def extract(self, content: bytes) -> ExtractionResult:
        """Extract schema from JSON content."""
        try:
            import json as json_mod

            sample = content[: self._max_bytes]
            text = sample.decode("utf-8", errors="replace").strip()

            if not text:
                return ExtractionResult(
                    schema=None,
                    format="json",
                    confidence=0.0,
                    error="Empty JSON file",
                )

            records: list[dict[str, Any]] = []
            warnings: list[str] = []

            # Try JSON array first
            if text.startswith("["):
                try:
                    data = json_mod.loads(text)
                    if isinstance(data, list):
                        records = [r for r in data[: self._max_records] if isinstance(r, dict)]
                except json_mod.JSONDecodeError:
                    # Possibly truncated — try parsing available records
                    warnings.append("JSON may be truncated; inferring from partial data")

            # Try NDJSON
            if not records and "\n" in text:
                for line in text.split("\n")[: self._max_records]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json_mod.loads(line)
                        if isinstance(obj, dict):
                            records.append(obj)
                    except json_mod.JSONDecodeError:
                        continue

            # Try single object
            if not records:
                try:
                    obj = json_mod.loads(text)
                    if isinstance(obj, dict):
                        records = [obj]
                except json_mod.JSONDecodeError:
                    return ExtractionResult(
                        schema=None,
                        format="json",
                        confidence=0.0,
                        error="Could not parse JSON content",
                    )

            if not records:
                return ExtractionResult(
                    schema=None,
                    format="json",
                    confidence=0.0,
                    error="No JSON objects found in content",
                )

            # Infer schema from records
            column_types: dict[str, set[str]] = {}
            for record in records:
                for key, value in record.items():
                    if key not in column_types:
                        column_types[key] = set()
                    column_types[key].add(_json_type(value))

            columns: list[dict[str, str]] = []
            for name, types in column_types.items():
                columns.append(
                    {
                        "name": name,
                        "type": _resolve_types(types),
                        "nullable": str("null" in types),
                    }
                )

            # Confidence based on sample coverage
            total_bytes = len(content)
            sample_bytes = len(sample)
            if total_bytes <= sample_bytes:
                confidence = 0.9  # JSON type inference is inherently less certain
            else:
                confidence = round(min(0.85, sample_bytes / total_bytes + 0.3), 2)

            if len(content) > self._max_bytes:
                warnings.append(
                    f"Only first {self._max_bytes} bytes sampled ({len(records)} records)"
                )

            return ExtractionResult(
                schema=columns,
                format="json",
                confidence=confidence,
                row_count=len(records),
                warnings=warnings,
            )

        except Exception as e:
            return ExtractionResult(
                schema=None,
                format="json",
                confidence=0.0,
                error=f"JSON extraction failed: {e}",
            )


# ============================================================================
# Type inference helpers
# ============================================================================


def _infer_type(value: str) -> str:
    """Infer the type of a CSV cell value."""
    if value is None or value.strip() == "":
        return "null"

    value = value.strip()

    # Boolean
    if value.lower() in ("true", "false"):
        return "boolean"

    # Integer
    try:
        int(value)
        return "integer"
    except ValueError:
        pass

    # Float
    try:
        float(value)
        return "float"
    except ValueError:
        pass

    return "string"


def _json_type(value: Any) -> str:
    """Get the type of a JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _resolve_types(types: set[str]) -> str:
    """Resolve a set of observed types into a single type.

    Priority: if mixed, prefer the most general type.
    """
    types_no_null = types - {"null"}
    if not types_no_null:
        return "string"
    if len(types_no_null) == 1:
        return types_no_null.pop()
    if types_no_null == {"integer", "float"}:
        return "float"
    if "object" in types_no_null or "array" in types_no_null:
        return "string"  # Mixed complex types → string
    return "string"  # Mixed types → string
