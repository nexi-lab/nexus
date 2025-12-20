"""Embedding providers for semantic search.

Supports multiple embedding providers:
- OpenAI (text-embedding-3-large, text-embedding-3-small) - recommended
- Voyage AI (voyage-3, voyage-3-lite) - fast, cost-effective, high quality
- OpenRouter (via OpenAI-compatible API)

Batch optimization:
- All providers support automatic batching for large document sets
- Default batch size optimized per provider for best throughput
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Default batch sizes optimized per provider
DEFAULT_BATCH_SIZES = {
    "openai": 100,  # OpenAI handles large batches well
    "voyage": 128,  # Voyage AI optimal batch size
    "openrouter": 50,  # Conservative for OpenRouter
    "cohere": 96,  # Cohere recommended batch size
    "fastembed": 256,  # Local inference can handle larger batches
}


class EmbeddingModel(StrEnum):
    """Supported embedding models."""

    # OpenAI (recommended for quality)
    OPENAI_LARGE = "text-embedding-3-large"
    OPENAI_SMALL = "text-embedding-3-small"
    OPENAI_ADA = "text-embedding-ada-002"

    # Voyage AI (recommended for speed/cost)
    VOYAGE_3 = "voyage-3"  # Best quality, 1024d, $0.06/1M tokens
    VOYAGE_3_LITE = "voyage-3-lite"  # Fast & cheap, 512d, $0.02/1M tokens
    VOYAGE_3_LARGE = "voyage-3-large"  # Highest quality, 1024d
    VOYAGE_2 = "voyage-2"  # Legacy
    VOYAGE_LARGE_2 = "voyage-large-2"  # Legacy

    # OpenRouter (via OpenAI-compatible API)
    OPENROUTER_DEFAULT = "openai/text-embedding-3-small"


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers.

    All providers support automatic batching for optimal throughput.
    """

    # Default batch size (can be overridden by subclasses)
    batch_size: int = 100

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

    async def embed_texts_batched(
        self,
        texts: list[str],
        batch_size: int | None = None,
        parallel: bool = True,
    ) -> list[list[float]]:
        """Embed texts with automatic batching for optimal throughput.

        Args:
            texts: List of texts to embed
            batch_size: Batch size (uses provider default if not specified)
            parallel: If True, process batches in parallel (default: True)

        Returns:
            List of embeddings in the same order as input texts
        """
        if not texts:
            return []

        batch_size = batch_size or self.batch_size

        # If texts fit in one batch, use direct embed
        if len(texts) <= batch_size:
            return await self.embed_texts(texts)

        # Split into batches
        batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]

        logger.info(
            f"Embedding {len(texts)} texts in {len(batches)} batches "
            f"(batch_size={batch_size}, parallel={parallel})"
        )

        if parallel:
            # Process batches in parallel
            tasks = [self.embed_texts(batch) for batch in batches]
            results = await asyncio.gather(*tasks)
        else:
            # Process batches sequentially
            results = []
            for i, batch in enumerate(batches):
                logger.debug(f"Processing batch {i + 1}/{len(batches)}")
                result = await self.embed_texts(batch)
                results.append(result)

        # Flatten results
        embeddings = []
        for batch_result in results:
            embeddings.extend(batch_result)

        return embeddings


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI embedding provider."""

    batch_size: int = DEFAULT_BATCH_SIZES["openai"]

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
    """Voyage AI embedding provider.

    Voyage AI offers excellent quality/cost ratio:
    - voyage-3: Best quality, 1024d, $0.06/1M tokens (recommended)
    - voyage-3-lite: Fast & cheap, 512d, $0.02/1M tokens (6.5x cheaper than OpenAI)
    - voyage-3-large: Highest quality, 1024d

    Features:
    - 32K context length (vs OpenAI 8K)
    - Lower latency than OpenAI
    - Smaller dimensions = faster vector search
    """

    batch_size: int = DEFAULT_BATCH_SIZES["voyage"]

    def __init__(self, model: str = EmbeddingModel.VOYAGE_3, api_key: str | None = None):
        """Initialize Voyage AI embedding provider.

        Args:
            model: Model name (default: voyage-3, recommended)
                   Options: voyage-3, voyage-3-lite (fast/cheap), voyage-3-large
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
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.client.embed(
                texts,
                model=self.model,
                input_type="document",  # Optimize for document embedding
            ),
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

    async def embed_query(self, text: str) -> list[float]:
        """Embed a query text (optimized for search queries).

        Args:
            text: Query text to embed

        Returns:
            Embedding vector
        """
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.client.embed(
                [text],
                model=self.model,
                input_type="query",  # Optimize for query embedding
            ),
        )
        return cast(list[float], result.embeddings[0])

    def embedding_dimension(self) -> int:
        """Get the embedding dimension.

        Returns:
            Embedding dimension
        """
        # Voyage 3 models
        if self.model == EmbeddingModel.VOYAGE_3_LITE:
            return 512
        elif (
            self.model in (EmbeddingModel.VOYAGE_3, EmbeddingModel.VOYAGE_3_LARGE)
            or self.model == EmbeddingModel.VOYAGE_2
        ):
            return 1024
        elif self.model == EmbeddingModel.VOYAGE_LARGE_2:
            return 1536
        else:
            # Default for unknown models
            return 1024


class OpenRouterEmbeddingProvider(EmbeddingProvider):
    """OpenRouter embedding provider (OpenAI-compatible API)."""

    batch_size: int = DEFAULT_BATCH_SIZES["openrouter"]

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


class FastEmbedProvider(EmbeddingProvider):
    """Local embedding provider using FastEmbed (ONNX-optimized).

    FastEmbed provides fast, local embeddings without API calls:
    - No API latency or costs
    - ONNX-optimized for fast inference
    - Works offline
    - Good quality with bge-small-en-v1.5

    Install with: pip install fastembed
    """

    batch_size: int = DEFAULT_BATCH_SIZES["fastembed"]

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        """Initialize FastEmbed provider.

        Args:
            model: Model name (default: BAAI/bge-small-en-v1.5)
                   Options: BAAI/bge-small-en-v1.5, BAAI/bge-base-en-v1.5
        """
        self.model_name = model

        try:
            from fastembed import TextEmbedding

            self.model = TextEmbedding(model_name=model)
        except ImportError as e:
            raise ImportError(
                "FastEmbed package not installed. Install with: pip install fastembed"
            ) from e

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of embeddings
        """
        # FastEmbed is sync and CPU-bound, run in executor
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(None, lambda: list(self.model.embed(texts)))
        return [e.tolist() for e in embeddings]

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
        if "small" in self.model_name:
            return 384
        elif "base" in self.model_name:
            return 768
        else:
            return 384


def create_embedding_provider(
    provider: str = "openai", model: str | None = None, api_key: str | None = None
) -> EmbeddingProvider:
    """Create an embedding provider.

    Args:
        provider: Provider name
                 Options:
                 - "openai": OpenAI embeddings (recommended for quality)
                 - "voyage": Voyage AI embeddings (recommended for speed/cost)
                 - "voyage-lite": Voyage AI lite (fastest API, 6.5x cheaper)
                 - "fastembed": Local ONNX embeddings (no API, free)
                 - "openrouter": OpenRouter (various models)
        model: Model name (uses default if not provided)
        api_key: API key for the provider

    Returns:
        Embedding provider instance

    Raises:
        ValueError: If provider is unknown

    Examples:
        # OpenAI (best quality)
        provider = create_embedding_provider("openai")

        # Voyage AI (recommended for speed/cost)
        provider = create_embedding_provider("voyage")

        # Voyage AI lite (fastest, cheapest)
        provider = create_embedding_provider("voyage-lite")

        # Local embeddings (no API, free)
        provider = create_embedding_provider("fastembed")
    """
    if provider == "openai":
        model = model or EmbeddingModel.OPENAI_LARGE
        return OpenAIEmbeddingProvider(model=model, api_key=api_key)
    elif provider == "voyage":
        model = model or EmbeddingModel.VOYAGE_3
        return VoyageAIEmbeddingProvider(model=model, api_key=api_key)
    elif provider == "voyage-lite":
        model = model or EmbeddingModel.VOYAGE_3_LITE
        return VoyageAIEmbeddingProvider(model=model, api_key=api_key)
    elif provider == "voyage-large":
        model = model or EmbeddingModel.VOYAGE_3_LARGE
        return VoyageAIEmbeddingProvider(model=model, api_key=api_key)
    elif provider == "fastembed":
        model = model or "BAAI/bge-small-en-v1.5"
        return FastEmbedProvider(model=model)
    elif provider == "openrouter":
        model = model or EmbeddingModel.OPENROUTER_DEFAULT
        return OpenRouterEmbeddingProvider(model=model, api_key=api_key)
    else:
        raise ValueError(
            f"Unknown embedding provider: {provider}. "
            "Supported: openai, voyage, voyage-lite, voyage-large, fastembed, openrouter"
        )
