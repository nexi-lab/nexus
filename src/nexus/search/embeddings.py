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
    from nexus.cache.base import EmbeddingCacheProtocol

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


class CachedEmbeddingProvider(EmbeddingProvider):
    """Wrapper that adds caching to any embedding provider.

    Implements Issue #950 - reduces embedding API calls by 90% through:
    - Content-hash based caching (same text = same embedding)
    - Batch deduplication (dedupe before API call)

    Usage:
        from nexus.cache import CacheFactory, CacheSettings

        # Initialize cache
        settings = CacheSettings.from_env()
        factory = CacheFactory(settings)
        await factory.initialize()

        # Wrap any embedding provider
        base_provider = create_embedding_provider("openai")
        embedding_cache = factory.get_embedding_cache()

        if embedding_cache:
            provider = CachedEmbeddingProvider(base_provider, embedding_cache)
        else:
            provider = base_provider  # Fallback to uncached
    """

    def __init__(
        self,
        provider: EmbeddingProvider,
        cache: EmbeddingCacheProtocol,
    ):
        """Initialize cached embedding provider.

        Args:
            provider: Base embedding provider to wrap
            cache: EmbeddingCacheProtocol instance (any driver)
        """
        self._provider = provider
        self._cache: EmbeddingCacheProtocol = cache
        self._model_name = self._get_model_name()

    def _get_model_name(self) -> str:
        """Get model name from wrapped provider."""
        if hasattr(self._provider, "model"):
            return str(self._provider.model)
        if hasattr(self._provider, "model_name"):
            return str(self._provider.model_name)
        return self._provider.__class__.__name__

    @property
    def batch_size(self) -> int:
        """Delegate batch size to wrapped provider."""
        return self._provider.batch_size

    def embedding_dimension(self) -> int:
        """Delegate embedding dimension to wrapped provider."""
        return self._provider.embedding_dimension()

    async def embed_text(self, text: str) -> list[float]:
        """Embed single text with caching.

        Args:
            text: Text to embed

        Returns:
            Embedding vector (cached or freshly generated)
        """
        # Check cache first
        cached = await self._cache.get(text, self._model_name)
        if cached is not None:
            return cached

        # Generate embedding
        embedding = await self._provider.embed_text(text)

        # Cache the result (best-effort)
        await self._cache.set(text, self._model_name, embedding)

        return embedding

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed batch of texts with caching and deduplication.

        Args:
            texts: List of texts to embed

        Returns:
            List of embeddings in the same order as input
        """
        if not texts:
            return []

        # Use cache's batch method with deduplication
        return await self._cache.get_or_embed_batch(
            texts=texts,
            model=self._model_name,
            embed_fn=self._provider.embed_texts,
        )

    async def embed_texts_batched(
        self,
        texts: list[str],
        _batch_size: int | None = None,
        _parallel: bool = True,
    ) -> list[list[float]]:
        """Embed texts with batching and caching.

        The cache handles deduplication internally, so we just pass through
        to embed_texts which will use the cache.
        """
        # For cached provider, we let the cache handle batching
        # since it already does deduplication
        return await self.embed_texts(texts)

    def get_cache_metrics(self) -> dict:
        """Get cache metrics.

        Returns:
            Dict with cache statistics
        """
        return self._cache.get_metrics()


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


async def create_cached_embedding_provider(
    provider: str = "openai",
    model: str | None = None,
    api_key: str | None = None,
    cache_url: str | None = None,
    cache_ttl: int = 86400,
) -> EmbeddingProvider:
    """Create an embedding provider with caching enabled.

    This is a convenience function that creates a base embedding provider
    and wraps it with caching if a cache URL is provided.

    Implements Issue #950 - reduces embedding API calls by 90%.

    Args:
        provider: Provider name (openai, voyage, voyage-lite, fastembed, etc.)
        model: Model name (uses default if not provided)
        api_key: API key for the provider
        cache_url: Redis/Dragonfly URL for caching (e.g., redis://localhost:6379)
                   If None, returns uncached provider
        cache_ttl: Cache TTL in seconds (default: 24 hours)

    Returns:
        EmbeddingProvider instance (cached if cache_url provided)

    Examples:
        # Cached OpenAI embeddings
        provider = await create_cached_embedding_provider(
            "openai",
            cache_url="redis://localhost:6379"
        )

        # Uncached fallback
        provider = await create_cached_embedding_provider("openai")
    """
    # Create base provider
    base_provider = create_embedding_provider(provider, model, api_key)

    # Return uncached if no cache URL
    if not cache_url:
        logger.info("Embedding cache not configured, using uncached provider")
        return base_provider

    # Try to create cached provider via CacheStoreABC
    try:
        from nexus.cache.domain import EmbeddingCache
        from nexus.cache.dragonfly import DragonflyCacheStore, DragonflyClient

        # Connect to cache
        client = DragonflyClient(url=cache_url)
        await client.connect()

        # Create driver-agnostic cache via CacheStoreABC
        store = DragonflyCacheStore(client)
        cache = EmbeddingCache(store=store, ttl=cache_ttl)
        cached_provider = CachedEmbeddingProvider(base_provider, cache)

        logger.info(f"Embedding cache enabled with TTL={cache_ttl}s")
        return cached_provider

    except ImportError:
        logger.warning("redis package not installed, using uncached provider")
        return base_provider
    except Exception as e:
        logger.warning(f"Failed to connect to embedding cache ({e}), using uncached provider")
        return base_provider
