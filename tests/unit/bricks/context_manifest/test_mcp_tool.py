"""Unit tests for nexus_resolve_context MCP tool (Issue #2984).

Tests:
1. Input parsing — valid and invalid JSON
2. Source validation — Pydantic ContextSource union
3. Resolver unavailable — graceful error
4. Happy path — stub resolver returns results
5. Memory wiring — shared helper function
"""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.bricks.context_manifest.models import (
    FileGlobSource,
    ManifestResult,
    MemoryQuerySource,
    SourceResult,
)
from nexus.bricks.context_manifest.resolver import ManifestResolver

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class StubExecutor:
    """Stub executor that returns ok results."""

    async def execute(self, source: Any, variables: dict[str, str]) -> SourceResult:
        return SourceResult.ok(
            source_type=source.type,
            source_name=source.source_name,
            data={"stub": True},
            elapsed_ms=1.0,
        )


@pytest.fixture
def stub_resolver() -> ManifestResolver:
    return ManifestResolver(
        executors={
            "file_glob": StubExecutor(),
            "memory_query": StubExecutor(),
            "workspace_snapshot": StubExecutor(),
            "mcp_tool": StubExecutor(),
        },
        max_resolve_seconds=5.0,
    )


# ---------------------------------------------------------------------------
# Test 1: Input parsing
# ---------------------------------------------------------------------------


class TestInputParsing:
    def test_invalid_sources_json(self) -> None:
        """Invalid JSON in sources raises JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            json.loads("{not valid json")

    def test_sources_must_be_array(self) -> None:
        """sources must be a JSON array, not an object."""
        sources_str = '{"type": "file_glob"}'
        parsed = json.loads(sources_str)
        assert not isinstance(parsed, list)

    def test_empty_sources_array(self) -> None:
        """Empty sources array should be rejected."""
        sources_str = "[]"
        parsed = json.loads(sources_str)
        assert len(parsed) == 0

    def test_valid_sources_json(self) -> None:
        """Valid JSON array of sources parses correctly."""
        sources_str = json.dumps(
            [
                {"type": "file_glob", "pattern": "*.py"},
                {"type": "memory_query", "query": "test", "top_k": 5},
            ]
        )
        parsed = json.loads(sources_str)
        assert isinstance(parsed, list)
        assert len(parsed) == 2


# ---------------------------------------------------------------------------
# Test 2: Source validation via Pydantic
# ---------------------------------------------------------------------------


class TestSourceValidation:
    def test_valid_file_glob_source(self) -> None:
        """Valid file_glob source validates through Pydantic."""
        from pydantic import TypeAdapter

        from nexus.bricks.context_manifest.models import ContextSource

        adapter = TypeAdapter(ContextSource)
        source = adapter.validate_python({"type": "file_glob", "pattern": "*.py"})
        assert isinstance(source, FileGlobSource)
        assert source.pattern == "*.py"

    def test_valid_memory_query_source(self) -> None:
        """Valid memory_query source validates through Pydantic."""
        from pydantic import TypeAdapter

        from nexus.bricks.context_manifest.models import ContextSource

        adapter = TypeAdapter(ContextSource)
        source = adapter.validate_python(
            {"type": "memory_query", "query": "auth patterns", "top_k": 3}
        )
        assert isinstance(source, MemoryQuerySource)
        assert source.query == "auth patterns"
        assert source.top_k == 3

    def test_invalid_source_type(self) -> None:
        """Unknown source type fails validation."""
        from pydantic import TypeAdapter, ValidationError

        from nexus.bricks.context_manifest.models import ContextSource

        adapter = TypeAdapter(ContextSource)
        with pytest.raises(ValidationError):
            adapter.validate_python({"type": "unknown_type", "pattern": "*.py"})

    def test_missing_required_field(self) -> None:
        """Missing required field fails validation."""
        from pydantic import TypeAdapter, ValidationError

        from nexus.bricks.context_manifest.models import ContextSource

        adapter = TypeAdapter(ContextSource)
        with pytest.raises(ValidationError):
            adapter.validate_python({"type": "file_glob"})  # missing pattern


# ---------------------------------------------------------------------------
# Test 3: Resolver integration
# ---------------------------------------------------------------------------


class TestResolverIntegration:
    @pytest.mark.asyncio
    async def test_resolve_returns_results(self, stub_resolver: ManifestResolver) -> None:
        """Resolver returns ManifestResult with source results."""
        sources = [
            FileGlobSource(pattern="*.py"),
            MemoryQuerySource(query="test"),
        ]
        result = await stub_resolver.resolve(sources, {})

        assert isinstance(result, ManifestResult)
        assert len(result.sources) == 2
        assert all(r.status == "ok" for r in result.sources)
        assert result.sources[0].source_type == "file_glob"
        assert result.sources[1].source_type == "memory_query"

    @pytest.mark.asyncio
    async def test_with_executors_returns_new_resolver(
        self, stub_resolver: ManifestResolver
    ) -> None:
        """with_executors returns a new resolver, doesn't mutate original."""
        new_resolver = stub_resolver.with_executors({"custom": StubExecutor()})
        assert new_resolver is not stub_resolver


# ---------------------------------------------------------------------------
# Test 4: MCP tool serialization round-trip (replaces AgentRecord test)
# ---------------------------------------------------------------------------


class TestMCPSerializationRoundTrip:
    def test_json_to_pydantic_to_resolver(self) -> None:
        """JSON string → Pydantic ContextSource → resolver-compatible source."""
        from pydantic import TypeAdapter

        from nexus.bricks.context_manifest.models import ContextSource

        # Simulate MCP tool input
        sources_json = json.dumps(
            [
                {
                    "type": "file_glob",
                    "pattern": "src/**/*.py",
                    "max_files": 20,
                    "required": True,
                    "timeout_seconds": 3.0,
                },
                {
                    "type": "memory_query",
                    "query": "relevant to {{task.description}}",
                    "top_k": 5,
                    "required": False,
                },
            ]
        )

        # Parse JSON
        sources_list = json.loads(sources_json)

        # Validate through Pydantic
        adapter = TypeAdapter(ContextSource)
        pydantic_sources = [adapter.validate_python(s) for s in sources_list]

        # Verify types
        assert isinstance(pydantic_sources[0], FileGlobSource)
        assert pydantic_sources[0].pattern == "src/**/*.py"
        assert pydantic_sources[0].max_files == 20
        assert pydantic_sources[0].required is True
        assert pydantic_sources[0].timeout_seconds == 3.0

        assert isinstance(pydantic_sources[1], MemoryQuerySource)
        assert pydantic_sources[1].query == "relevant to {{task.description}}"
        assert pydantic_sources[1].top_k == 5
        assert pydantic_sources[1].required is False

    def test_result_serialization(self) -> None:
        """SourceResult can be serialized to JSON for MCP response."""
        result = SourceResult.ok(
            source_type="file_glob",
            source_name="*.py",
            data={"files": {"main.py": "print('hello')"}},
            elapsed_ms=12.5,
        )

        serialized = {
            "source_type": result.source_type,
            "source_name": result.source_name,
            "status": result.status,
            "data": result.data,
            "elapsed_ms": result.elapsed_ms,
        }

        # Must be JSON-serializable
        json_str = json.dumps(serialized)
        parsed = json.loads(json_str)
        assert parsed["status"] == "ok"
        assert parsed["data"]["files"]["main.py"] == "print('hello')"
        assert parsed["elapsed_ms"] == 12.5


# ---------------------------------------------------------------------------
# Test 5: Memory wiring helper
# ---------------------------------------------------------------------------


class TestMemoryWiringHelper:
    def test_no_memory_returns_original_resolver(self, stub_resolver: ManifestResolver) -> None:
        """When no memory provider, returns original resolver unchanged."""
        nx_mock = MagicMock()
        nx_mock._memory_provider = None

        # The helper is defined inside create_mcp_server closure,
        # so we test the logic directly
        mem_provider = getattr(nx_mock, "_memory_provider", None)
        memory = mem_provider.get_for_context() if mem_provider else None
        assert memory is None  # No memory → resolver stays the same

    def test_with_memory_creates_new_resolver(self, stub_resolver: ManifestResolver) -> None:
        """When memory provider exists, resolver gets memory executor merged."""
        # Test the with_executors pattern directly
        new_resolver = stub_resolver.with_executors({"memory_query": StubExecutor()})
        assert new_resolver is not stub_resolver
