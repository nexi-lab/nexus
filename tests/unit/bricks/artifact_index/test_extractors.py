"""Tests for artifact content extraction (~15 edge cases)."""

import json

from nexus.bricks.artifact_index.extractors import extract_content
from tests.unit.bricks.artifact_index.conftest import (
    StubArtifact,
    StubDataPart,
    StubFileContent,
    StubFilePart,
    StubTextPart,
)


class TestExtractContentTextPart:
    """TextPart extraction."""

    def test_single_text_part(self) -> None:
        artifact = StubArtifact(
            artifactId="art-1",
            parts=[StubTextPart(text="hello world")],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        assert result.text == "hello world"
        assert result.artifact_id == "art-1"
        assert result.task_id == "t1"
        assert result.zone_id == "z1"

    def test_multiple_text_parts_joined_by_newline(self) -> None:
        artifact = StubArtifact(
            artifactId="art-2",
            parts=[StubTextPart(text="line1"), StubTextPart(text="line2")],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        assert result.text == "line1\nline2"

    def test_empty_text_part(self) -> None:
        artifact = StubArtifact(
            artifactId="art-3",
            parts=[StubTextPart(text="")],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        assert result.text == ""

    def test_unicode_text(self) -> None:
        artifact = StubArtifact(
            artifactId="art-4",
            parts=[StubTextPart(text="日本語テスト 🎉")],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        assert "日本語" in result.text
        assert "🎉" in result.text


class TestExtractContentFilePart:
    """FilePart extraction."""

    def test_file_part_with_url(self) -> None:
        artifact = StubArtifact(
            artifactId="art-5",
            parts=[StubFilePart(file=StubFileContent(url="https://example.com/f.txt"))],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        assert result.text == "https://example.com/f.txt"

    def test_file_part_with_name_only(self) -> None:
        artifact = StubArtifact(
            artifactId="art-6",
            parts=[StubFilePart(file=StubFileContent(name="data.csv"))],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        assert result.text == "file:data.csv"

    def test_file_part_no_url_no_name(self) -> None:
        artifact = StubArtifact(
            artifactId="art-7",
            parts=[StubFilePart(file=StubFileContent())],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        assert result.text == "file:unknown"


class TestExtractContentDataPart:
    """DataPart extraction."""

    def test_data_part_serialized_to_json(self) -> None:
        data = {"key": "value", "num": 42}
        artifact = StubArtifact(
            artifactId="art-8",
            parts=[StubDataPart(data=data)],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        parsed = json.loads(result.text)
        assert parsed == data

    def test_empty_data_part(self) -> None:
        artifact = StubArtifact(
            artifactId="art-9",
            parts=[StubDataPart(data={})],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        assert result.text == "{}"


class TestExtractContentMixed:
    """Mixed part types and metadata."""

    def test_mixed_parts(self) -> None:
        artifact = StubArtifact(
            artifactId="art-10",
            parts=[
                StubTextPart(text="intro"),
                StubDataPart(data={"x": 1}),
                StubFilePart(file=StubFileContent(url="http://f.com/a")),
            ],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        assert "intro" in result.text
        assert '"x": 1' in result.text
        assert "http://f.com/a" in result.text

    def test_artifact_metadata_merged(self) -> None:
        artifact = StubArtifact(
            artifactId="art-11",
            name="report",
            description="quarterly",
            metadata={"source": "agent-1"},
            parts=[StubTextPart(text="data", metadata={"fmt": "md"})],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        assert result.metadata["source"] == "agent-1"
        assert result.metadata["artifact_name"] == "report"
        assert result.metadata["artifact_description"] == "quarterly"
        assert result.metadata["fmt"] == "md"

    def test_empty_parts_list(self) -> None:
        artifact = StubArtifact(artifactId="art-12", parts=[])
        result = extract_content(artifact, task_id="t1", zone_id="z1")
        assert result.text == ""


class TestExtractContentTruncation:
    """Max content bytes truncation."""

    def test_truncation_at_max_bytes(self) -> None:
        long_text = "a" * 200
        artifact = StubArtifact(
            artifactId="art-13",
            parts=[StubTextPart(text=long_text)],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1", max_bytes=100)
        assert len(result.text.encode("utf-8")) <= 100

    def test_no_truncation_within_limit(self) -> None:
        text = "short"
        artifact = StubArtifact(
            artifactId="art-14",
            parts=[StubTextPart(text=text)],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1", max_bytes=100)
        assert result.text == text

    def test_unicode_truncation_safe(self) -> None:
        """Truncation should not produce invalid UTF-8."""
        # Each CJK char is 3 bytes; 10 chars = 30 bytes
        text = "日" * 10
        artifact = StubArtifact(
            artifactId="art-15",
            parts=[StubTextPart(text=text)],
        )
        result = extract_content(artifact, task_id="t1", zone_id="z1", max_bytes=15)
        # Should decode cleanly (errors="ignore" strips partial chars)
        result.text.encode("utf-8")  # should not raise
        assert len(result.text.encode("utf-8")) <= 15
