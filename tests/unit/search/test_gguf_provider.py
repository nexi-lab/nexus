"""Unit tests for GGUFEmbeddingProvider (Issue #1214).

Tests for the GGUF embedding provider using llama-cpp-python for
local llama.cpp inference on mobile/edge devices.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from nexus.search.mobile_config import (
    EMBEDDING_MODELS,
    EmbeddingModelConfig,
    ModelProvider,
)


class TestGGUFEmbeddingProvider:
    """Tests for GGUFEmbeddingProvider."""

    @pytest.fixture
    def mock_llama(self):
        """Create a mock llama-cpp-python module."""
        mock_module = MagicMock()
        mock_llama_class = MagicMock()

        # Mock model instance
        mock_model = MagicMock()
        mock_model.n_embd.return_value = 384
        mock_model.embed.return_value = [0.1] * 384
        mock_llama_class.return_value = mock_model

        mock_module.Llama = mock_llama_class
        return mock_module, mock_model

    @pytest.fixture
    def arctic_xs_config(self):
        """Get arctic-xs config for testing."""
        return EMBEDDING_MODELS["arctic-xs"]

    def test_import_provider(self):
        """Test that GGUFEmbeddingProvider can be imported."""
        from nexus.search.mobile_providers import GGUFEmbeddingProvider

        assert GGUFEmbeddingProvider is not None

    def test_provider_initialization(self, arctic_xs_config):
        """Test provider initialization with config."""
        from nexus.search.mobile_providers import GGUFEmbeddingProvider

        provider = GGUFEmbeddingProvider(arctic_xs_config)

        assert provider.config == arctic_xs_config
        assert provider._model is None
        assert provider.is_loaded is False

    def test_provider_requires_gguf_model_provider(self):
        """Test that provider only accepts GGUF model configs."""
        from nexus.search.mobile_providers import GGUFEmbeddingProvider

        # Create a non-GGUF config
        non_gguf_config = EmbeddingModelConfig(
            name="test-model",
            provider=ModelProvider.FASTEMBED,
            size_mb=50,
            dimensions=384,
        )

        with pytest.raises(ValueError, match="GGUF"):
            GGUFEmbeddingProvider(non_gguf_config)

    async def test_load_model(self, arctic_xs_config, mock_llama):
        """Test loading a GGUF model."""
        mock_module, mock_model = mock_llama

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)
            await provider.load()

            assert provider.is_loaded is True
            assert provider._model is not None
            mock_module.Llama.assert_called_once()

    async def test_load_model_with_custom_threads(self, mock_llama):
        """Test loading with custom thread count."""
        mock_module, mock_model = mock_llama

        config = EmbeddingModelConfig(
            name="test-model",
            provider=ModelProvider.GGUF,
            size_mb=50,
            dimensions=384,
            metadata={"n_threads": 2, "n_ctx": 256},
        )

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(config)
            await provider.load()

            # Verify Llama was called with correct n_threads
            call_kwargs = mock_module.Llama.call_args.kwargs
            assert call_kwargs.get("n_threads") == 2
            assert call_kwargs.get("n_ctx") == 256

    async def test_lazy_loading_on_embed(self, arctic_xs_config, mock_llama):
        """Test that model is loaded automatically on first embed."""
        mock_module, mock_model = mock_llama

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)
            assert provider.is_loaded is False

            # Embed should trigger load
            await provider.embed_text("hello")

            assert provider.is_loaded is True

    async def test_embed_text(self, arctic_xs_config, mock_llama):
        """Test embedding a single text."""
        mock_module, mock_model = mock_llama
        expected_embedding = [0.1] * 384
        mock_model.embed.return_value = expected_embedding

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)
            embedding = await provider.embed_text("hello world")

            assert len(embedding) == 384
            assert embedding == expected_embedding
            mock_model.embed.assert_called_once_with("hello world")

    async def test_embed_texts_batch(self, arctic_xs_config, mock_llama):
        """Test embedding multiple texts."""
        mock_module, mock_model = mock_llama

        # Return different embeddings for each text
        def embed_side_effect(text):
            if "hello" in text:
                return [0.1] * 384
            else:
                return [0.2] * 384

        mock_model.embed.side_effect = embed_side_effect

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)
            texts = ["hello", "world"]
            embeddings = await provider.embed_texts(texts)

            assert len(embeddings) == 2
            assert len(embeddings[0]) == 384
            assert len(embeddings[1]) == 384
            assert embeddings[0] == [0.1] * 384
            assert embeddings[1] == [0.2] * 384

    async def test_unload_model(self, arctic_xs_config, mock_llama):
        """Test unloading model releases resources."""
        mock_module, mock_model = mock_llama

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)
            await provider.load()
            assert provider.is_loaded is True

            await provider.unload()

            assert provider.is_loaded is False
            assert provider._model is None

    def test_embedding_dimension(self, arctic_xs_config):
        """Test getting embedding dimension from config."""
        from nexus.search.mobile_providers import GGUFEmbeddingProvider

        provider = GGUFEmbeddingProvider(arctic_xs_config)
        assert provider.embedding_dimension() == 384

    def test_detect_optimal_threads(self):
        """Test optimal thread detection for mobile."""
        from nexus.search.mobile_providers import GGUFEmbeddingProvider

        # Mock os.cpu_count() to test thread capping
        with patch("os.cpu_count", return_value=8):
            threads = GGUFEmbeddingProvider._detect_threads()
            # Should cap at 4 for mobile battery efficiency
            assert threads == 4

        with patch("os.cpu_count", return_value=2):
            threads = GGUFEmbeddingProvider._detect_threads()
            assert threads == 2

        with patch("os.cpu_count", return_value=None):
            threads = GGUFEmbeddingProvider._detect_threads()
            # Fallback to 4
            assert threads == 4

    async def test_import_error_without_llama_cpp(self, arctic_xs_config):
        """Test clear error when llama-cpp-python not installed."""
        # Remove llama_cpp from modules if present
        with patch.dict(sys.modules, {"llama_cpp": None}):
            # Force reimport to trigger import error path
            import importlib

            from nexus.search import mobile_providers

            importlib.reload(mobile_providers)

            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)

            with pytest.raises(ImportError, match="llama-cpp-python"):
                await provider.load()


class TestGGUFProviderFactory:
    """Tests for GGUF provider factory integration."""

    @pytest.fixture
    def mock_llama(self):
        """Create a mock llama-cpp-python module."""
        mock_module = MagicMock()
        mock_model = MagicMock()
        mock_model.n_embd.return_value = 384
        mock_model.embed.return_value = [0.1] * 384
        mock_module.Llama.return_value = mock_model
        return mock_module, mock_model

    async def test_factory_creates_gguf_provider(self, mock_llama):
        """Test that factory creates GGUFEmbeddingProvider for GGUF configs."""
        mock_module, mock_model = mock_llama

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import (
                GGUFEmbeddingProvider,
                create_mobile_embedding_provider,
            )

            config = EMBEDDING_MODELS["arctic-xs"]
            provider = await create_mobile_embedding_provider(config, load_immediately=False)

            assert isinstance(provider, GGUFEmbeddingProvider)

    async def test_factory_loads_gguf_model(self, mock_llama):
        """Test that factory can load GGUF model."""
        mock_module, mock_model = mock_llama

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import create_mobile_embedding_provider

            config = EMBEDDING_MODELS["arctic-xs"]
            provider = await create_mobile_embedding_provider(config, load_immediately=True)

            assert provider.is_loaded is True


class TestGGUFModelDownload:
    """Tests for GGUF model download helper."""

    async def test_download_gguf_model_from_huggingface(self):
        """Test downloading GGUF model from HuggingFace."""
        from nexus.search.mobile_providers import download_gguf_model

        mock_hf = MagicMock()
        mock_hf.hf_hub_download.return_value = "/path/to/model.gguf"

        with patch.dict(sys.modules, {"huggingface_hub": mock_hf}):
            path = await download_gguf_model(
                repo_id="ChristianAzinn/snowflake-arctic-embed-xs-gguf",
                filename="snowflake-arctic-embed-xs-Q8_0.gguf",
            )

            assert path == "/path/to/model.gguf"
            mock_hf.hf_hub_download.assert_called_once()

    async def test_download_gguf_model_with_cache_dir(self):
        """Test downloading with custom cache directory."""
        from nexus.search.mobile_providers import download_gguf_model

        mock_hf = MagicMock()
        mock_hf.hf_hub_download.return_value = "/custom/cache/model.gguf"

        with patch.dict(sys.modules, {"huggingface_hub": mock_hf}):
            path = await download_gguf_model(
                repo_id="ChristianAzinn/snowflake-arctic-embed-xs-gguf",
                filename="snowflake-arctic-embed-xs-Q8_0.gguf",
                cache_dir="/custom/cache",
            )

            assert path == "/custom/cache/model.gguf"
            call_kwargs = mock_hf.hf_hub_download.call_args
            assert call_kwargs.kwargs.get("cache_dir") == "/custom/cache"


class TestGGUFEmbeddingProviderEdgeCases:
    """Edge case tests for GGUFEmbeddingProvider."""

    @pytest.fixture
    def mock_llama(self):
        """Create a mock llama-cpp-python module."""
        mock_module = MagicMock()
        mock_model = MagicMock()
        mock_model.n_embd.return_value = 384
        mock_model.embed.return_value = [0.1] * 384
        mock_module.Llama.return_value = mock_model
        return mock_module, mock_model

    @pytest.fixture
    def arctic_xs_config(self):
        """Get arctic-xs config for testing."""
        return EMBEDDING_MODELS["arctic-xs"]

    async def test_embed_empty_text(self, arctic_xs_config, mock_llama):
        """Test embedding empty text."""
        mock_module, mock_model = mock_llama
        mock_model.embed.return_value = [0.0] * 384

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)
            embedding = await provider.embed_text("")

            assert len(embedding) == 384

    async def test_embed_empty_list(self, arctic_xs_config, mock_llama):
        """Test embedding empty list of texts."""
        mock_module, mock_model = mock_llama

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)
            embeddings = await provider.embed_texts([])

            assert embeddings == []

    async def test_embed_long_text_truncation(self, mock_llama):
        """Test that long text is handled based on max_tokens."""
        mock_module, mock_model = mock_llama
        mock_model.embed.return_value = [0.1] * 384

        config = EmbeddingModelConfig(
            name="test-model",
            provider=ModelProvider.GGUF,
            size_mb=50,
            dimensions=384,
            max_tokens=512,
        )

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(config)
            long_text = "word " * 1000  # Very long text

            # Should not raise, llama-cpp handles truncation internally
            embedding = await provider.embed_text(long_text)
            assert len(embedding) == 384

    async def test_double_load_is_idempotent(self, arctic_xs_config, mock_llama):
        """Test that loading twice doesn't reload."""
        mock_module, mock_model = mock_llama

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)

            await provider.load()
            first_model = provider._model

            await provider.load()  # Second load
            second_model = provider._model

            # Should be same instance
            assert first_model is second_model
            # Llama should only be called once
            assert mock_module.Llama.call_count == 1

    async def test_unload_then_embed_reloads(self, arctic_xs_config, mock_llama):
        """Test that embedding after unload triggers reload."""
        mock_module, mock_model = mock_llama

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)

            await provider.load()
            await provider.unload()
            assert provider.is_loaded is False

            # Embedding should trigger reload
            await provider.embed_text("test")

            assert provider.is_loaded is True
            # Llama should have been called twice (load, reload)
            assert mock_module.Llama.call_count == 2

    async def test_numpy_array_embedding_conversion(self, arctic_xs_config, mock_llama):
        """Test that numpy array embeddings are converted to lists."""
        mock_module, mock_model = mock_llama

        # Return numpy array instead of list
        mock_model.embed.return_value = np.array([0.1] * 384)

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)
            embedding = await provider.embed_text("test")

            # Should be a list, not numpy array
            assert isinstance(embedding, list)
            assert len(embedding) == 384

    async def test_concurrent_embed_calls(self, arctic_xs_config, mock_llama):
        """Test concurrent embedding calls are handled correctly."""
        import asyncio

        mock_module, mock_model = mock_llama
        call_count = 0

        def counting_embed(text):
            nonlocal call_count
            call_count += 1
            return [0.1] * 384

        mock_model.embed.side_effect = counting_embed

        with patch.dict(sys.modules, {"llama_cpp": mock_module}):
            from nexus.search.mobile_providers import GGUFEmbeddingProvider

            provider = GGUFEmbeddingProvider(arctic_xs_config)

            # Run multiple embeds concurrently
            texts = ["text1", "text2", "text3", "text4"]
            embeddings = await asyncio.gather(*[provider.embed_text(t) for t in texts])

            assert len(embeddings) == 4
            assert call_count == 4
