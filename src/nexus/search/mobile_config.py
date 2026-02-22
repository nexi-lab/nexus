"""Mobile/Edge Search Configuration for Tiered Device Support (Issue #1213).

This module provides a configuration system for mobile/edge search that
automatically selects appropriate embedding and reranker models based on
device capabilities (RAM, compute).

Device Tiers:
- MINIMAL (<2GB RAM): Keyword-only search, no ML models
- LOW (4GB RAM): Arctic-XS embeddings (~15MB), no reranker
- MEDIUM (6-8GB RAM): Nomic v1.5 (~50MB) + Jina Tiny reranker (~40MB)
- HIGH (12GB+ RAM): EmbeddingGemma (~150MB) + Jina Tiny (~40MB)
- SERVER: API providers (OpenAI, Voyage, Cohere)

Search Modes:
- KEYWORD_ONLY: BM25 sparse retrieval only
- SEMANTIC_ONLY: Dense vector search only
- HYBRID: BM25 + Dense with RRF fusion
- HYBRID_RERANKED: Hybrid + cross-encoder reranking

References:
- EmbeddingGemma: https://developers.googleblog.com/en/introducing-embeddinggemma/
- Model2Vec: https://github.com/MinishLab/model2vec
- Jina Reranker Tiny: https://jina.ai/news/smaller-faster-cheaper-jina-rerankers-turbo-and-tiny/
- Snowflake Arctic-Embed: https://huggingface.co/Snowflake/snowflake-arctic-embed-xs
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# Enums
# =============================================================================


class DeviceTier(StrEnum):
    """Device capability tiers based on available RAM.

    Each tier maps to recommended model configurations that balance
    quality, latency, and memory usage.
    """

    MINIMAL = "minimal"  # <2GB RAM - keyword only
    LOW = "low"  # 4GB RAM - tiny embeddings
    MEDIUM = "medium"  # 6-8GB RAM - standard embeddings + reranker
    HIGH = "high"  # 12GB+ RAM - large embeddings + reranker
    SERVER = "server"  # Cloud/API providers


class SearchMode(StrEnum):
    """Search mode determining which retrieval strategies to use.

    Higher modes provide better quality but require more resources.
    """

    KEYWORD_ONLY = "keyword"  # BM25 only, no ML models
    SEMANTIC_ONLY = "semantic"  # Dense vectors only
    HYBRID = "hybrid"  # BM25 + Dense with RRF fusion
    HYBRID_RERANKED = "hybrid_reranked"  # Hybrid + cross-encoder reranking


class ModelProvider(StrEnum):
    """Model provider/runtime for loading and inference.

    Different providers have different performance characteristics
    and dependencies.
    """

    GGUF = "gguf"  # llama.cpp / llama-cpp-python (quantized)
    MODEL2VEC = "model2vec"  # Static embeddings (500x faster, numpy only)
    ONNX = "onnx"  # ONNX Runtime
    SENTENCE_TRANSFORMERS = "sentence_transformers"  # HuggingFace
    FASTEMBED = "fastembed"  # Qdrant's FastEmbed
    API = "api"  # Cloud API (OpenAI, Voyage, Cohere)


# =============================================================================
# Model Configuration Dataclasses
# =============================================================================


@dataclass
class EmbeddingModelConfig:
    """Configuration for an embedding model.

    Attributes:
        name: Model identifier (HuggingFace ID or API model name)
        provider: Runtime provider for model loading
        size_mb: Approximate model size in megabytes
        dimensions: Output embedding dimensions
        quantization: Quantization format (e.g., "Q4_K_M", "Q8_0")
        matryoshka_dims: Supported Matryoshka dimensions for size/quality tradeoff
        max_tokens: Maximum input token length
        batch_size: Recommended batch size for inference
        metadata: Additional provider-specific configuration
    """

    name: str
    provider: ModelProvider
    size_mb: int
    dimensions: int
    quantization: str | None = None
    matryoshka_dims: list[int] | None = None
    max_tokens: int = 512
    batch_size: int = 32
    metadata: dict[str, Any] = field(default_factory=dict)

    def effective_dimensions(self, target_dim: int | None = None) -> int:
        """Get effective dimensions, respecting Matryoshka if available.

        Args:
            target_dim: Desired dimension (uses closest Matryoshka dim)

        Returns:
            Effective embedding dimensions
        """
        if target_dim is None or self.matryoshka_dims is None:
            return self.dimensions

        # Find closest supported dimension
        valid_dims = [d for d in self.matryoshka_dims if d <= self.dimensions]
        if not valid_dims:
            return self.dimensions

        # Return closest dimension that's >= target
        for dim in sorted(valid_dims):
            if dim >= target_dim:
                return dim
        return max(valid_dims)


@dataclass
class RerankerModelConfig:
    """Configuration for a reranker (cross-encoder) model.

    Attributes:
        name: Model identifier (HuggingFace ID)
        provider: Runtime provider for model loading
        size_mb: Approximate model size in megabytes
        max_length: Maximum input sequence length
        batch_size: Recommended batch size for inference
        metadata: Additional provider-specific configuration
    """

    name: str
    provider: ModelProvider
    size_mb: int
    max_length: int = 512
    batch_size: int = 16
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MobileSearchConfig:
    """Complete search configuration for a device tier.

    This configuration determines which models to load and how
    search should be performed on a given device.

    Attributes:
        tier: Device capability tier
        mode: Search mode (keyword, semantic, hybrid, etc.)
        embedding: Embedding model configuration (None for keyword-only)
        reranker: Reranker model configuration (None if not using reranking)
        server_fallback: Whether to fallback to server API on local failure
        lazy_load: Whether to load models on first use vs startup
        max_memory_mb: Maximum memory budget for models
        unload_after_seconds: Unload models after this many seconds of inactivity
        target_dimensions: Target embedding dimensions (for Matryoshka)
    """

    tier: DeviceTier
    mode: SearchMode
    embedding: EmbeddingModelConfig | None = None
    reranker: RerankerModelConfig | None = None
    server_fallback: bool = True
    lazy_load: bool = True
    max_memory_mb: int = 200
    unload_after_seconds: int = 300  # 5 minutes
    target_dimensions: int | None = None  # For Matryoshka embeddings

    def total_model_size_mb(self) -> int:
        """Calculate total model memory requirement."""
        total = 0
        if self.embedding:
            total += self.embedding.size_mb
        if self.reranker:
            total += self.reranker.size_mb
        return total

    def fits_memory_budget(self) -> bool:
        """Check if configuration fits within memory budget."""
        return self.total_model_size_mb() <= self.max_memory_mb

    def requires_embedding(self) -> bool:
        """Check if this mode requires an embedding model."""
        return self.mode in (
            SearchMode.SEMANTIC_ONLY,
            SearchMode.HYBRID,
            SearchMode.HYBRID_RERANKED,
        )

    def requires_reranker(self) -> bool:
        """Check if this mode requires a reranker model."""
        return self.mode == SearchMode.HYBRID_RERANKED

    def requires_bm25(self) -> bool:
        """Check if this mode requires BM25 index."""
        return self.mode in (
            SearchMode.KEYWORD_ONLY,
            SearchMode.HYBRID,
            SearchMode.HYBRID_RERANKED,
        )


# =============================================================================
# Model Registries
# =============================================================================

# Embedding models optimized for different device tiers
EMBEDDING_MODELS: dict[str, EmbeddingModelConfig] = {
    # Tiny models (<20MB) - for LOW tier
    "arctic-xs": EmbeddingModelConfig(
        name="Snowflake/snowflake-arctic-embed-xs",
        provider=ModelProvider.GGUF,
        size_mb=15,
        dimensions=384,
        quantization="Q8_0",
        max_tokens=512,
        batch_size=64,
        metadata={
            "mteb_score": 50.15,
            "base_model": "all-MiniLM-L6-v2",
            "license": "Apache-2.0",
        },
    ),
    # Model2Vec - ultra-fast static embeddings
    "potion-base-8m": EmbeddingModelConfig(
        name="minishlab/potion-base-8M",
        provider=ModelProvider.MODEL2VEC,
        size_mb=8,
        dimensions=256,
        max_tokens=512,
        batch_size=256,  # Very fast, can handle large batches
        metadata={
            "mteb_score": 48.0,
            "speedup": "500x vs MiniLM",
            "license": "MIT",
        },
    ),
    "potion-base-32m": EmbeddingModelConfig(
        name="minishlab/potion-base-32M",
        provider=ModelProvider.MODEL2VEC,
        size_mb=32,
        dimensions=256,
        max_tokens=512,
        batch_size=256,
        metadata={
            "mteb_score": 51.66,
            "speedup": "500x vs MiniLM",
            "license": "MIT",
        },
    ),
    # Medium models (50-100MB) - for MEDIUM tier
    "nomic-v1.5": EmbeddingModelConfig(
        name="nomic-ai/nomic-embed-text-v1.5",
        provider=ModelProvider.GGUF,
        size_mb=50,
        dimensions=768,
        quantization="Q4_K_M",
        matryoshka_dims=[64, 128, 256, 512, 768],
        max_tokens=8192,
        batch_size=32,
        metadata={
            "mteb_score": 62.39,
            "supports_binary": True,
            "license": "Apache-2.0",
        },
    ),
    "arctic-s": EmbeddingModelConfig(
        name="Snowflake/snowflake-arctic-embed-s",
        provider=ModelProvider.GGUF,
        size_mb=33,
        dimensions=384,
        quantization="Q8_0",
        max_tokens=512,
        batch_size=64,
        metadata={
            "mteb_score": 51.98,
            "license": "Apache-2.0",
        },
    ),
    # Large models (100-200MB) - for HIGH tier
    "embeddinggemma": EmbeddingModelConfig(
        name="google/embeddinggemma-300m",
        provider=ModelProvider.GGUF,
        size_mb=150,
        dimensions=768,
        quantization="Q4_K_M",
        matryoshka_dims=[128, 256, 512, 768],
        max_tokens=2048,
        batch_size=16,
        metadata={
            "languages": "100+",
            "latency_ms": "<15 on EdgeTPU",
            "license": "Gemma Terms",
        },
    ),
    "nomic-v1.5-fp16": EmbeddingModelConfig(
        name="nomic-ai/nomic-embed-text-v1.5",
        provider=ModelProvider.SENTENCE_TRANSFORMERS,
        size_mb=137,
        dimensions=768,
        matryoshka_dims=[64, 128, 256, 512, 768],
        max_tokens=8192,
        batch_size=32,
        metadata={
            "mteb_score": 62.39,
            "supports_binary": True,
            "license": "Apache-2.0",
        },
    ),
    # FastEmbed models (Qdrant's optimized ONNX)
    "fastembed-small": EmbeddingModelConfig(
        name="BAAI/bge-small-en-v1.5",
        provider=ModelProvider.FASTEMBED,
        size_mb=45,
        dimensions=384,
        max_tokens=512,
        batch_size=256,
        metadata={
            "mteb_score": 51.68,
            "license": "MIT",
        },
    ),
}

# Reranker models for quality improvement
RERANKER_MODELS: dict[str, RerankerModelConfig] = {
    "jina-tiny": RerankerModelConfig(
        name="jinaai/jina-reranker-v1-tiny-en",
        provider=ModelProvider.GGUF,
        size_mb=40,
        max_length=8192,
        batch_size=32,
        metadata={
            "params": "33M",
            "beir_score": 48.54,
            "base_model": "JinaBERT",
            "license": "Apache-2.0",
        },
    ),
    "jina-turbo": RerankerModelConfig(
        name="jinaai/jina-reranker-v1-turbo-en",
        provider=ModelProvider.GGUF,
        size_mb=80,
        max_length=8192,
        batch_size=32,
        metadata={
            "params": "66M",
            "beir_score": 50.21,
            "license": "Apache-2.0",
        },
    ),
    "bge-reranker-base": RerankerModelConfig(
        name="BAAI/bge-reranker-base",
        provider=ModelProvider.SENTENCE_TRANSFORMERS,
        size_mb=110,
        max_length=512,
        batch_size=16,
        metadata={
            "params": "110M",
            "license": "MIT",
        },
    ),
}


# =============================================================================
# Tier Presets
# =============================================================================

TIER_PRESETS: dict[DeviceTier, MobileSearchConfig] = {
    DeviceTier.MINIMAL: MobileSearchConfig(
        tier=DeviceTier.MINIMAL,
        mode=SearchMode.KEYWORD_ONLY,
        embedding=None,
        reranker=None,
        max_memory_mb=0,
        server_fallback=True,
    ),
    DeviceTier.LOW: MobileSearchConfig(
        tier=DeviceTier.LOW,
        mode=SearchMode.SEMANTIC_ONLY,
        embedding=EMBEDDING_MODELS["arctic-xs"],
        reranker=None,
        max_memory_mb=50,
        lazy_load=True,
    ),
    DeviceTier.MEDIUM: MobileSearchConfig(
        tier=DeviceTier.MEDIUM,
        mode=SearchMode.HYBRID_RERANKED,
        embedding=EMBEDDING_MODELS["nomic-v1.5"],
        reranker=RERANKER_MODELS["jina-tiny"],
        max_memory_mb=150,
        lazy_load=True,
        target_dimensions=384,  # Use Matryoshka for memory savings
    ),
    DeviceTier.HIGH: MobileSearchConfig(
        tier=DeviceTier.HIGH,
        mode=SearchMode.HYBRID_RERANKED,
        embedding=EMBEDDING_MODELS["embeddinggemma"],
        reranker=RERANKER_MODELS["jina-tiny"],
        max_memory_mb=250,
        lazy_load=True,
    ),
    DeviceTier.SERVER: MobileSearchConfig(
        tier=DeviceTier.SERVER,
        mode=SearchMode.HYBRID_RERANKED,
        embedding=None,  # Use API providers from embeddings.py
        reranker=None,  # Use API providers
        server_fallback=False,  # Server IS the primary
        lazy_load=False,  # No local models to load
    ),
}


# =============================================================================
# Device Detection
# =============================================================================


def get_system_memory_gb() -> float:
    """Get total system memory in gigabytes.

    Returns:
        Total RAM in GB, or 4.0 as fallback if detection fails
    """
    try:
        import psutil

        return psutil.virtual_memory().total / (1024**3)  # type: ignore[no-any-return]
    except ImportError:
        logger.warning("psutil not available, using fallback memory detection")

    # Fallback: try platform-specific methods
    try:
        system = platform.system()
        if system == "Darwin":  # macOS
            import subprocess

            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return int(result.stdout.strip()) / (1024**3)
        elif system == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        # Value is in KB
                        kb = int(line.split()[1])
                        return kb / (1024**2)
    except Exception as e:
        logger.warning(f"Failed to detect system memory: {e}")

    # Conservative fallback
    return 4.0


def get_available_memory_gb() -> float:
    """Get available (free) system memory in gigabytes.

    Returns:
        Available RAM in GB, or 2.0 as fallback if detection fails
    """
    try:
        import psutil

        return psutil.virtual_memory().available / (1024**3)  # type: ignore[no-any-return]
    except ImportError:
        pass

    # Conservative fallback
    return 2.0


def detect_device_tier(
    total_ram_gb: float | None = None,
    available_ram_gb: float | None = None,
) -> DeviceTier:
    """Detect appropriate device tier based on system resources.

    Uses total RAM as primary signal, with available RAM as secondary
    consideration for dynamic adjustment.

    Args:
        total_ram_gb: Override for total RAM (auto-detected if None)
        available_ram_gb: Override for available RAM (auto-detected if None)

    Returns:
        Recommended DeviceTier for this device
    """
    if total_ram_gb is None:
        total_ram_gb = get_system_memory_gb()
    if available_ram_gb is None:
        available_ram_gb = get_available_memory_gb()

    logger.debug(f"Detected RAM: total={total_ram_gb:.1f}GB, available={available_ram_gb:.1f}GB")

    # Primary: base tier on total RAM
    if total_ram_gb < 2:
        base_tier = DeviceTier.MINIMAL
    elif total_ram_gb < 6:
        base_tier = DeviceTier.LOW
    elif total_ram_gb < 10:
        base_tier = DeviceTier.MEDIUM
    elif total_ram_gb < 24:
        base_tier = DeviceTier.HIGH
    else:
        base_tier = DeviceTier.SERVER

    # Secondary: downgrade if available RAM is very low
    if available_ram_gb < 1.0 and base_tier != DeviceTier.MINIMAL:
        logger.warning(
            f"Low available RAM ({available_ram_gb:.1f}GB), "
            f"downgrading from {base_tier} to lower tier"
        )
        tier_order = [
            DeviceTier.MINIMAL,
            DeviceTier.LOW,
            DeviceTier.MEDIUM,
            DeviceTier.HIGH,
            DeviceTier.SERVER,
        ]
        current_idx = tier_order.index(base_tier)
        if current_idx > 0:
            base_tier = tier_order[current_idx - 1]

    logger.info(f"Detected device tier: {base_tier}")
    return base_tier


def get_config_for_tier(tier: DeviceTier) -> MobileSearchConfig:
    """Get the preset configuration for a device tier.

    Args:
        tier: Device capability tier

    Returns:
        MobileSearchConfig for the specified tier
    """
    return TIER_PRESETS[tier]


def auto_detect_config() -> MobileSearchConfig:
    """Auto-detect device tier and return appropriate configuration.

    This is the main entry point for automatic configuration.

    Returns:
        MobileSearchConfig optimized for the current device

    Example:
        >>> config = auto_detect_config()
        >>> print(f"Using {config.tier} tier with {config.mode} search")
    """
    tier = detect_device_tier()
    return get_config_for_tier(tier)


# =============================================================================
# Configuration Customization
# =============================================================================


def create_custom_config(
    tier: DeviceTier | None = None,
    mode: SearchMode | None = None,
    embedding_name: str | None = None,
    reranker_name: str | None = None,
    max_memory_mb: int | None = None,
    **kwargs: Any,
) -> MobileSearchConfig:
    """Create a custom search configuration.

    Allows mixing and matching models and settings beyond the presets.

    Args:
        tier: Device tier (auto-detected if None)
        mode: Search mode (uses tier default if None)
        embedding_name: Embedding model key from EMBEDDING_MODELS
        reranker_name: Reranker model key from RERANKER_MODELS
        max_memory_mb: Memory budget override
        **kwargs: Additional MobileSearchConfig fields

    Returns:
        Customized MobileSearchConfig

    Example:
        >>> config = create_custom_config(
        ...     tier=DeviceTier.MEDIUM,
        ...     mode=SearchMode.HYBRID,  # Skip reranking for speed
        ...     embedding_name="nomic-v1.5",
        ...     max_memory_mb=100,
        ... )
    """
    # Start with tier preset or defaults
    if tier is None:
        tier = detect_device_tier()

    base_config = TIER_PRESETS.get(tier, TIER_PRESETS[DeviceTier.LOW])

    # Override with custom values
    config_mode = mode if mode is not None else base_config.mode

    embedding = None
    if embedding_name:
        if embedding_name not in EMBEDDING_MODELS:
            raise ValueError(
                f"Unknown embedding model: {embedding_name}. "
                f"Available: {list(EMBEDDING_MODELS.keys())}"
            )
        embedding = EMBEDDING_MODELS[embedding_name]
    elif base_config.embedding:
        embedding = base_config.embedding

    reranker = None
    if reranker_name:
        if reranker_name not in RERANKER_MODELS:
            raise ValueError(
                f"Unknown reranker model: {reranker_name}. "
                f"Available: {list(RERANKER_MODELS.keys())}"
            )
        reranker = RERANKER_MODELS[reranker_name]
    elif base_config.reranker and config_mode == SearchMode.HYBRID_RERANKED:
        reranker = base_config.reranker

    memory_budget = max_memory_mb if max_memory_mb is not None else base_config.max_memory_mb

    return MobileSearchConfig(
        tier=tier,
        mode=config_mode,
        embedding=embedding,
        reranker=reranker,
        max_memory_mb=memory_budget,
        server_fallback=kwargs.get("server_fallback", base_config.server_fallback),
        lazy_load=kwargs.get("lazy_load", base_config.lazy_load),
        unload_after_seconds=kwargs.get("unload_after_seconds", base_config.unload_after_seconds),
        target_dimensions=kwargs.get("target_dimensions", base_config.target_dimensions),
    )


def list_available_models() -> dict[str, dict[str, Any]]:
    """List all available embedding and reranker models.

    Returns:
        Dictionary with 'embeddings' and 'rerankers' keys

    Example:
        >>> models = list_available_models()
        >>> for name, config in models['embeddings'].items():
        ...     print(f"{name}: {config.size_mb}MB, {config.dimensions}d")
    """
    return {
        "embeddings": {
            name: {
                "name": cfg.name,
                "provider": cfg.provider.value,
                "size_mb": cfg.size_mb,
                "dimensions": cfg.dimensions,
                "quantization": cfg.quantization,
                "matryoshka": cfg.matryoshka_dims,
                **cfg.metadata,
            }
            for name, cfg in EMBEDDING_MODELS.items()
        },
        "rerankers": {
            name: {
                "name": cfg.name,
                "provider": cfg.provider.value,
                "size_mb": cfg.size_mb,
                "max_length": cfg.max_length,
                **cfg.metadata,
            }
            for name, cfg in RERANKER_MODELS.items()
        },
    }
