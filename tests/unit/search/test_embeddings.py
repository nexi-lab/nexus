"""Tests for embedding providers module."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.search.embeddings import (
    EmbeddingModel,
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
    VoyageAIEmbeddingProvider,
)


class TestEmbeddingModel:
    """Test EmbeddingModel enum."""

    def test_openai_models(self):
        """Test OpenAI model values."""
        assert EmbeddingModel.OPENAI_LARGE == "text-embedding-3-large"
        assert EmbeddingModel.OPENAI_SMALL == "text-embedding-3-small"
        assert EmbeddingModel.OPENAI_ADA == "text-embedding-ada-002"

    def test_voyage_models(self):
        """Test Voyage AI model values."""
        assert EmbeddingModel.VOYAGE_2 == "voyage-2"
        assert EmbeddingModel.VOYAGE_LARGE_2 == "voyage-large-2"


class TestEmbeddingProvider:
    """Test EmbeddingProvider abstract class."""

    def test_abstract_methods(self):
        """Test that EmbeddingProvider is abstract."""
        with pytest.raises(TypeError):
            EmbeddingProvider()  # type: ignore


class TestOpenAIEmbeddingProvider:
    """Test OpenAIEmbeddingProvider provider."""

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    @patch("openai.AsyncOpenAI")
    def test_init_with_env_key(self, mock_openai):
        """Test initialization with API key from environment."""
        provider = OpenAIEmbeddingProvider()
        assert provider.model == EmbeddingModel.OPENAI_LARGE
        mock_openai.assert_called_once_with(api_key="test-key")

    @patch("openai.AsyncOpenAI")
    def test_init_with_explicit_key(self, mock_openai):
        """Test initialization with explicit API key."""
        OpenAIEmbeddingProvider(api_key="explicit-key")
        mock_openai.assert_called_once_with(api_key="explicit-key")

    @patch.dict("os.environ", {}, clear=True)
    def test_init_without_key_raises_error(self):
        """Test initialization without API key raises error."""
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            OpenAIEmbeddingProvider()

    @patch("openai.AsyncOpenAI")
    def test_init_custom_model(self, mock_openai):
        """Test initialization with custom model."""
        provider = OpenAIEmbeddingProvider(api_key="test-key", model=EmbeddingModel.OPENAI_SMALL)
        assert provider.model == EmbeddingModel.OPENAI_SMALL

    @patch("openai.AsyncOpenAI")
    async def test_embed_texts(self, mock_openai):
        """Test embedding multiple texts."""
        # Mock the OpenAI client response
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_embedding = MagicMock()
        mock_embedding.embedding = [0.1, 0.2, 0.3]
        mock_response = MagicMock()
        mock_response.data = [mock_embedding, mock_embedding]
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        provider = OpenAIEmbeddingProvider(api_key="test-key")
        texts = ["hello", "world"]
        embeddings = await provider.embed_texts(texts)

        assert len(embeddings) == 2
        assert embeddings[0] == [0.1, 0.2, 0.3]
        assert embeddings[1] == [0.1, 0.2, 0.3]

        mock_client.embeddings.create.assert_called_once_with(
            input=texts, model=EmbeddingModel.OPENAI_LARGE
        )

    @patch("openai.AsyncOpenAI")
    async def test_embed_text(self, mock_openai):
        """Test embedding a single text."""
        # Mock the OpenAI client response
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_embedding = MagicMock()
        mock_embedding.embedding = [0.1, 0.2, 0.3]
        mock_response = MagicMock()
        mock_response.data = [mock_embedding]
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        provider = OpenAIEmbeddingProvider(api_key="test-key")
        embedding = await provider.embed_text("hello")

        assert embedding == [0.1, 0.2, 0.3]
        mock_client.embeddings.create.assert_called_once_with(
            input=["hello"], model=EmbeddingModel.OPENAI_LARGE
        )

    @patch("openai.AsyncOpenAI")
    def test_get_dimension_openai_large(self, mock_openai):
        """Test getting dimension for OpenAI large model."""
        provider = OpenAIEmbeddingProvider(api_key="test-key", model=EmbeddingModel.OPENAI_LARGE)
        assert provider.embedding_dimension() == 3072

    @patch("openai.AsyncOpenAI")
    def test_get_dimension_openai_small(self, mock_openai):
        """Test getting dimension for OpenAI small model."""
        provider = OpenAIEmbeddingProvider(api_key="test-key", model=EmbeddingModel.OPENAI_SMALL)
        assert provider.embedding_dimension() == 1536

    @patch("openai.AsyncOpenAI")
    def test_get_dimension_openai_ada(self, mock_openai):
        """Test getting dimension for OpenAI ada model."""
        provider = OpenAIEmbeddingProvider(api_key="test-key", model=EmbeddingModel.OPENAI_ADA)
        assert provider.embedding_dimension() == 1536


class TestVoyageAIEmbeddingProvider:
    """Test VoyageAIEmbeddingProvider provider."""

    @patch.dict("os.environ", {"VOYAGE_API_KEY": "test-key"})
    def test_init_with_env_key(self):
        """Test initialization with API key from environment."""
        # Create mock voyageai module
        mock_voyageai = MagicMock()
        mock_client = MagicMock()
        mock_voyageai.Client = MagicMock(return_value=mock_client)

        with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
            provider = VoyageAIEmbeddingProvider()
            assert provider.model == EmbeddingModel.VOYAGE_2
            mock_voyageai.Client.assert_called_once_with(api_key="test-key")

    def test_init_with_explicit_key(self):
        """Test initialization with explicit API key."""
        # Create mock voyageai module
        mock_voyageai = MagicMock()
        mock_client = MagicMock()
        mock_voyageai.Client = MagicMock(return_value=mock_client)

        with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
            VoyageAIEmbeddingProvider(api_key="explicit-key")
            mock_voyageai.Client.assert_called_once_with(api_key="explicit-key")

    @patch.dict("os.environ", {}, clear=True)
    def test_init_without_key_raises_error(self):
        """Test initialization without API key raises error."""
        with pytest.raises(ValueError, match="VOYAGE_API_KEY"):
            VoyageAIEmbeddingProvider()

    def test_init_custom_model(self):
        """Test initialization with custom model."""
        mock_voyageai = MagicMock()
        mock_client = MagicMock()
        mock_voyageai.Client = MagicMock(return_value=mock_client)

        with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
            provider = VoyageAIEmbeddingProvider(
                api_key="test-key", model=EmbeddingModel.VOYAGE_LARGE_2
            )
            assert provider.model == EmbeddingModel.VOYAGE_LARGE_2

    async def test_embed_texts(self):
        """Test embedding multiple texts."""
        mock_voyageai = MagicMock()
        mock_client = MagicMock()
        mock_voyageai.Client = MagicMock(return_value=mock_client)

        mock_result = MagicMock()
        mock_result.embeddings = [[0.1, 0.2], [0.3, 0.4]]
        mock_client.embed.return_value = mock_result

        with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
            provider = VoyageAIEmbeddingProvider(api_key="test-key")
            texts = ["hello", "world"]
            embeddings = await provider.embed_texts(texts)

            assert len(embeddings) == 2
            assert embeddings[0] == [0.1, 0.2]
            assert embeddings[1] == [0.3, 0.4]

            mock_client.embed.assert_called_once_with(texts, model=EmbeddingModel.VOYAGE_2)

    async def test_embed_text(self):
        """Test embedding a single text."""
        mock_voyageai = MagicMock()
        mock_client = MagicMock()
        mock_voyageai.Client = MagicMock(return_value=mock_client)

        mock_result = MagicMock()
        mock_result.embeddings = [[0.1, 0.2, 0.3]]
        mock_client.embed.return_value = mock_result

        with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
            provider = VoyageAIEmbeddingProvider(api_key="test-key")
            embedding = await provider.embed_text("hello")

            assert embedding == [0.1, 0.2, 0.3]
            mock_client.embed.assert_called_once_with(["hello"], model=EmbeddingModel.VOYAGE_2)

    def test_get_dimension_voyage_2(self):
        """Test getting dimension for Voyage 2 model."""
        mock_voyageai = MagicMock()
        mock_client = MagicMock()
        mock_voyageai.Client = MagicMock(return_value=mock_client)

        with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
            provider = VoyageAIEmbeddingProvider(api_key="test-key", model=EmbeddingModel.VOYAGE_2)
            assert provider.embedding_dimension() == 1024

    def test_get_dimension_voyage_large_2(self):
        """Test getting dimension for Voyage Large 2 model."""
        mock_voyageai = MagicMock()
        mock_client = MagicMock()
        mock_voyageai.Client = MagicMock(return_value=mock_client)

        with patch.dict(sys.modules, {"voyageai": mock_voyageai}):
            provider = VoyageAIEmbeddingProvider(
                api_key="test-key", model=EmbeddingModel.VOYAGE_LARGE_2
            )
            assert provider.embedding_dimension() == 1536

    @patch("builtins.__import__", side_effect=ImportError("No module named 'voyageai'"))
    def test_init_without_voyageai_installed(self, mock_import):
        """Test initialization fails gracefully without voyageai package."""
        with pytest.raises(ImportError, match="Voyage AI package not installed"):
            VoyageAIEmbeddingProvider(api_key="test-key")
