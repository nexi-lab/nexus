"""Tests for catalog schema extractors — CSV, JSON, Parquet (Issue #2929).

Parametrized test matrix covering happy path, edge cases, and error
conditions for each extractor (Issue #11, Test Review).
"""

import json

import pytest

from nexus.bricks.catalog.extractors import (
    AvroExtractor,
    CSVExtractor,
    DocumentExtractionResult,
    ExtractionResult,
    HtmlDocumentExtractor,
    JsonDocumentExtractor,
    JSONExtractor,
    MarkdownExtractor,
    ParquetExtractor,
    PythonDocumentExtractor,
    TypeScriptDocumentExtractor,
    YamlDocumentExtractor,
    _load_json_head,
)


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401

        return True
    except ImportError:
        return False


def _has_fastavro() -> bool:
    try:
        import fastavro  # noqa: F401

        return True
    except ImportError:
        return False


# ============================================================================
# CSV Extractor Tests
# ============================================================================


class TestCSVExtractor:
    """CSV/TSV schema extraction."""

    def test_simple_csv(self) -> None:
        content = b"name,age,city\nAlice,30,NYC\nBob,25,LA\n"
        result = CSVExtractor().extract(content)

        assert result.format == "csv"
        assert result.error is None
        assert result.schema is not None
        assert len(result.schema) == 3
        assert result.schema[0]["name"] == "name"
        assert result.schema[1]["name"] == "age"
        assert result.confidence > 0.5

    def test_type_inference(self) -> None:
        content = b"id,score,active\n1,3.14,true\n2,2.72,false\n"
        result = CSVExtractor().extract(content)

        assert result.schema is not None
        types = {c["name"]: c["type"] for c in result.schema}
        assert types["id"] == "integer"
        assert types["score"] == "float"
        assert types["active"] == "boolean"

    def test_empty_csv(self) -> None:
        result = CSVExtractor().extract(b"")
        assert result.error is not None
        assert result.schema is None

    def test_header_only_csv(self) -> None:
        result = CSVExtractor().extract(b"name,age,city\n")
        assert result.schema is not None
        assert result.row_count == 0

    def test_no_header_empty_columns(self) -> None:
        result = CSVExtractor().extract(b",,,\n1,2,3,4\n")
        # All headers are empty — extractor correctly rejects this
        assert result.error is not None

    def test_tab_delimited(self) -> None:
        content = b"name\tage\nAlice\t30\nBob\t25\n"
        result = CSVExtractor().extract(content)
        assert result.schema is not None
        assert len(result.schema) == 2

    def test_mixed_types_resolve_to_string(self) -> None:
        content = b"col\n1\nhello\n3.14\n"
        result = CSVExtractor().extract(content)
        assert result.schema is not None
        # Mixed int/string/float → string
        assert result.schema[0]["type"] == "string"

    def test_unicode_headers(self) -> None:
        content = "名前,年齢\nアリス,30\n".encode()
        result = CSVExtractor().extract(content)
        assert result.schema is not None
        assert result.schema[0]["name"] == "名前"

    def test_max_rows_bounded(self) -> None:
        rows = ["id,val"] + [f"{i},{i * 2}" for i in range(100)]
        content = "\n".join(rows).encode()
        result = CSVExtractor(max_rows=10).extract(content)
        assert result.row_count == 10

    def test_large_file_warning(self) -> None:
        rows = ["id,val"] + [f"{i},{i * 2}" for i in range(1000)]
        content = "\n".join(rows).encode()
        result = CSVExtractor(max_bytes=100).extract(content)
        assert any("sampled" in w for w in result.warnings)

    def test_binary_content_graceful(self) -> None:
        content = bytes(range(256))
        result = CSVExtractor().extract(content)
        # Should not raise, may have error or partial result
        assert isinstance(result, ExtractionResult)

    def test_single_column(self) -> None:
        content = b"values\n1\n2\n3\n"
        result = CSVExtractor().extract(content)
        assert result.schema is not None
        assert len(result.schema) == 1


# ============================================================================
# JSON Extractor Tests
# ============================================================================


class TestJSONExtractor:
    """JSON/NDJSON schema extraction."""

    def test_json_array(self) -> None:
        data = [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]
        content = json.dumps(data).encode()
        result = JSONExtractor().extract(content)

        assert result.format == "json"
        assert result.error is None
        assert result.schema is not None
        assert len(result.schema) == 2
        assert result.row_count == 2

    def test_ndjson(self) -> None:
        lines = [
            json.dumps({"id": 1, "name": "Alice"}),
            json.dumps({"id": 2, "name": "Bob"}),
        ]
        content = "\n".join(lines).encode()
        result = JSONExtractor().extract(content)

        assert result.schema is not None
        assert result.row_count == 2

    def test_single_object(self) -> None:
        content = json.dumps({"key": "value", "count": 42}).encode()
        result = JSONExtractor().extract(content)

        assert result.schema is not None
        assert result.row_count == 1

    def test_empty_json(self) -> None:
        result = JSONExtractor().extract(b"")
        assert result.error is not None
        assert result.schema is None

    def test_invalid_json(self) -> None:
        result = JSONExtractor().extract(b"not json at all")
        assert result.error is not None

    def test_json_type_inference(self) -> None:
        data = [{"i": 1, "f": 3.14, "b": True, "s": "hello", "n": None}]
        content = json.dumps(data).encode()
        result = JSONExtractor().extract(content)

        assert result.schema is not None
        types = {c["name"]: c["type"] for c in result.schema}
        assert types["i"] == "integer"
        assert types["f"] == "float"
        assert types["b"] == "boolean"
        assert types["s"] == "string"
        # null-only columns resolve to "string" via _resolve_types
        assert types["n"] == "string"

    def test_nested_objects(self) -> None:
        data = [{"name": "Alice", "address": {"city": "NYC"}}]
        content = json.dumps(data).encode()
        result = JSONExtractor().extract(content)

        assert result.schema is not None
        types = {c["name"]: c["type"] for c in result.schema}
        assert types["address"] == "object"

    def test_arrays_in_json(self) -> None:
        data = [{"tags": ["a", "b"], "name": "test"}]
        content = json.dumps(data).encode()
        result = JSONExtractor().extract(content)

        assert result.schema is not None
        types = {c["name"]: c["type"] for c in result.schema}
        assert types["tags"] == "array"

    def test_max_records_bounded(self) -> None:
        data = [{"id": i} for i in range(100)]
        content = json.dumps(data).encode()
        result = JSONExtractor(max_records=10).extract(content)
        assert result.row_count == 10

    def test_non_utf8_graceful(self) -> None:
        content = b"\xff\xfe" + b"not utf8"
        result = JSONExtractor().extract(content)
        assert isinstance(result, ExtractionResult)

    def test_json_array_of_non_objects(self) -> None:
        content = json.dumps([1, 2, 3]).encode()
        result = JSONExtractor().extract(content)
        # Array of primitives — no dict records
        assert result.schema is None or result.row_count == 0


# ============================================================================
# Parquet Extractor Tests
# ============================================================================


class TestParquetExtractor:
    """Parquet schema extraction."""

    def test_missing_pyarrow_graceful(self) -> None:
        """If pyarrow is not installed, returns error gracefully."""
        # We can't easily uninstall pyarrow, but we can test with invalid content
        result = ParquetExtractor().extract(b"not a parquet file")
        assert result.error is not None
        assert result.format == "parquet"

    def test_empty_content(self) -> None:
        result = ParquetExtractor().extract(b"")
        assert result.error is not None

    @pytest.mark.skipif(
        not _has_pyarrow(),
        reason="pyarrow not installed",
    )
    def test_valid_parquet(self) -> None:
        """Test with a real Parquet file if pyarrow is available."""
        import io

        import pyarrow as pa
        import pyarrow.parquet as pq

        # Create a small Parquet file in memory
        table = pa.table(
            {
                "id": [1, 2, 3],
                "name": ["Alice", "Bob", "Charlie"],
                "score": [3.14, 2.72, 1.41],
            }
        )
        buf = io.BytesIO()
        pq.write_table(table, buf)
        content = buf.getvalue()

        result = ParquetExtractor().extract(content)
        assert result.error is None
        assert result.format == "parquet"
        assert result.confidence == 1.0
        assert result.schema is not None
        assert len(result.schema) == 3
        assert result.row_count == 3

        names = {c["name"] for c in result.schema}
        assert names == {"id", "name", "score"}


# ============================================================================
# ExtractionResult Tests
# ============================================================================


class TestExtractionResult:
    """ExtractionResult value type tests."""

    def test_success_result(self) -> None:
        result = ExtractionResult(
            schema=[{"name": "id", "type": "integer"}],
            format="csv",
            confidence=0.95,
            row_count=100,
        )
        assert result.error is None
        assert result.warnings == []

    def test_error_result(self) -> None:
        result = ExtractionResult(
            schema=None,
            format="unknown",
            confidence=0.0,
            error="Could not parse",
        )
        assert result.schema is None
        assert result.error is not None

    def test_frozen(self) -> None:
        result = ExtractionResult(schema=None, format="csv", confidence=0.0)
        with pytest.raises(AttributeError):
            result.format = "json"


# ============================================================================
# Avro Extractor Tests
# ============================================================================


class TestAvroExtractor:
    """Avro schema extraction."""

    def test_invalid_content(self) -> None:
        result = AvroExtractor().extract(b"not an avro file")
        assert result.error is not None
        assert result.format == "avro"

    def test_empty_content(self) -> None:
        result = AvroExtractor().extract(b"")
        assert result.error is not None

    @pytest.mark.skipif(not _has_fastavro(), reason="fastavro not installed")
    def test_valid_avro(self) -> None:
        """Test with a real Avro file."""
        import io

        import fastavro

        schema = {
            "type": "record",
            "name": "User",
            "fields": [
                {"name": "id", "type": "int"},
                {"name": "name", "type": "string"},
                {"name": "score", "type": "double"},
            ],
        }
        records = [
            {"id": 1, "name": "Alice", "score": 3.14},
            {"id": 2, "name": "Bob", "score": 2.72},
        ]
        buf = io.BytesIO()
        fastavro.writer(buf, schema, records)
        content = buf.getvalue()

        result = AvroExtractor().extract(content)
        assert result.error is None
        assert result.format == "avro"
        assert result.confidence == 1.0
        assert result.schema is not None
        assert len(result.schema) == 3
        assert result.row_count is None  # O(1) header-only — no row counting

        names = {c["name"] for c in result.schema}
        assert names == {"id", "name", "score"}

    @pytest.mark.skipif(not _has_fastavro(), reason="fastavro not installed")
    def test_nullable_union_types(self) -> None:
        """Avro union types like ["null", "string"] should be detected as nullable."""
        import io

        import fastavro

        schema = {
            "type": "record",
            "name": "NullableTest",
            "fields": [
                {"name": "required_id", "type": "int"},
                {"name": "optional_name", "type": ["null", "string"]},
            ],
        }
        records = [{"required_id": 1, "optional_name": "Alice"}]
        buf = io.BytesIO()
        fastavro.writer(buf, schema, records)
        content = buf.getvalue()

        result = AvroExtractor().extract(content)
        assert result.schema is not None
        cols = {c["name"]: c for c in result.schema}
        assert cols["required_id"]["nullable"] == "False"
        assert cols["optional_name"]["nullable"] == "True"
        assert cols["optional_name"]["type"] == "string"

    @pytest.mark.skipif(not _has_fastavro(), reason="fastavro not installed")
    def test_nested_record_type(self) -> None:
        """Avro nested record fields should be typed as 'record'."""
        import io

        import fastavro

        schema = {
            "type": "record",
            "name": "Parent",
            "fields": [
                {"name": "id", "type": "int"},
                {
                    "name": "address",
                    "type": {
                        "type": "record",
                        "name": "Address",
                        "fields": [
                            {"name": "city", "type": "string"},
                        ],
                    },
                },
            ],
        }
        records = [{"id": 1, "address": {"city": "NYC"}}]
        buf = io.BytesIO()
        fastavro.writer(buf, schema, records)
        content = buf.getvalue()

        result = AvroExtractor().extract(content)
        assert result.schema is not None
        types = {c["name"]: c["type"] for c in result.schema}
        assert types["address"] == "record"

    @pytest.mark.skipif(not _has_fastavro(), reason="fastavro not installed")
    def test_array_type(self) -> None:
        """Avro array fields should be typed as 'array'."""
        import io

        import fastavro

        schema = {
            "type": "record",
            "name": "ArrayTest",
            "fields": [
                {"name": "tags", "type": {"type": "array", "items": "string"}},
            ],
        }
        records = [{"tags": ["a", "b"]}]
        buf = io.BytesIO()
        fastavro.writer(buf, schema, records)
        content = buf.getvalue()

        result = AvroExtractor().extract(content)
        assert result.schema is not None
        assert result.schema[0]["type"] == "array"

    @pytest.mark.skipif(not _has_fastavro(), reason="fastavro not installed")
    def test_enum_type(self) -> None:
        """Avro enum fields."""
        import io

        import fastavro

        schema = {
            "type": "record",
            "name": "EnumTest",
            "fields": [
                {
                    "name": "color",
                    "type": {
                        "type": "enum",
                        "name": "Color",
                        "symbols": ["RED", "GREEN", "BLUE"],
                    },
                },
            ],
        }
        records = [{"color": "RED"}]
        buf = io.BytesIO()
        fastavro.writer(buf, schema, records)
        content = buf.getvalue()

        result = AvroExtractor().extract(content)
        assert result.schema is not None
        assert result.schema[0]["type"] == "enum"

    @pytest.mark.skipif(not _has_fastavro(), reason="fastavro not installed")
    def test_schema_only_no_records(self) -> None:
        """Avro file with schema but no data records."""
        import io

        import fastavro

        schema = {
            "type": "record",
            "name": "Empty",
            "fields": [
                {"name": "id", "type": "int"},
            ],
        }
        buf = io.BytesIO()
        fastavro.writer(buf, schema, [])
        content = buf.getvalue()

        result = AvroExtractor().extract(content)
        assert result.error is None
        assert result.schema is not None
        assert len(result.schema) == 1
        assert result.row_count is None  # O(1) header-only — no row counting

    @pytest.mark.skipif(not _has_fastavro(), reason="fastavro not installed")
    def test_map_type(self) -> None:
        """Avro map fields should be typed as 'map'."""
        import io

        import fastavro

        schema = {
            "type": "record",
            "name": "MapTest",
            "fields": [
                {"name": "attrs", "type": {"type": "map", "values": "string"}},
            ],
        }
        records = [{"attrs": {"key1": "val1"}}]
        buf = io.BytesIO()
        fastavro.writer(buf, schema, records)
        content = buf.getvalue()

        result = AvroExtractor().extract(content)
        assert result.schema is not None
        assert result.schema[0]["type"] == "map"

    def test_self_registration_metadata(self) -> None:
        """AvroExtractor declares mime_types and extensions."""
        ext = AvroExtractor()
        assert "application/avro" in ext.mime_types
        assert "avro" in ext.extensions


# ============================================================================
# Markdown Extractor Tests
# ============================================================================


class TestMarkdownExtractor:
    """Markdown document structure extraction."""

    def test_simple_markdown(self) -> None:
        content = b"# Hello World\n\nThis is a test.\n"
        result = MarkdownExtractor().extract(content)

        assert result.format == "markdown"
        assert result.error is None
        assert result.confidence == 1.0
        assert result.title == "Hello World"
        assert len(result.headings) == 1
        assert result.headings[0]["level"] == 1
        assert result.word_count > 0

    def test_empty_markdown(self) -> None:
        result = MarkdownExtractor().extract(b"")
        assert result.error is not None
        assert result.confidence == 0.0

    def test_whitespace_only(self) -> None:
        result = MarkdownExtractor().extract(b"   \n  \n  ")
        assert result.error is not None

    def test_yaml_front_matter(self) -> None:
        content = b"---\ntitle: My Document\nauthor: Alice\ntags:\n  - python\n  - testing\n---\n\n# Content\n\nBody text here.\n"
        result = MarkdownExtractor().extract(content)

        assert result.front_matter is not None
        assert result.front_matter["title"] == "My Document"
        assert result.front_matter["author"] == "Alice"
        assert result.front_matter["tags"] == ["python", "testing"]
        # Title from front matter takes priority
        assert result.title == "My Document"

    def test_invalid_yaml_front_matter_graceful(self) -> None:
        content = b"---\ninvalid: yaml: content: [[\n---\n\n# Heading\n"
        result = MarkdownExtractor().extract(content)
        # Should not crash — front matter is None, but heading is still extracted
        assert result.error is None
        assert len(result.headings) >= 1

    def test_no_front_matter(self) -> None:
        content = b"# Just a heading\n\nSome text.\n"
        result = MarkdownExtractor().extract(content)
        assert result.front_matter is None
        assert result.title == "Just a heading"

    def test_multiple_heading_levels(self) -> None:
        content = b"# H1\n## H2\n### H3\n#### H4\n##### H5\n###### H6\n"
        result = MarkdownExtractor().extract(content)

        assert len(result.headings) == 6
        for i, h in enumerate(result.headings, 1):
            assert h["level"] == i
            assert h["text"] == f"H{i}"

    def test_setext_headings(self) -> None:
        content = b"Heading One\n===\n\nHeading Two\n---\n"
        result = MarkdownExtractor().extract(content)

        assert len(result.headings) == 2
        assert result.headings[0]["level"] == 1
        assert result.headings[0]["text"] == "Heading One"
        assert result.headings[1]["level"] == 2
        assert result.headings[1]["text"] == "Heading Two"

    def test_heading_inside_code_block_ignored(self) -> None:
        content = b"# Real Heading\n\n```python\n# This is a comment, not a heading\ndef foo():\n    pass\n```\n"
        result = MarkdownExtractor().extract(content)

        assert len(result.headings) == 1
        assert result.headings[0]["text"] == "Real Heading"

    def test_code_block_languages(self) -> None:
        content = b"```python\nprint('hello')\n```\n\n```rust\nfn main() {}\n```\n\n```python\nx = 1\n```\n"
        result = MarkdownExtractor().extract(content)

        assert len(result.code_languages) == 3
        assert result.code_languages[0] == "python"
        assert result.code_languages[1] == "rust"
        assert result.code_languages[2] == "python"

    def test_code_block_no_language(self) -> None:
        content = b"```\nsome code\n```\n"
        result = MarkdownExtractor().extract(content)
        # No language annotation — should not appear in list
        assert len(result.code_languages) == 0

    def test_link_count(self) -> None:
        content = b"[link1](http://a.com) and [link2](http://b.com)\n\nAlso [ref][1]\n\n[1]: http://c.com\n"
        result = MarkdownExtractor().extract(content)
        assert result.link_count >= 2

    def test_word_count_excludes_code_blocks(self) -> None:
        content = b"Hello world.\n\n```python\nthis_is_code = True\nmore_code_here = False\n```\n\nGoodbye world.\n"
        result = MarkdownExtractor().extract(content)
        # "Hello world." + "Goodbye world." = ~4 words, code excluded
        assert result.word_count >= 2
        assert result.word_count < 10  # Code words should not be counted

    def test_unicode_headings(self) -> None:
        content = "# 日本語の見出し\n\n本文テキスト。\n".encode()
        result = MarkdownExtractor().extract(content)
        assert result.title == "日本語の見出し"

    def test_front_matter_date_sanitized(self) -> None:
        """YAML dates should be sanitized to strings."""
        content = b"---\ntitle: Test\ndate: 2026-01-15\n---\n\n# Content\n"
        result = MarkdownExtractor().extract(content)
        assert result.front_matter is not None
        # The date should be a string (sanitized), not a datetime object
        date_val = result.front_matter.get("date")
        assert isinstance(date_val, str)

    def test_self_registration_metadata(self) -> None:
        """MarkdownExtractor declares mime_types and extensions."""
        ext = MarkdownExtractor()
        assert "text/markdown" in ext.mime_types
        assert "md" in ext.extensions
        assert "markdown" in ext.extensions

    def test_title_from_first_h1_when_no_front_matter(self) -> None:
        content = b"## Not a title\n\n# Actual Title\n\nBody.\n"
        result = MarkdownExtractor().extract(content)
        assert result.title == "Actual Title"


# ============================================================================
# DocumentExtractionResult Tests
# ============================================================================


class TestDocumentExtractionResult:
    """DocumentExtractionResult value type tests."""

    def test_success_result(self) -> None:
        result = DocumentExtractionResult(
            title="Test",
            headings=[{"level": 1, "text": "Test"}],
            front_matter=None,
            word_count=10,
            link_count=2,
            code_languages=["python"],
            format="markdown",
            confidence=1.0,
        )
        assert result.error is None
        assert result.warnings == []

    def test_frozen(self) -> None:
        result = DocumentExtractionResult(
            title=None,
            headings=[],
            front_matter=None,
            word_count=0,
            link_count=0,
            code_languages=[],
            format="markdown",
            confidence=0.0,
        )
        with pytest.raises(AttributeError):
            result.format = "other"


# ============================================================================
# Self-Registration Metadata Tests
# ============================================================================


class TestExtractorSelfRegistration:
    """Verify all extractors declare mime_types and extensions."""

    def test_csv_extractor_metadata(self) -> None:
        ext = CSVExtractor()
        assert "text/csv" in ext.mime_types
        assert "csv" in ext.extensions

    def test_json_extractor_metadata(self) -> None:
        ext = JSONExtractor()
        assert "application/json" in ext.mime_types
        assert "json" in ext.extensions

    def test_parquet_extractor_metadata(self) -> None:
        ext = ParquetExtractor()
        assert "application/parquet" in ext.mime_types
        assert "parquet" in ext.extensions


# ============================================================================
# _load_json_head helper (Issue #3725 — 8A)
# ============================================================================


class TestLoadJsonHead:
    def test_returns_dict_for_simple_object(self) -> None:
        content = b'{"name": "acme", "version": "1.0"}'
        result = _load_json_head(content)
        assert result == {"name": "acme", "version": "1.0"}

    def test_returns_first_element_for_array(self) -> None:
        content = b'[{"title": "first"}, {"title": "second"}]'
        result = _load_json_head(content)
        assert result == {"title": "first"}

    def test_returns_none_for_empty_input(self) -> None:
        assert _load_json_head(b"") is None

    def test_returns_none_for_truncated_json(self) -> None:
        # 2KB boundary cuts mid-object — must not raise
        truncated = b'{"name": "acme", "long_key": "' + b"x" * 3000
        result = _load_json_head(truncated, max_bytes=50)
        assert result is None  # graceful degradation

    def test_returns_none_for_binary_input(self) -> None:
        binary = bytes(range(256))
        assert _load_json_head(binary) is None

    def test_returns_none_for_plain_string_json(self) -> None:
        # Valid JSON but not a dict/array-of-dicts
        assert _load_json_head(b'"just a string"') is None


# ============================================================================
# JsonDocumentExtractor (Issue #3725 — skeleton title extraction)
# ============================================================================


class TestJsonDocumentExtractor:
    def setup_method(self) -> None:
        self.ext = JsonDocumentExtractor()

    # Happy path
    def test_extracts_title_key(self) -> None:
        content = b'{"title": "My Project", "version": "2.0"}'
        result = self.ext.extract(content)
        assert result.title == "My Project"
        assert result.error is None

    def test_prefers_title_over_name(self) -> None:
        content = b'{"title": "Title Value", "name": "Name Value"}'
        result = self.ext.extract(content)
        assert result.title == "Title Value"

    def test_falls_back_to_name_if_no_title(self) -> None:
        content = b'{"name": "my-package", "version": "1.2.3"}'
        result = self.ext.extract(content)
        assert result.title == "my-package"

    def test_falls_back_to_description(self) -> None:
        content = b'{"description": "A useful tool", "scripts": {}}'
        result = self.ext.extract(content)
        assert result.title == "A useful tool"

    # Adversarial / edge cases
    def test_name_is_integer_returns_none(self) -> None:
        # name=42 is not a string — should not set title
        content = b'{"name": 42, "version": "1.0"}'
        result = self.ext.extract(content)
        assert result.title is None

    def test_empty_content(self) -> None:
        result = self.ext.extract(b"")
        assert result.title is None
        assert result.error is not None

    def test_truncated_at_2kb_does_not_raise(self) -> None:
        # Simulate what SkeletonIndexer passes: content[:2048] that ends mid-JSON
        payload = (b'{"name": "partial", "data": "' + b"x" * 2000 + b'"')[:2048]
        result = self.ext.extract(payload)
        # May or may not extract a title, but must never raise
        assert isinstance(result, DocumentExtractionResult)

    def test_binary_blob_does_not_raise(self) -> None:
        result = self.ext.extract(bytes(range(256)))
        assert isinstance(result, DocumentExtractionResult)


# ============================================================================
# YamlDocumentExtractor (Issue #3725)
# ============================================================================


class TestYamlDocumentExtractor:
    def setup_method(self) -> None:
        self.ext = YamlDocumentExtractor()

    # Happy path
    def test_extracts_title_key(self) -> None:
        content = b"title: My YAML Title\nversion: 1.0\n"
        result = self.ext.extract(content)
        assert result.title == "My YAML Title"

    def test_extracts_name_key(self) -> None:
        content = b"name: my-chart\ndescription: A Helm chart\n"
        result = self.ext.extract(content)
        assert result.title == "my-chart"

    # Adversarial
    def test_empty_file_returns_null_title(self) -> None:
        result = self.ext.extract(b"")
        assert result.title is None

    def test_comment_only_file_returns_null_title(self) -> None:
        content = b"# just a comment\n# another comment\n"
        result = self.ext.extract(content)
        # All lines start with # → no title extractable
        assert result.title is None

    def test_binary_blob_does_not_raise(self) -> None:
        result = self.ext.extract(bytes(range(256)))
        assert isinstance(result, DocumentExtractionResult)

    def test_truncated_yaml_does_not_raise(self) -> None:
        # Long YAML cut mid-value
        content = (b"title: " + b"x" * 3000)[:2048]
        result = self.ext.extract(content)
        assert isinstance(result, DocumentExtractionResult)


# ============================================================================
# PythonDocumentExtractor (Issue #3725)
# ============================================================================


class TestPythonDocumentExtractor:
    def setup_method(self) -> None:
        self.ext = PythonDocumentExtractor()

    # Happy path
    def test_module_docstring_inline(self) -> None:
        content = b'"""Authentication middleware for session management."""\n\nimport os\n'
        result = self.ext.extract(content)
        assert result.title == "Authentication middleware for session management."

    def test_module_docstring_multiline(self) -> None:
        content = b'"""\nOAuth2 provider integration.\n\nDetails follow.\n"""\n'
        result = self.ext.extract(content)
        assert result.title == "OAuth2 provider integration."

    def test_falls_back_to_class_name(self) -> None:
        content = b"# no docstring\nclass AuthHandler:\n    pass\n"
        result = self.ext.extract(content)
        assert result.title == "AuthHandler"

    def test_falls_back_to_function_name(self) -> None:
        content = b"# no docstring\ndef parse_user_auth(token):\n    pass\n"
        result = self.ext.extract(content)
        assert result.title == "parse_user_auth"

    # Adversarial
    def test_shebang_skipped(self) -> None:
        content = b'#!/usr/bin/env python3\n\n"""Real module docstring."""\n'
        result = self.ext.extract(content)
        assert result.title == "Real module docstring."

    def test_empty_file_returns_null_title(self) -> None:
        result = self.ext.extract(b"")
        assert result.title is None

    def test_binary_blob_does_not_raise(self) -> None:
        result = self.ext.extract(bytes(range(256)))
        assert isinstance(result, DocumentExtractionResult)

    def test_truncated_docstring_does_not_raise(self) -> None:
        # Docstring opening with no closing quotes (truncated at 2KB)
        content = b'"""' + b"x" * 2045  # no closing """
        result = self.ext.extract(content)
        assert isinstance(result, DocumentExtractionResult)


# ============================================================================
# TypeScriptDocumentExtractor (Issue #3725)
# ============================================================================


class TestTypeScriptDocumentExtractor:
    def setup_method(self) -> None:
        self.ext = TypeScriptDocumentExtractor()

    # Happy path
    def test_jsdoc_description(self) -> None:
        content = (
            b"/**\n * @description User authentication service\n */\nexport class AuthService {}\n"
        )
        result = self.ext.extract(content)
        assert result.title == "User authentication service"

    def test_jsdoc_fileoverview(self) -> None:
        content = (
            b"/**\n * @fileoverview OAuth middleware module\n */\nimport { foo } from './bar';\n"
        )
        result = self.ext.extract(content)
        assert result.title == "OAuth middleware module"

    def test_falls_back_to_exported_class(self) -> None:
        content = b"export class LoginHandler {\n  handle() {}\n}\n"
        result = self.ext.extract(content)
        assert result.title == "LoginHandler"

    def test_falls_back_to_exported_function(self) -> None:
        content = b"export function parseUserToken(token: string) {\n  return null;\n}\n"
        result = self.ext.extract(content)
        assert result.title == "parseUserToken"

    # Adversarial
    def test_empty_file_returns_null_title(self) -> None:
        result = self.ext.extract(b"")
        assert result.title is None

    def test_binary_blob_does_not_raise(self) -> None:
        result = self.ext.extract(bytes(range(256)))
        assert isinstance(result, DocumentExtractionResult)

    def test_no_exports_returns_null_title(self) -> None:
        content = b"const x = 1;\nconst y = 2;\n"
        result = self.ext.extract(content)
        assert result.title is None


# ============================================================================
# HtmlDocumentExtractor (Issue #3725)
# ============================================================================


class TestHtmlDocumentExtractor:
    def setup_method(self) -> None:
        self.ext = HtmlDocumentExtractor()

    # Happy path
    def test_extracts_title_tag(self) -> None:
        content = b"<html><head><title>Authentication Guide</title></head></html>"
        result = self.ext.extract(content)
        assert result.title == "Authentication Guide"

    def test_falls_back_to_h1(self) -> None:
        content = b"<html><body><h1>OAuth2 Flow</h1></body></html>"
        result = self.ext.extract(content)
        assert result.title == "OAuth2 Flow"

    def test_strips_html_tags_from_title(self) -> None:
        content = b"<html><head><title><em>Welcome</em> Guide</title></head></html>"
        result = self.ext.extract(content)
        assert "Welcome" in (result.title or "")

    # Adversarial
    def test_empty_file_returns_null_title(self) -> None:
        result = self.ext.extract(b"")
        assert result.title is None

    def test_no_title_or_h1_returns_null(self) -> None:
        content = b"<html><body><p>No headings here.</p></body></html>"
        result = self.ext.extract(content)
        assert result.title is None

    def test_binary_blob_does_not_raise(self) -> None:
        result = self.ext.extract(bytes(range(256)))
        assert isinstance(result, DocumentExtractionResult)


# ============================================================================
# MarkdownExtractor adversarial (augments existing happy-path tests)
# ============================================================================


class TestMarkdownExtractorAdversarial:
    def setup_method(self) -> None:
        self.ext = MarkdownExtractor()

    def test_empty_file_returns_null_title(self) -> None:
        result = self.ext.extract(b"")
        assert result.title is None
        assert result.confidence == 0.0

    def test_truncated_at_2kb_does_not_raise(self) -> None:
        # Markdown with an H1 near the 2KB boundary
        long_md = b"# Title\n\n" + b"x " * 1000
        result = self.ext.extract(long_md[:2048])
        assert isinstance(result, DocumentExtractionResult)
        assert result.title == "Title"

    def test_binary_blob_does_not_raise(self) -> None:
        result = self.ext.extract(bytes(range(256)))
        assert isinstance(result, DocumentExtractionResult)
