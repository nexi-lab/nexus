"""Unit tests for LineageAspect model and registration (Issue #3417)."""

import pytest

from nexus.contracts.aspects import (
    AspectRegistry,
    LineageAspect,
)


@pytest.fixture(autouse=True)
def _ensure_lineage_registered():
    """Ensure lineage aspect is registered (may be cleared by other tests' reset())."""
    registry = AspectRegistry.get()
    if not registry.is_registered("lineage"):
        registry.register("lineage", LineageAspect, max_versions=5)


class TestLineageAspectRegistration:
    """Verify LineageAspect is properly registered."""

    def test_registered_with_correct_name(self) -> None:
        registry = AspectRegistry.get()
        assert registry.is_registered("lineage")

    def test_max_versions_is_5(self) -> None:
        registry = AspectRegistry.get()
        assert registry.max_versions_for("lineage") == 5

    def test_validate_payload_accepts_valid(self) -> None:
        registry = AspectRegistry.get()
        payload = {
            "upstream": [
                {"path": "/a.txt", "version": 1, "content_id": "abc", "access_type": "content"}
            ],
            "agent_id": "agent-1",
            "operation": "write",
        }
        # Should not raise
        registry.validate_payload("lineage", payload)

    def test_validate_payload_accepts_empty_upstream(self) -> None:
        registry = AspectRegistry.get()
        payload = {"upstream": [], "agent_id": "agent-1", "operation": "write"}
        registry.validate_payload("lineage", payload)


class TestLineageAspectModel:
    """Test LineageAspect as a Pydantic-style model."""

    def test_default_construction(self) -> None:
        aspect = LineageAspect()
        assert aspect.upstream == []
        assert aspect.agent_id == ""
        assert aspect.agent_generation is None
        assert aspect.operation == "write"
        assert aspect.duration_ms is None
        assert aspect.truncated is False

    def test_construction_with_data(self) -> None:
        upstream = [{"path": "/a.txt", "version": 1, "content_id": "abc", "access_type": "content"}]
        aspect = LineageAspect(
            upstream=upstream,
            agent_id="agent-1",
            agent_generation=3,
            operation="write_batch",
            duration_ms=150,
        )
        assert len(aspect.upstream) == 1
        assert aspect.upstream[0]["path"] == "/a.txt"
        assert aspect.agent_id == "agent-1"
        assert aspect.agent_generation == 3
        assert aspect.operation == "write_batch"
        assert aspect.duration_ms == 150

    def test_to_dict(self) -> None:
        aspect = LineageAspect(
            upstream=[
                {"path": "/x.csv", "version": 2, "content_id": "def", "access_type": "content"}
            ],
            agent_id="agent-2",
            operation="write",
        )
        d = aspect.to_dict()
        assert d["agent_id"] == "agent-2"
        assert d["operation"] == "write"
        assert len(d["upstream"]) == 1
        assert d["upstream"][0]["path"] == "/x.csv"

    def test_from_dict(self) -> None:
        data = {
            "upstream": [
                {"path": "/a.txt", "version": 1, "content_id": "abc", "access_type": "content"}
            ],
            "agent_id": "agent-3",
            "operation": "write",
        }
        aspect = LineageAspect.from_dict(data)
        assert aspect.agent_id == "agent-3"
        assert len(aspect.upstream) == 1

    def test_to_dict_roundtrip(self) -> None:
        original = LineageAspect(
            upstream=[
                {"path": "/a.txt", "version": 5, "content_id": "hash", "access_type": "metadata"}
            ],
            agent_id="roundtrip-agent",
            agent_generation=7,
            operation="copy",
            duration_ms=42,
            truncated=True,
        )
        d = original.to_dict()
        restored = LineageAspect.from_dict(d)
        assert restored.agent_id == original.agent_id
        assert restored.operation == original.operation
        assert restored.duration_ms == original.duration_ms
        assert restored.truncated == original.truncated
        assert len(restored.upstream) == len(original.upstream)


class TestLineageAspectFromSessionReads:
    """Test the from_session_reads factory method."""

    def test_basic_construction(self) -> None:
        reads = [
            {"path": "/data/a.csv", "version": 3, "content_id": "aaa", "access_type": "content"},
            {"path": "/data/b.csv", "version": 7, "content_id": "bbb", "access_type": "content"},
        ]
        aspect = LineageAspect.from_session_reads(
            reads=reads,
            agent_id="agent-1",
            agent_generation=1,
            operation="write",
            duration_ms=100,
        )
        assert len(aspect.upstream) == 2
        assert aspect.upstream[0]["path"] == "/data/a.csv"
        assert aspect.upstream[1]["path"] == "/data/b.csv"
        assert aspect.agent_id == "agent-1"
        assert aspect.agent_generation == 1
        assert aspect.operation == "write"
        assert aspect.duration_ms == 100
        assert aspect.truncated is False

    def test_empty_reads_produces_empty_upstream(self) -> None:
        aspect = LineageAspect.from_session_reads(reads=[], agent_id="agent-1")
        assert aspect.upstream == []
        assert aspect.truncated is False

    def test_truncation_at_max_entries(self) -> None:
        reads = [
            {
                "path": f"/file_{i}.txt",
                "version": i,
                "content_id": f"e{i}",
                "access_type": "content",
            }
            for i in range(600)
        ]
        aspect = LineageAspect.from_session_reads(reads=reads, agent_id="agent-1")
        assert len(aspect.upstream) == LineageAspect.MAX_UPSTREAM_ENTRIES
        assert aspect.truncated is True

    def test_missing_optional_fields_in_reads(self) -> None:
        reads = [{"path": "/a.txt"}]  # Missing version, content_id, access_type
        aspect = LineageAspect.from_session_reads(reads=reads, agent_id="agent-1")
        assert aspect.upstream[0]["version"] == 0
        assert aspect.upstream[0]["content_id"] == ""
        assert aspect.upstream[0]["access_type"] == "content"


class TestLineageAspectFromExplicitDeclaration:
    """Test the from_explicit_declaration factory method."""

    def test_basic_construction(self) -> None:
        upstream = [
            {"path": "/source/config.yaml", "version": 2, "content_id": "cfg"},
        ]
        aspect = LineageAspect.from_explicit_declaration(
            upstream=upstream,
            agent_id="declared-agent",
            agent_generation=5,
        )
        assert len(aspect.upstream) == 1
        assert aspect.agent_id == "declared-agent"
        assert aspect.agent_generation == 5
        assert aspect.operation == "explicit"

    def test_truncation(self) -> None:
        upstream = [{"path": f"/f{i}", "version": i, "content_id": f"e{i}"} for i in range(600)]
        aspect = LineageAspect.from_explicit_declaration(upstream=upstream, agent_id="a")
        assert len(aspect.upstream) == LineageAspect.MAX_UPSTREAM_ENTRIES
        assert aspect.truncated is True
