"""Integration tests for LLM brick extraction (Issue #1521).

Verifies:
- Import paths resolve correctly (canonical locations)
- Protocol compliance (LiteLLMProvider satisfies LLMProviderProtocol)
- LLMService protocol compliance
- Brick manifest and verify_imports()
- Provider caching in LLMService
- Cross-module wiring (search → context_builder, ace → protocol)
"""

import importlib

import pytest

# ---------------------------------------------------------------------------
# 1. Import path validation
# ---------------------------------------------------------------------------


class TestImportPaths:
    """Verify all import paths resolve correctly after the move."""

    def test_new_service_imports(self) -> None:
        """New canonical import paths from services/ work."""
        from nexus.services.llm.llm_citation import (
            Citation,
            CitationExtractor,
            DocumentReadResult,
        )
        from nexus.services.llm.llm_context_builder import (
            AdaptiveRetrievalConfig,
            ChunkLike,
            ContextBuilder,
        )
        from nexus.services.llm.llm_document_reader import (
            LLMDocumentReader,
            ReadChunk,
        )

        assert Citation is not None
        assert CitationExtractor is not None
        assert DocumentReadResult is not None
        assert AdaptiveRetrievalConfig is not None
        assert ChunkLike is not None
        assert ContextBuilder is not None
        assert LLMDocumentReader is not None
        assert ReadChunk is not None

    def test_brick_level_exports(self) -> None:
        """LLM brick __init__ exports core types (not orchestration)."""
        from nexus.bricks.llm import (
            LiteLLMProvider,
            LLMBrickManifest,
            LLMConfig,
            LLMException,
            LLMMetrics,
            LLMProvider,
            LLMResponse,
            Message,
            MessageRole,
            verify_imports,
        )

        assert LLMConfig is not None
        assert LLMProvider is not None
        assert LiteLLMProvider is not None
        assert LLMResponse is not None
        assert Message is not None
        assert MessageRole is not None
        assert LLMMetrics is not None
        assert LLMException is not None
        assert LLMBrickManifest is not None
        assert verify_imports is not None


# ---------------------------------------------------------------------------
# 2. Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    """Verify protocol contracts are satisfied."""

    def test_litellm_provider_satisfies_protocol(self) -> None:
        """LiteLLMProvider satisfies LLMProviderProtocol at runtime."""
        from nexus.bricks.llm.provider import LiteLLMProvider
        from nexus.services.protocols.llm_provider import LLMProviderProtocol

        assert issubclass(LiteLLMProvider, LLMProviderProtocol)

    def test_read_chunk_satisfies_chunk_like(self) -> None:
        """ReadChunk dataclass satisfies ChunkLike protocol."""
        from nexus.services.llm.llm_context_builder import ChunkLike
        from nexus.services.llm.llm_document_reader import ReadChunk

        chunk = ReadChunk(path="/test.txt", chunk_text="hello", chunk_index=0)
        assert isinstance(chunk, ChunkLike)

    def test_protocol_exports_in_protocols_package(self) -> None:
        """Both protocols are exported from the protocols package."""
        from nexus.services.protocols import (
            LLMProviderProtocol,
            LLMServiceProtocol,
        )

        assert LLMProviderProtocol is not None
        assert LLMServiceProtocol is not None


# ---------------------------------------------------------------------------
# 3. Brick manifest
# ---------------------------------------------------------------------------


class TestBrickManifest:
    """Verify LLM brick manifest and import validation."""

    def test_manifest_metadata(self) -> None:
        """LLMBrickManifest has correct metadata."""
        from nexus.bricks.llm.manifest import LLMBrickManifest

        manifest = LLMBrickManifest()
        assert manifest.name == "llm"
        assert manifest.protocol == "LLMProviderProtocol"
        assert manifest.version == "1.0.0"
        assert "model" in manifest.config_schema
        assert "temperature" in manifest.config_schema
        assert "max_output_tokens" in manifest.config_schema
        assert "timeout" in manifest.config_schema
        assert "caching_prompt" in manifest.config_schema

    def test_manifest_is_frozen(self) -> None:
        """LLMBrickManifest is immutable."""
        from nexus.bricks.llm.manifest import LLMBrickManifest

        manifest = LLMBrickManifest()
        with pytest.raises(AttributeError):
            manifest.name = "something_else"  # type: ignore[misc]

    def test_verify_imports_all_pass(self) -> None:
        """verify_imports() succeeds for all required modules."""
        from nexus.bricks.llm.manifest import verify_imports

        status = verify_imports()
        for mod, ok in status.items():
            assert ok, f"Module {mod} failed to import"

    def test_verify_imports_returns_expected_modules(self) -> None:
        """verify_imports() checks the correct set of modules."""
        from nexus.bricks.llm.manifest import verify_imports

        status = verify_imports()
        # Required external dependencies
        assert "litellm" in status
        assert "pydantic" in status
        assert "tenacity" in status
        # Internal modules
        assert "nexus.bricks.llm.config" in status
        assert "nexus.bricks.llm.provider" in status
        assert "nexus.bricks.llm.message" in status
        assert "nexus.bricks.llm.metrics" in status
        assert "nexus.bricks.llm.exceptions" in status
        assert "nexus.bricks.llm.cancellation" in status


# ---------------------------------------------------------------------------
# 4. Cross-module wiring
# ---------------------------------------------------------------------------


class TestCrossModuleWiring:
    """Verify cross-module imports resolve without circular dependencies."""

    def test_search_imports_context_builder_from_services(self) -> None:
        """Search modules can import ContextBuilder from services."""
        # Reimport to ensure clean resolution
        mod = importlib.import_module("nexus.bricks.search.semantic")
        assert mod is not None

    def test_ace_imports_protocol(self) -> None:
        """ACE services can import LLMProviderProtocol."""
        mod = importlib.import_module("nexus.services.ace.reflection")
        assert mod is not None

    def test_document_reader_uses_chunk_like(self) -> None:
        """LLMDocumentReader resolves imports from services."""
        mod = importlib.import_module("nexus.services.llm.llm_document_reader")
        assert hasattr(mod, "LLMDocumentReader")
        assert hasattr(mod, "ReadChunk")

    def test_no_circular_import_services_to_llm(self) -> None:
        """Services → LLM brick imports work without circular dependency."""
        # This was a real bug: services/llm_document_reader → nexus.llm.message
        # → nexus.llm.__init__ → nexus.llm.document_reader (stub) → services/
        # Fixed by removing eager re-exports from __init__.py
        import sys

        # Remove cached modules to force fresh resolution
        to_remove = [k for k in sys.modules if "nexus.services.llm.llm_document_reader" in k]
        saved = {k: sys.modules.pop(k) for k in to_remove}

        try:
            importlib.import_module("nexus.services.llm.llm_document_reader")
        finally:
            sys.modules.update(saved)


# ---------------------------------------------------------------------------
# 5. Provider caching in LLMService
# ---------------------------------------------------------------------------


class TestProviderCaching:
    """Verify LLMService caches providers by config hash."""

    def test_llm_service_has_provider_cache(self) -> None:
        """LLMService initializes with empty provider cache."""
        from nexus.services.llm.llm_service import LLMService

        service = LLMService(nexus_fs=None)
        assert hasattr(service, "_provider_cache")
        assert isinstance(service._provider_cache, dict)
        assert len(service._provider_cache) == 0


# ---------------------------------------------------------------------------
# 6. Context builder with ChunkLike
# ---------------------------------------------------------------------------


class TestContextBuilderIntegration:
    """Verify ContextBuilder works with different ChunkLike implementations."""

    def test_build_context_with_read_chunks(self) -> None:
        """ContextBuilder works with ReadChunk objects."""
        from nexus.services.llm.llm_context_builder import ContextBuilder
        from nexus.services.llm.llm_document_reader import ReadChunk

        chunks = [
            ReadChunk(path="/doc1.txt", chunk_text="Hello world", chunk_index=0),
            ReadChunk(path="/doc2.txt", chunk_text="Foo bar", chunk_index=1, score=0.9),
        ]

        builder = ContextBuilder(max_context_tokens=1000)
        context = builder.build_context(chunks)

        assert "/doc1.txt" in context
        assert "/doc2.txt" in context
        assert "Hello world" in context
        assert "Foo bar" in context

    def test_citation_extractor_with_chunk_like_objects(self) -> None:
        """CitationExtractor works with ChunkLike objects (not just dicts)."""
        from nexus.services.llm.llm_citation import CitationExtractor
        from nexus.services.llm.llm_document_reader import ReadChunk

        chunks = [
            ReadChunk(
                path="/docs/api.md",
                chunk_text="API reference content",
                chunk_index=0,
                score=0.95,
            ),
        ]

        citations = CitationExtractor.extract_citations(
            answer="Based on [Source: /docs/api.md], the API provides...",
            chunks=chunks,
            include_all_sources=False,
        )

        assert len(citations) == 1
        assert citations[0].path == "/docs/api.md"
        assert citations[0].score == 0.95

    def test_citation_extractor_with_dict_chunks(self) -> None:
        """CitationExtractor still works with legacy dict chunks."""
        from nexus.services.llm.llm_citation import CitationExtractor

        chunks = [
            {
                "path": "/docs/api.md",
                "chunk_text": "API reference content",
                "chunk_index": 0,
                "score": 0.95,
            },
        ]

        citations = CitationExtractor.extract_citations(
            answer="Based on [Source: /docs/api.md], the API provides...",
            chunks=chunks,
            include_all_sources=False,
        )

        assert len(citations) == 1
        assert citations[0].path == "/docs/api.md"


# ---------------------------------------------------------------------------
# 7. Performance validation
# ---------------------------------------------------------------------------


class TestPerformanceValidation:
    """Basic performance checks for import times."""

    def test_llm_brick_import_time(self) -> None:
        """LLM brick imports in reasonable time (<2s)."""
        import time

        start = time.perf_counter()
        importlib.import_module("nexus.bricks.llm")
        elapsed = time.perf_counter() - start

        # Should be fast since litellm is already cached
        assert elapsed < 2.0, f"LLM brick import took {elapsed:.2f}s"

    def test_manifest_verify_time(self) -> None:
        """verify_imports() completes quickly (<1s)."""
        import time

        from nexus.bricks.llm.manifest import verify_imports

        start = time.perf_counter()
        verify_imports()
        elapsed = time.perf_counter() - start

        assert elapsed < 1.0, f"verify_imports took {elapsed:.2f}s"
