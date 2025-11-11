"""Embedding providers for semantic search.

Supports multiple embedding providers:
- OpenAI (text-embedding-3-large, text-embedding-3-small) - recommended
- Voyage AI (voyage-2, voyage-large-2) - specialized embeddings
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    pass


class EmbeddingModel(StrEnum):
    """Supported embedding models."""

    # OpenAI (recommended)
    OPENAI_LARGE = "text-embedding-3-large"
    OPENAI_SMALL = "text-embedding-3-small"
    OPENAI_ADA = "text-embedding-ada-002"

    # Voyage AI
    VOYAGE_2 = "voyage-2"
    VOYAGE_LARGE_2 = "voyage-large-2"

    # OpenRouter (via OpenAI-compatible API)
    OPENROUTER_DEFAULT = "openai/text-embedding-3-small"


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""

    @abstractmethod
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of embeddings (each embedding is a list of floats)
        """
        pass

    @abstractmethod
    async def embed_text(self, text: str) -> list[float]:
        """Embed a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        pass

    @abstractmethod
    def embedding_dimension(self) -> int:
        """Get the embedding dimension.

        Returns:
            Embedding dimension
        """
        pass


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI embedding provider."""

    def __init__(self, model: str = EmbeddingModel.OPENAI_LARGE, api_key: str | None = None):
        """Initialize OpenAI embedding provider.

        Args:
            model: Model name
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
        """
        self.model = model
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")

        if not self.api_key:
            raise ValueError("OpenAI API key not provided and OPENAI_API_KEY env var not set")

        # Import OpenAI client
        try:
            from openai import AsyncOpenAI

            self.client = AsyncOpenAI(api_key=self.api_key)
        except ImportError as e:
            raise ImportError(
                "OpenAI package not installed. Install with: pip install openai"
            ) from e

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of embeddings
        """
        response = await self.client.embeddings.create(input=texts, model=self.model)
        return [item.embedding for item in response.data]

    async def embed_text(self, text: str) -> list[float]:
        """Embed a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        embeddings = await self.embed_texts([text])
        return embeddings[0]

    def embedding_dimension(self) -> int:
        """Get the embedding dimension.

        Returns:
            Embedding dimension
        """
        if self.model == EmbeddingModel.OPENAI_LARGE:
            return 3072
        elif self.model in (EmbeddingModel.OPENAI_SMALL, EmbeddingModel.OPENAI_ADA):
            return 1536
        else:
            # Default for unknown models
            return 1536


class VoyageAIEmbeddingProvider(EmbeddingProvider):
    """Voyage AI embedding provider."""

    def __init__(self, model: str = EmbeddingModel.VOYAGE_2, api_key: str | None = None):
        """Initialize Voyage AI embedding provider.

        Args:
            model: Model name
            api_key: Voyage AI API key (defaults to VOYAGE_API_KEY env var)
        """
        self.model = model
        self.api_key = api_key or os.getenv("VOYAGE_API_KEY")

        if not self.api_key:
            raise ValueError("Voyage AI API key not provided and VOYAGE_API_KEY env var not set")

        # Import Voyage AI client
        try:
            import voyageai

            self.client = voyageai.Client(api_key=self.api_key)
        except ImportError as e:
            raise ImportError(
                "Voyage AI package not installed. Install with: pip install voyageai"
            ) from e

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of embeddings
        """
        # Voyage AI client is sync, so we run it in executor to not block
        import asyncio

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: self.client.embed(texts, model=self.model)
        )
        return cast(list[list[float]], result.embeddings)

    async def embed_text(self, text: str) -> list[float]:
        """Embed a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        embeddings = await self.embed_texts([text])
        return embeddings[0]

    def embedding_dimension(self) -> int:
        """Get the embedding dimension.

        Returns:
            Embedding dimension
        """
        if self.model == EmbeddingModel.VOYAGE_2:
            return 1024
        elif self.model == EmbeddingModel.VOYAGE_LARGE_2:
            return 1536
        else:
            # Default for unknown models
            return 1024


class OpenRouterEmbeddingProvider(EmbeddingProvider):
    """OpenRouter embedding provider (OpenAI-compatible API)."""

    def __init__(self, model: str = EmbeddingModel.OPENROUTER_DEFAULT, api_key: str | None = None):
        """Initialize OpenRouter embedding provider.

        Args:
            model: Model name (OpenRouter format: provider/model)
            api_key: OpenRouter API key (defaults to OPENROUTER_API_KEY env var)
        """
        self.model = model
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")

        if not self.api_key:
            raise ValueError(
                "OpenRouter API key not provided and OPENROUTER_API_KEY env var not set"
            )

        # Import OpenAI client
        try:
            from openai import AsyncOpenAI

            # OpenRouter uses OpenAI-compatible API
            self.client = AsyncOpenAI(api_key=self.api_key, base_url="https://openrouter.ai/api/v1")
        except ImportError as e:
            raise ImportError(
                "OpenAI package not installed. Install with: pip install openai"
            ) from e

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of embeddings
        """
        response = await self.client.embeddings.create(input=texts, model=self.model)
        return [item.embedding for item in response.data]

    async def embed_text(self, text: str) -> list[float]:
        """Embed a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        embeddings = await self.embed_texts([text])
        return embeddings[0]

    def embedding_dimension(self) -> int:
        """Get the embedding dimension.

        Returns:
            Embedding dimension
        """
        # OpenRouter typically uses OpenAI models
        if "text-embedding-3-large" in self.model:
            return 3072
        elif "text-embedding-3-small" in self.model or "text-embedding-ada" in self.model:
            return 1536
        else:
            # Default for unknown models
            return 1536


def create_embedding_provider(
    provider: str = "openai", model: str | None = None, api_key: str | None = None
) -> EmbeddingProvider:
    """Create an embedding provider.

    Args:
        provider: Provider name (default: "openai" - recommended)
                 Options: "openai" (recommended), "voyage", "openrouter"
        model: Model name (uses default if not provided)
        api_key: API key for the provider

    Returns:
        Embedding provider instance

    Raises:
        ValueError: If provider is unknown
    """
    if provider == "openai":
        model = model or EmbeddingModel.OPENAI_LARGE
        return OpenAIEmbeddingProvider(model=model, api_key=api_key)
    elif provider == "voyage":
        model = model or EmbeddingModel.VOYAGE_2
        return VoyageAIEmbeddingProvider(model=model, api_key=api_key)
    elif provider == "openrouter":
        model = model or EmbeddingModel.OPENROUTER_DEFAULT
        return OpenRouterEmbeddingProvider(model=model, api_key=api_key)
    else:
        raise ValueError(
            f"Unknown embedding provider: {provider}. Supported providers: openai, voyage, openrouter"
        )
