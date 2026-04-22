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
import re
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


@dataclass(frozen=True, slots=True)
class DocumentExtractionResult:
    """Result of a document structure extraction attempt.

    Unlike ExtractionResult (tabular schema), this captures document-level
    structure: headings, front matter, code blocks, etc.
    """

    title: str | None
    headings: list[dict[str, Any]]
    front_matter: dict[str, Any] | None
    word_count: int
    link_count: int
    code_languages: list[str]
    format: str
    confidence: float
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class SchemaExtractor(Protocol):
    """Protocol for format-specific schema extractors."""

    mime_types: tuple[str, ...]
    extensions: tuple[str, ...]

    def extract(self, content: bytes) -> ExtractionResult:
        """Extract schema from raw file content.

        Must never raise. Return ExtractionResult with error on failure.
        """
        ...


class DocumentExtractor(Protocol):
    """Protocol for document structure extractors (Markdown, PDF, etc.)."""

    mime_types: tuple[str, ...]
    extensions: tuple[str, ...]

    def extract(self, content: bytes) -> DocumentExtractionResult:
        """Extract document structure from raw file content.

        Must never raise. Return DocumentExtractionResult with error on failure.
        """
        ...


class CSVExtractor:
    """CSV/TSV schema extractor with bounded row reading.

    Reads at most ``max_rows`` rows for type inference. Confidence
    reflects sample coverage.
    """

    mime_types: tuple[str, ...] = ("text/csv", "application/csv")
    extensions: tuple[str, ...] = ("csv", "tsv")

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

    mime_types: tuple[str, ...] = ("application/parquet", "application/x-parquet")
    extensions: tuple[str, ...] = ("parquet", "pq")

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

    mime_types: tuple[str, ...] = ("application/json", "text/json")
    extensions: tuple[str, ...] = ("json", "jsonl", "ndjson")

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


class AvroExtractor:
    """Avro schema extractor — reads header only (O(1)).

    Avro stores its complete schema in the file header as JSON.
    Extraction is always fast regardless of file size. Confidence
    is always 1.0 since Avro embeds the native schema.
    """

    mime_types: tuple[str, ...] = ("application/avro", "application/x-avro")
    extensions: tuple[str, ...] = ("avro",)

    def extract(self, content: bytes) -> ExtractionResult:
        """Extract schema from Avro file content."""
        try:
            import fastavro

            buf = io.BytesIO(content)
            try:
                reader = fastavro.reader(buf)
                avro_schema = reader.writer_schema
            except Exception as e:
                return ExtractionResult(
                    schema=None,
                    format="avro",
                    confidence=0.0,
                    error=f"Invalid Avro file: {e}",
                )

            if avro_schema is None:
                return ExtractionResult(
                    schema=None,
                    format="avro",
                    confidence=0.0,
                    error="Avro file has no writer schema",
                )

            if not isinstance(avro_schema, dict):
                return ExtractionResult(
                    schema=None,
                    format="avro",
                    confidence=0.0,
                    error="Avro writer schema is not a record",
                )

            columns = _avro_schema_to_columns(avro_schema)

            # Avro does not store row count in the header — skip data
            # section entirely to keep extraction O(1).
            return ExtractionResult(
                schema=columns,
                format="avro",
                confidence=1.0,
                row_count=None,
            )

        except ImportError:
            return ExtractionResult(
                schema=None,
                format="avro",
                confidence=0.0,
                error="fastavro not installed — cannot extract Avro schema",
            )
        except Exception as e:
            return ExtractionResult(
                schema=None,
                format="avro",
                confidence=0.0,
                error=f"Avro extraction failed: {e}",
            )

    def extract_from_path(self, path: str) -> ExtractionResult:
        """Extract schema from Avro file by path (header-only read)."""
        try:
            import fastavro

            with open(path, "rb") as f:
                reader = fastavro.reader(f)
                avro_schema = reader.writer_schema

                if avro_schema is None:
                    return ExtractionResult(
                        schema=None,
                        format="avro",
                        confidence=0.0,
                        error="Avro file has no writer schema",
                    )

                if not isinstance(avro_schema, dict):
                    return ExtractionResult(
                        schema=None,
                        format="avro",
                        confidence=0.0,
                        error="Avro writer schema is not a record",
                    )

                columns = _avro_schema_to_columns(avro_schema)

            return ExtractionResult(
                schema=columns,
                format="avro",
                confidence=1.0,
                row_count=None,
            )

        except ImportError:
            return ExtractionResult(
                schema=None,
                format="avro",
                confidence=0.0,
                error="fastavro not installed — cannot extract Avro schema",
            )
        except Exception as e:
            return ExtractionResult(
                schema=None,
                format="avro",
                confidence=0.0,
                error=f"Avro extraction failed: {e}",
            )


class MarkdownExtractor:
    """Markdown document structure extractor.

    Extracts headings, front matter, code blocks, and statistics.
    Parses YAML front matter (--- delimiters) and ATX/Setext headings.
    """

    mime_types: tuple[str, ...] = ("text/markdown",)
    extensions: tuple[str, ...] = ("md", "markdown")

    def extract(self, content: bytes) -> DocumentExtractionResult:
        """Extract document structure from Markdown content."""
        try:
            text = content.decode("utf-8", errors="replace")

            if not text.strip():
                return DocumentExtractionResult(
                    title=None,
                    headings=[],
                    front_matter=None,
                    word_count=0,
                    link_count=0,
                    code_languages=[],
                    format="markdown",
                    confidence=0.0,
                    error="Empty Markdown file",
                )

            # Parse front matter
            front_matter, body = _parse_front_matter(text)

            # Parse headings (skip content inside code blocks)
            headings = _extract_headings(body)

            # Extract code block languages
            code_languages = _extract_code_languages(body)

            # Count words (excluding code blocks and front matter)
            word_count = _count_words(body)

            # Count links
            link_count = _count_links(body)

            # Derive title: front matter "title" key, or first H1
            title = None
            if front_matter and "title" in front_matter:
                title = str(front_matter["title"])
            elif headings:
                for h in headings:
                    if h["level"] == 1:
                        title = h["text"]
                        break

            return DocumentExtractionResult(
                title=title,
                headings=headings,
                front_matter=front_matter,
                word_count=word_count,
                link_count=link_count,
                code_languages=code_languages,
                format="markdown",
                confidence=1.0,
            )

        except Exception as e:
            return DocumentExtractionResult(
                title=None,
                headings=[],
                front_matter=None,
                word_count=0,
                link_count=0,
                code_languages=[],
                format="markdown",
                confidence=0.0,
                error=f"Markdown extraction failed: {e}",
            )


# ============================================================================
# Avro schema helpers
# ============================================================================


def _avro_type_to_str(avro_type: Any) -> str:
    """Convert an Avro type to a string representation."""
    if isinstance(avro_type, str):
        return avro_type
    if isinstance(avro_type, dict):
        return str(avro_type.get("type", "unknown"))
    if isinstance(avro_type, list):
        # Union type: filter out "null" and return the first non-null type
        non_null = [t for t in avro_type if t != "null"]
        if non_null:
            return _avro_type_to_str(non_null[0])
        return "null"
    return "unknown"


def _avro_schema_to_columns(schema: dict[str, Any]) -> list[dict[str, str]]:
    """Convert an Avro schema to a list of column dicts."""
    columns: list[dict[str, str]] = []
    fields = schema.get("fields", [])

    for field_def in fields:
        name = field_def.get("name", "")
        avro_type = field_def.get("type", "unknown")

        # Detect nullable: union types containing "null"
        nullable = False
        if isinstance(avro_type, list) and "null" in avro_type:
            nullable = True

        columns.append(
            {
                "name": name,
                "type": _avro_type_to_str(avro_type),
                "nullable": str(nullable),
            }
        )

    return columns


# ============================================================================
# Markdown parsing helpers
# ============================================================================


def _parse_front_matter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Parse YAML front matter from Markdown text.

    Returns (front_matter_dict, remaining_body). Front matter values
    are sanitized to JSON-safe primitives (strings, numbers, bools, lists).
    """
    if not text.startswith("---"):
        return None, text

    # Find closing ---
    end_match = re.search(r"\n---\s*\n", text[3:])
    if end_match is None:
        return None, text

    yaml_text = text[3 : 3 + end_match.start()]
    body = text[3 + end_match.end() :]

    try:
        import yaml

        raw = yaml.safe_load(yaml_text)
        if not isinstance(raw, dict):
            return None, text

        # Sanitize to JSON-safe primitives
        sanitized = _sanitize_front_matter(raw)
        return sanitized, body
    except Exception:
        return None, text


def _sanitize_front_matter(data: dict[str, Any]) -> dict[str, Any]:
    """Sanitize front matter values to JSON-safe primitives."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        result[str(key)] = _sanitize_value(value)
    return result


def _sanitize_value(value: Any) -> Any:
    """Convert a value to JSON-safe primitive."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _sanitize_value(v) for k, v in value.items()}
    # datetime, date, or other complex types → string
    return str(value)


def _extract_headings(text: str) -> list[dict[str, Any]]:
    """Extract headings from Markdown body, skipping content in code blocks."""
    lines = text.split("\n")
    headings: list[dict[str, Any]] = []
    in_code_block = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Track fenced code blocks
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            continue

        # ATX headings: # to ######
        atx_match = re.match(r"^(#{1,6})\s+(.+?)(?:\s+#+)?$", line)
        if atx_match:
            headings.append(
                {
                    "level": len(atx_match.group(1)),
                    "text": atx_match.group(2).strip(),
                }
            )
            continue

        # Setext headings: === (H1) or --- (H2)
        if i > 0 and not in_code_block:
            prev_line = lines[i - 1].strip()
            if prev_line and stripped and re.match(r"^={3,}$", stripped):
                headings.append({"level": 1, "text": prev_line})
            elif (
                prev_line
                and stripped
                and re.match(r"^-{3,}$", stripped)
                and (i > 1 or not text.startswith("---"))
            ):
                headings.append({"level": 2, "text": prev_line})

    return headings


def _extract_code_languages(text: str) -> list[str]:
    """Extract language annotations from fenced code blocks."""
    languages: list[str] = []
    for match in re.finditer(r"^```(\w+)", text, re.MULTILINE):
        languages.append(match.group(1).lower())
    for match in re.finditer(r"^~~~(\w+)", text, re.MULTILINE):
        languages.append(match.group(1).lower())
    return languages


def _count_words(text: str) -> int:
    """Count words in Markdown body, excluding code blocks."""
    # Remove fenced code blocks
    no_code = re.sub(r"```[\s\S]*?```", "", text)
    no_code = re.sub(r"~~~[\s\S]*?~~~", "", no_code)
    # Count whitespace-delimited tokens
    words = no_code.split()
    return len(words)


def _count_links(text: str) -> int:
    """Count Markdown links (both inline and reference-style)."""
    # Inline links: [text](url)
    inline = len(re.findall(r"\[.*?\]\(.*?\)", text))
    # Reference links: [text][ref]
    reference = len(re.findall(r"\[.*?\]\[.*?\]", text))
    return inline + reference


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


# ============================================================================
# Document title extractors (Issue #3725 — skeleton index)
# ============================================================================
# All extractors below implement DocumentExtractor and return DocumentExtractionResult
# with only `title` populated.  Other fields (headings, word_count, etc.) are left at
# zero/empty because the skeleton index only needs the title string.
#
# Shared parsing helpers are extracted to avoid duplication across extractor classes.
# ============================================================================


def _load_json_head(content: bytes, max_bytes: int = 2048) -> dict[str, Any] | None:
    """Parse up to max_bytes of JSON content and return the top-level object.

    Returns the first parseable dict, or None if parsing fails (including
    truncation at the max_bytes boundary, binary input, or non-object JSON).
    Never raises.  Used by both JSONExtractor (schema) and JsonDocumentExtractor
    (title) to share the JSON parsing preamble.
    """
    import json as _json

    try:
        sample = content[:max_bytes].decode("utf-8", errors="replace").strip()
        if not sample:
            return None
        obj = _json.loads(sample)
        if isinstance(obj, dict):
            return obj
        # Array: peek at first element
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            return obj[0]
        return None
    except Exception:
        return None


def _null_doc_result(format: str, error: str) -> "DocumentExtractionResult":
    """Return a zero-confidence DocumentExtractionResult with no title."""
    return DocumentExtractionResult(
        title=None,
        headings=[],
        front_matter=None,
        word_count=0,
        link_count=0,
        code_languages=[],
        format=format,
        confidence=0.0,
        error=error,
    )


class JsonDocumentExtractor:
    """JSON/YAML-style title extractor for the skeleton index (Issue #3725).

    Looks for top-level ``name``, ``title``, or ``description`` keys and
    returns the first string value found.  Reads at most max_bytes bytes.
    """

    mime_types: tuple[str, ...] = ("application/json", "text/json")
    extensions: tuple[str, ...] = ("json",)

    def extract(self, content: bytes) -> DocumentExtractionResult:
        """Extract title from top-level name/title/description key."""
        try:
            obj = _load_json_head(content)
            if obj is None:
                return _null_doc_result("json", "No parseable JSON object")

            title: str | None = None
            for key in ("title", "name", "description"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    title = val.strip()
                    break

            return DocumentExtractionResult(
                title=title,
                headings=[],
                front_matter=None,
                word_count=0,
                link_count=0,
                code_languages=[],
                format="json",
                confidence=1.0 if title else 0.5,
            )
        except Exception as e:
            return _null_doc_result("json", f"JSON title extraction failed: {e}")


class YamlDocumentExtractor:
    """YAML title extractor for the skeleton index (Issue #3725).

    Looks for top-level ``name``, ``title``, or ``description`` keys.
    Falls back to the first non-blank line if no key matches.
    """

    mime_types: tuple[str, ...] = ("application/x-yaml", "text/yaml")
    extensions: tuple[str, ...] = ("yaml", "yml", "toml")

    def extract(self, content: bytes) -> DocumentExtractionResult:
        """Extract title from YAML/TOML top-level keys."""
        try:
            text = content.decode("utf-8", errors="replace")
            if not text.strip():
                return _null_doc_result("yaml", "Empty file")

            title: str | None = None

            # Try YAML parse for structured key lookup
            try:
                import yaml as _yaml

                obj = _yaml.safe_load(text)
                if isinstance(obj, dict):
                    for key in ("title", "name", "description"):
                        val = obj.get(key)
                        if isinstance(val, str) and val.strip():
                            title = val.strip()
                            break
            except Exception:
                pass

            # Fallback: first non-blank, non-comment line
            if title is None:
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        title = stripped[:200]  # cap length
                        break

            return DocumentExtractionResult(
                title=title,
                headings=[],
                front_matter=None,
                word_count=0,
                link_count=0,
                code_languages=[],
                format="yaml",
                confidence=1.0 if title else 0.3,
            )
        except Exception as e:
            return _null_doc_result("yaml", f"YAML title extraction failed: {e}")


class PythonDocumentExtractor:
    """Python module title extractor for the skeleton index (Issue #3725).

    Priority order:
    1. Module-level docstring (first non-blank line of the docstring)
    2. First class/function with a docstring (name as title)
    Falls back to NULL if none found.  Skips shebang lines.
    """

    mime_types: tuple[str, ...] = ("text/x-python",)
    extensions: tuple[str, ...] = ("py", "pyi")

    # Matches: def foo(, class Foo(, class Foo:
    _DEF_RE = re.compile(r"^(?:class|def)\s+(\w+)")
    # Matches triple-quoted docstring openings
    _DOCSTRING_RE = re.compile(r'^\s*(?:"""|\'\'\')(.*)$')

    def extract(self, content: bytes) -> DocumentExtractionResult:
        """Extract title from module docstring or first definition."""
        try:
            text = content.decode("utf-8", errors="replace")
            if not text.strip():
                return _null_doc_result("python", "Empty file")

            lines = text.splitlines()
            title: str | None = None

            i = 0
            # Skip shebang and encoding declarations
            while i < len(lines) and i < 3:
                stripped = lines[i].strip()
                if stripped.startswith("#") or not stripped:
                    i += 1
                    continue
                break

            # Check for module docstring at current position
            if i < len(lines):
                m = self._DOCSTRING_RE.match(lines[i])
                if m:
                    # Inline content on the opening line
                    inline = m.group(1).strip().rstrip("'\"").strip()
                    if inline:
                        title = inline
                    else:
                        # Multi-line docstring: take next non-blank line
                        for j in range(i + 1, min(i + 10, len(lines))):
                            candidate = lines[j].strip().rstrip("'\"").strip()
                            if candidate and not candidate.startswith(('"""', "'''")):
                                title = candidate
                                break

            # Fallback: first class/def name
            if title is None:
                for line in lines:
                    m2 = self._DEF_RE.match(line.strip())
                    if m2:
                        title = m2.group(1)
                        break

            return DocumentExtractionResult(
                title=title,
                headings=[],
                front_matter=None,
                word_count=0,
                link_count=0,
                code_languages=["python"],
                format="python",
                confidence=1.0 if title else 0.4,
            )
        except Exception as e:
            return _null_doc_result("python", f"Python title extraction failed: {e}")


class TypeScriptDocumentExtractor:
    """TypeScript/JavaScript title extractor for the skeleton index (Issue #3725).

    Priority order:
    1. JSDoc ``@description`` or ``@fileoverview`` at the top of the file
    2. First exported symbol name (export const/function/class/default)
    Falls back to NULL if none found.
    """

    mime_types: tuple[str, ...] = (
        "application/typescript",
        "text/typescript",
        "application/javascript",
        "text/javascript",
    )
    extensions: tuple[str, ...] = ("ts", "tsx", "js", "jsx", "mjs", "cjs")

    _JSDOC_DESC_RE = re.compile(
        r"@(?:description|fileoverview)\s+(.+?)(?:\s*\*/\s*)?$", re.IGNORECASE
    )
    _EXPORT_RE = re.compile(
        r"^export\s+(?:default\s+)?(?:const|function|class|async\s+function)\s+(\w+)"
    )

    def extract(self, content: bytes) -> DocumentExtractionResult:
        """Extract title from JSDoc description or first exported symbol."""
        try:
            text = content.decode("utf-8", errors="replace")
            if not text.strip():
                return _null_doc_result("typescript", "Empty file")

            title: str | None = None

            # Scan for JSDoc block at top of file (first 30 lines)
            lines = text.splitlines()
            for line in lines[:30]:
                m = self._JSDOC_DESC_RE.search(line)
                if m:
                    title = m.group(1).strip()
                    break

            # Fallback: first exported symbol
            if title is None:
                for line in lines:
                    m2 = self._EXPORT_RE.match(line.strip())
                    if m2:
                        title = m2.group(1)
                        break

            return DocumentExtractionResult(
                title=title,
                headings=[],
                front_matter=None,
                word_count=0,
                link_count=0,
                code_languages=["typescript"],
                format="typescript",
                confidence=1.0 if title else 0.4,
            )
        except Exception as e:
            return _null_doc_result("typescript", f"TypeScript title extraction failed: {e}")


class HtmlDocumentExtractor:
    """HTML title extractor for the skeleton index (Issue #3725).

    Extracts the ``<title>`` tag content.  Falls back to first ``<h1>`` if
    no ``<title>`` tag is present.
    """

    mime_types: tuple[str, ...] = ("text/html",)
    extensions: tuple[str, ...] = ("html", "htm")

    _TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
    _H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
    _TAG_RE = re.compile(r"<[^>]+>")

    def extract(self, content: bytes) -> DocumentExtractionResult:
        """Extract title from <title> or <h1> tag."""
        try:
            text = content.decode("utf-8", errors="replace")
            if not text.strip():
                return _null_doc_result("html", "Empty file")

            title: str | None = None

            m = self._TITLE_RE.search(text)
            if m:
                title = self._TAG_RE.sub("", m.group(1)).strip()

            if not title:
                m2 = self._H1_RE.search(text)
                if m2:
                    title = self._TAG_RE.sub("", m2.group(1)).strip()

            return DocumentExtractionResult(
                title=title or None,
                headings=[],
                front_matter=None,
                word_count=0,
                link_count=0,
                code_languages=[],
                format="html",
                confidence=1.0 if title else 0.3,
            )
        except Exception as e:
            return _null_doc_result("html", f"HTML title extraction failed: {e}")


# ---------------------------------------------------------------------------
# Skeleton extractor registry
# ---------------------------------------------------------------------------
# Indexed by lowercase file extension.  Used by SkeletonIndexer to dispatch
# title extraction without importing individual extractor classes.

SKELETON_EXTRACTOR_REGISTRY: dict[str, DocumentExtractor] = {
    ext: extractor
    for extractor in (
        MarkdownExtractor(),
        JsonDocumentExtractor(),
        YamlDocumentExtractor(),
        PythonDocumentExtractor(),
        TypeScriptDocumentExtractor(),
        HtmlDocumentExtractor(),
    )
    for ext in extractor.extensions
}
