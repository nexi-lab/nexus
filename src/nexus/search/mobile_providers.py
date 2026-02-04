"""Mobile/Edge Search Providers (Issue #1213).

This module provides embedding and reranker providers optimized for
mobile/edge deployment, connecting MobileSearchConfig to actual model inference.

Supported Providers:
- FastEmbed: ONNX-optimized models (bge-small, etc.)
- Model2Vec: Ultra-fast static embeddings (500x faster)
- SentenceTransformers: Full HuggingFace models
- GGUF: Quantized models via llama-cpp-python (future)

Usage:
    from nexus.search.mobile_config import auto_detect_config
    from nexus.search.mobile_providers import create_provider_from_config

    config = auto_detect_config()
    provider = await create_provider_from_config(config)
    embedding = await provider.embed_text("hello world")
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.search.mobile_config import (
        EmbeddingModelConfig,
        MobileSearchConfig,
        RerankerModelConfig,
    )

logger = logging.getLogger(__name__)

# Default cache directory for downloaded models
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "nexus" / "models"


# =============================================================================
# Abstract Base Classes
# =============================================================================


class MobileEmbeddingProvider(ABC):
    """Abstract base class for mobile embedding providers."""

    def __init__(self, config: EmbeddingModelConfig):
        self.config = config
        self._model: Any = None
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        """Check if model is loaded."""
        return self._loaded

    @abstractmethod
    async def load(self) -> None:
        """Load the model into memory."""
        pass

    @abstractmethod
    async def unload(self) -> None:
        """Unload the model from memory."""
        pass

    @abstractmethod
    async def embed_text(self, text: str) -> list[float]:
        """Embed a single text."""
        pass

    @abstractmethod
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts."""
        pass

    def embedding_dimension(self) -> int:
        """Get embedding dimension."""
        return self.config.dimensions


class MobileRerankerProvider(ABC):
    """Abstract base class for mobile reranker providers."""

    def __init__(self, config: RerankerModelConfig):
        self.config = config
        self._model: Any = None
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        """Check if model is loaded."""
        return self._loaded

    @abstractmethod
    async def load(self) -> None:
        """Load the model into memory."""
        pass

    @abstractmethod
    async def unload(self) -> None:
        """Unload the model from memory."""
        pass

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[tuple[int, float]]:
        """Rerank documents by relevance to query.

        Args:
            query: Search query
            documents: List of document texts
            top_k: Return only top K results (None = all)

        Returns:
            List of (original_index, score) tuples, sorted by score descending
        """
        pass


# =============================================================================
# FastEmbed Provider (ONNX)
# =============================================================================


class FastEmbedMobileProvider(MobileEmbeddingProvider):
    """FastEmbed provider for ONNX-optimized models.

    Supports models like:
    - BAAI/bge-small-en-v1.5 (384d, ~45MB)
    - BAAI/bge-base-en-v1.5 (768d, ~110MB)
    - snowflake/snowflake-arctic-embed-xs (384d, ~22MB) - if supported

    Models are automatically downloaded on first use.
    """

    # Mapping from our model names to FastEmbed model names
    MODEL_MAP = {
        "BAAI/bge-small-en-v1.5": "BAAI/bge-small-en-v1.5",
        "BAAI/bge-base-en-v1.5": "BAAI/bge-base-en-v1.5",
        "sentence-transformers/all-MiniLM-L6-v2": "sentence-transformers/all-MiniLM-L6-v2",
    }

    async def load(self) -> None:
        """Load FastEmbed model."""
        if self._loaded:
            return

        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise ImportError("FastEmbed not installed. Install with: pip install fastembed") from e

        model_name = self.MODEL_MAP.get(self.config.name, self.config.name)
        logger.info(f"Loading FastEmbed model: {model_name}")

        loop = asyncio.get_event_loop()
        self._model = await loop.run_in_executor(None, lambda: TextEmbedding(model_name=model_name))
        self._loaded = True
        logger.info(f"FastEmbed model loaded: {model_name}")

    async def unload(self) -> None:
        """Unload FastEmbed model."""
        if self._model is not None:
            del self._model
            self._model = None
            self._loaded = False
            logger.info("FastEmbed model unloaded")

    async def embed_text(self, text: str) -> list[float]:
        """Embed single text."""
        if not self._loaded:
            await self.load()

        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(None, lambda: list(self._model.embed([text])))
        return embeddings[0].tolist()

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts."""
        if not self._loaded:
            await self.load()

        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(None, lambda: list(self._model.embed(texts)))
        return [e.tolist() for e in embeddings]


# =============================================================================
# Model2Vec Provider (Static Embeddings)
# =============================================================================


class Model2VecProvider(MobileEmbeddingProvider):
    """Model2Vec provider for ultra-fast static embeddings.

    500x faster than transformer models on CPU.
    Only requires numpy as dependency.

    Supports models like:
    - minishlab/potion-base-8M (~8MB, 256d)
    - minishlab/potion-base-32M (~32MB, 256d)
    """

    async def load(self) -> None:
        """Load Model2Vec model."""
        if self._loaded:
            return

        try:
            from model2vec import StaticModel
        except ImportError as e:
            raise ImportError("Model2Vec not installed. Install with: pip install model2vec") from e

        logger.info(f"Loading Model2Vec model: {self.config.name}")

        loop = asyncio.get_event_loop()
        self._model = await loop.run_in_executor(
            None, lambda: StaticModel.from_pretrained(self.config.name)
        )
        self._loaded = True
        logger.info(f"Model2Vec model loaded: {self.config.name}")

    async def unload(self) -> None:
        """Unload Model2Vec model."""
        if self._model is not None:
            del self._model
            self._model = None
            self._loaded = False
            logger.info("Model2Vec model unloaded")

    async def embed_text(self, text: str) -> list[float]:
        """Embed single text."""
        if not self._loaded:
            await self.load()

        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(None, lambda: self._model.encode(text))
        return embedding.tolist()

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts."""
        if not self._loaded:
            await self.load()

        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(None, lambda: self._model.encode(texts))
        return embeddings.tolist()


# =============================================================================
# SentenceTransformers Provider
# =============================================================================


class SentenceTransformersProvider(MobileEmbeddingProvider):
    """SentenceTransformers provider for HuggingFace models.

    Supports any model from HuggingFace that works with sentence-transformers:
    - nomic-ai/nomic-embed-text-v1.5
    - Snowflake/snowflake-arctic-embed-xs
    - google/embeddinggemma-300m (requires trust_remote_code)
    """

    async def load(self) -> None:
        """Load SentenceTransformers model."""
        if self._loaded:
            return

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ImportError(
                "sentence-transformers not installed. "
                "Install with: pip install sentence-transformers"
            ) from e

        logger.info(f"Loading SentenceTransformers model: {self.config.name}")

        # Some models need trust_remote_code
        trust_remote = "gemma" in self.config.name.lower()

        loop = asyncio.get_event_loop()
        self._model = await loop.run_in_executor(
            None,
            lambda: SentenceTransformer(
                self.config.name,
                trust_remote_code=trust_remote,
            ),
        )
        self._loaded = True
        logger.info(f"SentenceTransformers model loaded: {self.config.name}")

    async def unload(self) -> None:
        """Unload model."""
        if self._model is not None:
            del self._model
            self._model = None
            self._loaded = False
            logger.info("SentenceTransformers model unloaded")

    async def embed_text(self, text: str) -> list[float]:
        """Embed single text."""
        if not self._loaded:
            await self.load()

        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(None, lambda: self._model.encode(text))
        return embedding.tolist()

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts."""
        if not self._loaded:
            await self.load()

        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(None, lambda: self._model.encode(texts))
        return embeddings.tolist()


# =============================================================================
# Cross-Encoder Reranker Provider
# =============================================================================


class CrossEncoderRerankerProvider(MobileRerankerProvider):
    """Cross-encoder reranker using sentence-transformers.

    Supports models like:
    - jinaai/jina-reranker-v1-tiny-en (33M params)
    - jinaai/jina-reranker-v1-turbo-en (66M params)
    - BAAI/bge-reranker-base (110M params)
    """

    async def load(self) -> None:
        """Load cross-encoder model."""
        if self._loaded:
            return

        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            raise ImportError(
                "sentence-transformers not installed. "
                "Install with: pip install sentence-transformers"
            ) from e

        logger.info(f"Loading CrossEncoder model: {self.config.name}")

        loop = asyncio.get_event_loop()
        self._model = await loop.run_in_executor(
            None,
            lambda: CrossEncoder(
                self.config.name,
                max_length=self.config.max_length,
            ),
        )
        self._loaded = True
        logger.info(f"CrossEncoder model loaded: {self.config.name}")

    async def unload(self) -> None:
        """Unload model."""
        if self._model is not None:
            del self._model
            self._model = None
            self._loaded = False
            logger.info("CrossEncoder model unloaded")

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[tuple[int, float]]:
        """Rerank documents by relevance to query."""
        if not self._loaded:
            await self.load()

        if not documents:
            return []

        # Create query-document pairs
        pairs = [(query, doc) for doc in documents]

        loop = asyncio.get_event_loop()
        scores = await loop.run_in_executor(None, lambda: self._model.predict(pairs))

        # Create (index, score) tuples and sort by score descending
        indexed_scores = list(enumerate(scores.tolist()))
        indexed_scores.sort(key=lambda x: x[1], reverse=True)

        if top_k is not None:
            indexed_scores = indexed_scores[:top_k]

        return indexed_scores


# =============================================================================
# Provider Factory
# =============================================================================


def _get_embedding_provider_class(
    config: EmbeddingModelConfig,
) -> type[MobileEmbeddingProvider]:
    """Get the appropriate provider class for a config."""
    from nexus.search.mobile_config import ModelProvider

    provider_map: dict[ModelProvider, type[MobileEmbeddingProvider]] = {
        ModelProvider.FASTEMBED: FastEmbedMobileProvider,
        ModelProvider.MODEL2VEC: Model2VecProvider,
        ModelProvider.SENTENCE_TRANSFORMERS: SentenceTransformersProvider,
        ModelProvider.GGUF: SentenceTransformersProvider,  # Fallback for now
        ModelProvider.ONNX: FastEmbedMobileProvider,  # FastEmbed uses ONNX
    }

    provider_class = provider_map.get(config.provider)
    if provider_class is None:
        raise ValueError(f"Unsupported embedding provider: {config.provider}")

    return provider_class


def _get_reranker_provider_class(
    config: RerankerModelConfig,
) -> type[MobileRerankerProvider]:
    """Get the appropriate reranker provider class for a config."""
    from nexus.search.mobile_config import ModelProvider

    provider_map: dict[ModelProvider, type[MobileRerankerProvider]] = {
        ModelProvider.SENTENCE_TRANSFORMERS: CrossEncoderRerankerProvider,
        ModelProvider.GGUF: CrossEncoderRerankerProvider,  # Fallback for now
    }

    provider_class = provider_map.get(config.provider)
    if provider_class is None:
        raise ValueError(f"Unsupported reranker provider: {config.provider}")

    return provider_class


async def create_embedding_provider(
    config: EmbeddingModelConfig,
    load_immediately: bool = True,
) -> MobileEmbeddingProvider:
    """Create an embedding provider from config.

    Args:
        config: Embedding model configuration
        load_immediately: If True, load model before returning

    Returns:
        Configured embedding provider

    Example:
        >>> from nexus.search.mobile_config import EMBEDDING_MODELS
        >>> config = EMBEDDING_MODELS["arctic-xs"]
        >>> provider = await create_embedding_provider(config)
        >>> embedding = await provider.embed_text("hello")
    """
    provider_class = _get_embedding_provider_class(config)
    provider = provider_class(config)

    if load_immediately:
        await provider.load()

    return provider


async def create_reranker_provider(
    config: RerankerModelConfig,
    load_immediately: bool = True,
) -> MobileRerankerProvider:
    """Create a reranker provider from config.

    Args:
        config: Reranker model configuration
        load_immediately: If True, load model before returning

    Returns:
        Configured reranker provider

    Example:
        >>> from nexus.search.mobile_config import RERANKER_MODELS
        >>> config = RERANKER_MODELS["jina-tiny"]
        >>> provider = await create_reranker_provider(config)
        >>> results = await provider.rerank("query", ["doc1", "doc2"])
    """
    provider_class = _get_reranker_provider_class(config)
    provider = provider_class(config)

    if load_immediately:
        await provider.load()

    return provider


# =============================================================================
# Mobile Search Service
# =============================================================================


class MobileSearchService:
    """High-level service for mobile search with automatic config.

    Manages embedding and reranker providers based on MobileSearchConfig.
    Supports lazy loading and automatic unloading after inactivity.

    Example:
        >>> from nexus.search.mobile_config import auto_detect_config
        >>> config = auto_detect_config()
        >>> service = MobileSearchService(config)
        >>> await service.initialize()
        >>> embedding = await service.embed_text("hello world")
        >>> results = await service.rerank("query", ["doc1", "doc2"])
    """

    def __init__(self, config: MobileSearchConfig):
        """Initialize mobile search service.

        Args:
            config: Mobile search configuration
        """
        self.config = config
        self._embedding_provider: MobileEmbeddingProvider | None = None
        self._reranker_provider: MobileRerankerProvider | None = None
        self._initialized = False

    @property
    def is_initialized(self) -> bool:
        """Check if service is initialized."""
        return self._initialized

    async def initialize(self) -> None:
        """Initialize the service (load models if not lazy)."""
        if self._initialized:
            return

        # Create providers (lazy_load determines if models are loaded now)
        if self.config.embedding:
            self._embedding_provider = await create_embedding_provider(
                self.config.embedding,
                load_immediately=not self.config.lazy_load,
            )

        if self.config.reranker:
            self._reranker_provider = await create_reranker_provider(
                self.config.reranker,
                load_immediately=not self.config.lazy_load,
            )

        self._initialized = True
        logger.info(f"MobileSearchService initialized with tier={self.config.tier}")

    async def shutdown(self) -> None:
        """Shutdown the service (unload models)."""
        if self._embedding_provider:
            await self._embedding_provider.unload()
            self._embedding_provider = None

        if self._reranker_provider:
            await self._reranker_provider.unload()
            self._reranker_provider = None

        self._initialized = False
        logger.info("MobileSearchService shutdown")

    async def embed_text(self, text: str) -> list[float]:
        """Embed a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector

        Raises:
            RuntimeError: If no embedding provider configured
        """
        if not self._embedding_provider:
            raise RuntimeError(
                f"No embedding provider for tier={self.config.tier}, mode={self.config.mode}"
            )

        return await self._embedding_provider.embed_text(text)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors
        """
        if not self._embedding_provider:
            raise RuntimeError("No embedding provider configured")

        return await self._embedding_provider.embed_texts(texts)

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[tuple[int, float]]:
        """Rerank documents by relevance to query.

        Args:
            query: Search query
            documents: List of document texts
            top_k: Return only top K results

        Returns:
            List of (original_index, score) tuples

        Raises:
            RuntimeError: If no reranker provider configured
        """
        if not self._reranker_provider:
            raise RuntimeError(
                f"No reranker provider for tier={self.config.tier}, mode={self.config.mode}"
            )

        return await self._reranker_provider.rerank(query, documents, top_k)

    def get_status(self) -> dict[str, Any]:
        """Get service status."""
        return {
            "initialized": self._initialized,
            "tier": self.config.tier.value,
            "mode": self.config.mode.value,
            "embedding": {
                "model": self.config.embedding.name if self.config.embedding else None,
                "loaded": (
                    self._embedding_provider.is_loaded if self._embedding_provider else False
                ),
                "dimensions": (self.config.embedding.dimensions if self.config.embedding else None),
            },
            "reranker": {
                "model": self.config.reranker.name if self.config.reranker else None,
                "loaded": (self._reranker_provider.is_loaded if self._reranker_provider else False),
            },
            "memory_budget_mb": self.config.max_memory_mb,
            "total_model_size_mb": self.config.total_model_size_mb(),
        }


# =============================================================================
# Convenience Functions
# =============================================================================


async def create_service_from_config(
    config: MobileSearchConfig,
    initialize: bool = True,
) -> MobileSearchService:
    """Create and optionally initialize a MobileSearchService.

    Args:
        config: Mobile search configuration
        initialize: If True, initialize service before returning

    Returns:
        Configured MobileSearchService

    Example:
        >>> from nexus.search.mobile_config import auto_detect_config
        >>> config = auto_detect_config()
        >>> service = await create_service_from_config(config)
        >>> embedding = await service.embed_text("hello")
    """
    service = MobileSearchService(config)
    if initialize:
        await service.initialize()
    return service


async def create_auto_service(initialize: bool = True) -> MobileSearchService:
    """Create a MobileSearchService with auto-detected configuration.

    Automatically detects device tier and creates appropriate service.

    Args:
        initialize: If True, initialize service before returning

    Returns:
        Configured MobileSearchService

    Example:
        >>> service = await create_auto_service()
        >>> print(service.get_status())
    """
    from nexus.search.mobile_config import auto_detect_config

    config = auto_detect_config()
    return await create_service_from_config(config, initialize)


# =============================================================================
# Model Download Utilities
# =============================================================================


def check_model_available(model_name: str, provider: str) -> bool:
    """Check if a model is downloaded/available.

    Args:
        model_name: Model name/ID
        provider: Provider type (fastembed, model2vec, sentence_transformers)

    Returns:
        True if model is available locally
    """
    try:
        if provider == "fastembed":
            # Check if model is cached
            cache_dir = Path.home() / ".cache" / "fastembed"
            model_dir = model_name.replace("/", "_")
            return (cache_dir / model_dir).exists()

        elif provider == "model2vec":
            from huggingface_hub import hf_hub_download

            # Check HF cache
            try:
                hf_hub_download(model_name, "model.safetensors", local_files_only=True)
                return True
            except Exception:
                return False

        elif provider == "sentence_transformers":
            from huggingface_hub import hf_hub_download

            try:
                hf_hub_download(model_name, "config.json", local_files_only=True)
                return True
            except Exception:
                return False

    except ImportError:
        return False
    except Exception:
        return False

    return False


async def download_model(model_name: str, provider: str) -> bool:
    """Download a model for offline use.

    Args:
        model_name: Model name/ID
        provider: Provider type

    Returns:
        True if download successful
    """
    loop = asyncio.get_event_loop()

    try:
        if provider == "fastembed":
            from fastembed import TextEmbedding

            logger.info(f"Downloading FastEmbed model: {model_name}")
            await loop.run_in_executor(None, lambda: TextEmbedding(model_name=model_name))
            logger.info(f"FastEmbed model downloaded: {model_name}")
            return True

        elif provider == "model2vec":
            from model2vec import StaticModel

            logger.info(f"Downloading Model2Vec model: {model_name}")
            await loop.run_in_executor(None, lambda: StaticModel.from_pretrained(model_name))
            logger.info(f"Model2Vec model downloaded: {model_name}")
            return True

        elif provider == "sentence_transformers":
            from sentence_transformers import SentenceTransformer

            logger.info(f"Downloading SentenceTransformers model: {model_name}")
            await loop.run_in_executor(None, lambda: SentenceTransformer(model_name))
            logger.info(f"SentenceTransformers model downloaded: {model_name}")
            return True

    except ImportError as e:
        logger.error(f"Provider not installed: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to download model {model_name}: {e}")
        return False

    return False


async def download_models_for_tier(tier: str) -> dict[str, bool]:
    """Download all models needed for a device tier.

    Args:
        tier: Device tier (minimal, low, medium, high)

    Returns:
        Dict of model_name -> success status
    """
    from nexus.search.mobile_config import DeviceTier, get_config_for_tier

    config = get_config_for_tier(DeviceTier(tier))
    results = {}

    if config.embedding:
        provider = config.embedding.provider.value
        success = await download_model(config.embedding.name, provider)
        results[config.embedding.name] = success

    if config.reranker:
        provider = config.reranker.provider.value
        success = await download_model(config.reranker.name, provider)
        results[config.reranker.name] = success

    return results
