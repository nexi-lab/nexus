"""Unit tests for shared messaging primitives."""

from __future__ import annotations

from nexus.core.messaging import (
    DataPart,
    FilePart,
    MessageMetadata,
    Part,
    TextPart,
)


class TestMessageMetadata:
    def test_defaults(self) -> None:
        meta = MessageMetadata()
        assert meta.correlation_id is None
        assert meta.timestamp is not None
        assert meta.ttl_seconds is None
        assert meta.version == "1.0"

    def test_with_values(self) -> None:
        meta = MessageMetadata(correlation_id="corr-1", ttl_seconds=60, version="2.0")
        assert meta.correlation_id == "corr-1"
        assert meta.ttl_seconds == 60
        assert meta.version == "2.0"


class TestPartReexports:
    def test_text_part_available(self) -> None:
        p = TextPart(text="hello")
        assert p.type == "text"

    def test_file_part_available(self) -> None:
        from nexus.a2a.models import FileContent

        p = FilePart(file=FileContent(name="test.txt"))
        assert p.type == "file"

    def test_data_part_available(self) -> None:
        p = DataPart(data={"key": "value"})
        assert p.type == "data"

    def test_part_union_available(self) -> None:
        # Part should be the Union type
        assert Part is not None
