"""Tests for ArtifactContent frozen dataclass and ArtifactIndexerProtocol."""

import pytest

from nexus.bricks.artifact_index.protocol import ArtifactContent, ArtifactIndexerProtocol


class TestArtifactContent:
    """ArtifactContent frozen dataclass creation and immutability."""

    def test_create_with_all_fields(self) -> None:
        content = ArtifactContent(
            text="hello world",
            metadata={"key": "val"},
            artifact_id="art-1",
            task_id="task-1",
            zone_id="zone-1",
        )
        assert content.text == "hello world"
        assert content.metadata == {"key": "val"}
        assert content.artifact_id == "art-1"
        assert content.task_id == "task-1"
        assert content.zone_id == "zone-1"

    def test_frozen_immutability(self) -> None:
        content = ArtifactContent(text="x", metadata={}, artifact_id="a", task_id="t", zone_id="z")
        with pytest.raises(AttributeError):
            content.text = "y"

    def test_empty_text(self) -> None:
        content = ArtifactContent(text="", metadata={}, artifact_id="a", task_id="t", zone_id="z")
        assert content.text == ""

    def test_slots(self) -> None:
        content = ArtifactContent(text="x", metadata={}, artifact_id="a", task_id="t", zone_id="z")
        assert not hasattr(content, "__dict__")


class TestArtifactIndexerProtocol:
    """Protocol runtime-checkable conformance."""

    def test_protocol_is_runtime_checkable(self) -> None:
        assert hasattr(ArtifactIndexerProtocol, "__protocol_attrs__") or hasattr(
            ArtifactIndexerProtocol, "__abstractmethods__"
        )

    def test_class_with_index_method_conforms(self) -> None:
        class _Adapter:
            async def index(self, content: ArtifactContent) -> None:
                pass

        assert isinstance(_Adapter(), ArtifactIndexerProtocol)

    def test_class_without_index_fails(self) -> None:
        class _Bad:
            pass

        assert not isinstance(_Bad(), ArtifactIndexerProtocol)
