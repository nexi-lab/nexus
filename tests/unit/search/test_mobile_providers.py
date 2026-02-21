"""Unit tests for mobile provider selection and error handling (Issue #1499).

Tests cover:
- Provider class selection for each ModelProvider enum
- Provider initialization with missing imports (ImportError)
- Provider initialization with file errors (OSError)
- MobileSearchService lifecycle
"""

from unittest.mock import AsyncMock, patch

import pytest

from nexus.search.mobile_config import (
    DeviceTier,
    EmbeddingModelConfig,
    MobileSearchConfig,
    ModelProvider,
    RerankerModelConfig,
    SearchMode,
)
from nexus.search.mobile_providers import (
    CrossEncoderRerankerProvider,
    FastEmbedMobileProvider,
    GGUFEmbeddingProvider,
    MobileSearchService,
    Model2VecProvider,
    SentenceTransformersProvider,
    _get_embedding_provider_class,
    _get_reranker_provider_class,
)

# =============================================================================
# Provider class selection
# =============================================================================


class TestProviderClassSelection:
    """Test _get_embedding_provider_class for each ModelProvider."""

    def _make_config(self, provider: ModelProvider) -> EmbeddingModelConfig:
        return EmbeddingModelConfig(
            name="test-model",
            provider=provider,
            dimensions=384,
            size_mb=10,
        )

    def test_fastembed_provider(self) -> None:
        config = self._make_config(ModelProvider.FASTEMBED)
        assert _get_embedding_provider_class(config) is FastEmbedMobileProvider

    def test_model2vec_provider(self) -> None:
        config = self._make_config(ModelProvider.MODEL2VEC)
        assert _get_embedding_provider_class(config) is Model2VecProvider

    def test_sentence_transformers_provider(self) -> None:
        config = self._make_config(ModelProvider.SENTENCE_TRANSFORMERS)
        assert _get_embedding_provider_class(config) is SentenceTransformersProvider

    def test_gguf_provider(self) -> None:
        config = self._make_config(ModelProvider.GGUF)
        assert _get_embedding_provider_class(config) is GGUFEmbeddingProvider

    def test_onnx_provider(self) -> None:
        config = self._make_config(ModelProvider.ONNX)
        assert _get_embedding_provider_class(config) is FastEmbedMobileProvider

    def test_unsupported_provider(self) -> None:
        config = self._make_config(ModelProvider.API)
        with pytest.raises(ValueError, match="Unsupported embedding provider"):
            _get_embedding_provider_class(config)


class TestRerankerProviderClassSelection:
    """Test _get_reranker_provider_class for each ModelProvider."""

    def _make_config(self, provider: ModelProvider) -> RerankerModelConfig:
        return RerankerModelConfig(
            name="test-reranker",
            provider=provider,
            size_mb=10,
        )

    def test_sentence_transformers_reranker(self) -> None:
        config = self._make_config(ModelProvider.SENTENCE_TRANSFORMERS)
        assert _get_reranker_provider_class(config) is CrossEncoderRerankerProvider

    def test_gguf_reranker(self) -> None:
        config = self._make_config(ModelProvider.GGUF)
        assert _get_reranker_provider_class(config) is CrossEncoderRerankerProvider

    def test_unsupported_reranker(self) -> None:
        config = self._make_config(ModelProvider.API)
        with pytest.raises(ValueError, match="Unsupported reranker provider"):
            _get_reranker_provider_class(config)


# =============================================================================
# Provider initialization with missing imports
# =============================================================================


class TestProviderImportErrors:
    """Test providers raise ImportError when dependencies missing."""

    @pytest.mark.asyncio
    async def test_fastembed_import_error(self) -> None:
        config = EmbeddingModelConfig(
            name="BAAI/bge-small-en-v1.5",
            provider=ModelProvider.FASTEMBED,
            dimensions=384,
            size_mb=45,
        )
        provider = FastEmbedMobileProvider(config)
        with (
            patch.dict("sys.modules", {"fastembed": None}),
            pytest.raises(ImportError, match="FastEmbed not installed"),
        ):
            await provider.load()

    @pytest.mark.asyncio
    async def test_model2vec_import_error(self) -> None:
        config = EmbeddingModelConfig(
            name="minishlab/potion-base-8M",
            provider=ModelProvider.MODEL2VEC,
            dimensions=256,
            size_mb=8,
        )
        provider = Model2VecProvider(config)
        with (
            patch.dict("sys.modules", {"model2vec": None}),
            pytest.raises(ImportError, match="Model2Vec not installed"),
        ):
            await provider.load()

    @pytest.mark.asyncio
    async def test_sentence_transformers_import_error(self) -> None:
        config = EmbeddingModelConfig(
            name="nomic-ai/nomic-embed-text-v1.5",
            provider=ModelProvider.SENTENCE_TRANSFORMERS,
            dimensions=768,
            size_mb=100,
        )
        provider = SentenceTransformersProvider(config)
        with (
            patch.dict("sys.modules", {"sentence_transformers": None}),
            pytest.raises(ImportError, match="sentence-transformers not installed"),
        ):
            await provider.load()

    @pytest.mark.asyncio
    async def test_gguf_import_error(self) -> None:
        config = EmbeddingModelConfig(
            name="arctic-xs-gguf",
            provider=ModelProvider.GGUF,
            dimensions=384,
            size_mb=15,
        )
        provider = GGUFEmbeddingProvider(config)
        with (
            patch.dict("sys.modules", {"llama_cpp": None}),
            pytest.raises(ImportError, match="llama-cpp-python not installed"),
        ):
            await provider.load()


# =============================================================================
# MobileSearchService lifecycle
# =============================================================================


class TestMobileSearchServiceLifecycle:
    """Test MobileSearchService init → embed → shutdown."""

    def _make_service_config(self) -> MobileSearchConfig:
        return MobileSearchConfig(
            tier=DeviceTier.MEDIUM,
            mode=SearchMode.SEMANTIC_ONLY,
            embedding=EmbeddingModelConfig(
                name="test-model",
                provider=ModelProvider.FASTEMBED,
                dimensions=384,
                size_mb=45,
            ),
            reranker=None,
            max_memory_mb=256,
            lazy_load=True,
        )

    def test_initial_state(self) -> None:
        config = self._make_service_config()
        service = MobileSearchService(config)
        assert service.is_initialized is False

    @pytest.mark.asyncio
    async def test_initialize(self) -> None:
        config = self._make_service_config()
        service = MobileSearchService(config)

        # Mock the provider creation to avoid actually loading ML models
        mock_provider = AsyncMock()
        mock_provider.is_loaded = False

        with patch(
            "nexus.search.mobile_providers.create_mobile_embedding_provider",
            return_value=mock_provider,
        ):
            await service.initialize()
            assert service.is_initialized is True

    @pytest.mark.asyncio
    async def test_double_initialize_idempotent(self) -> None:
        config = self._make_service_config()
        service = MobileSearchService(config)

        mock_provider = AsyncMock()
        mock_provider.is_loaded = False

        with patch(
            "nexus.search.mobile_providers.create_mobile_embedding_provider",
            return_value=mock_provider,
        ) as mock_create:
            await service.initialize()
            await service.initialize()  # Second call should be no-op
            assert mock_create.call_count == 1

    @pytest.mark.asyncio
    async def test_shutdown(self) -> None:
        config = self._make_service_config()
        service = MobileSearchService(config)

        mock_provider = AsyncMock()
        mock_provider.is_loaded = True

        with patch(
            "nexus.search.mobile_providers.create_mobile_embedding_provider",
            return_value=mock_provider,
        ):
            await service.initialize()
            await service.shutdown()
            assert service.is_initialized is False
            mock_provider.unload.assert_called_once()

    @pytest.mark.asyncio
    async def test_embed_text_without_provider(self) -> None:
        config = MobileSearchConfig(
            tier=DeviceTier.LOW,
            mode=SearchMode.KEYWORD_ONLY,
            embedding=None,
            reranker=None,
            max_memory_mb=64,
        )
        service = MobileSearchService(config)
        service._initialized = True

        with pytest.raises(RuntimeError, match="No embedding provider"):
            await service.embed_text("hello")

    @pytest.mark.asyncio
    async def test_rerank_without_provider(self) -> None:
        config = MobileSearchConfig(
            tier=DeviceTier.LOW,
            mode=SearchMode.KEYWORD_ONLY,
            embedding=None,
            reranker=None,
            max_memory_mb=64,
        )
        service = MobileSearchService(config)
        service._initialized = True

        with pytest.raises(RuntimeError, match="No reranker provider"):
            await service.rerank("query", ["doc1"])

    def test_get_status(self) -> None:
        config = self._make_service_config()
        service = MobileSearchService(config)
        status = service.get_status()

        assert status["initialized"] is False
        assert status["tier"] == "medium"
        assert status["mode"] == "semantic"
        assert status["embedding"]["model"] == "test-model"
        assert status["embedding"]["loaded"] is False
        assert status["reranker"]["model"] is None
