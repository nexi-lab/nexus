"""Unit tests for mobile search configuration (Issue #1213)."""

from __future__ import annotations

import pytest

from nexus.search.mobile_config import (
    EMBEDDING_MODELS,
    RERANKER_MODELS,
    TIER_PRESETS,
    DeviceTier,
    EmbeddingModelConfig,
    MobileSearchConfig,
    ModelProvider,
    RerankerModelConfig,
    SearchMode,
    auto_detect_config,
    create_custom_config,
    detect_device_tier,
    get_config_for_tier,
    list_available_models,
)


class TestDeviceTier:
    """Tests for DeviceTier enum."""

    def test_tier_values(self):
        """Test all tier values are defined."""
        assert DeviceTier.MINIMAL == "minimal"
        assert DeviceTier.LOW == "low"
        assert DeviceTier.MEDIUM == "medium"
        assert DeviceTier.HIGH == "high"
        assert DeviceTier.SERVER == "server"

    def test_tier_ordering(self):
        """Test tiers can be compared as strings."""
        tiers = [DeviceTier.MINIMAL, DeviceTier.LOW, DeviceTier.MEDIUM, DeviceTier.HIGH]
        assert len(tiers) == 4


class TestSearchMode:
    """Tests for SearchMode enum."""

    def test_mode_values(self):
        """Test all search mode values are defined."""
        assert SearchMode.KEYWORD_ONLY == "keyword"
        assert SearchMode.SEMANTIC_ONLY == "semantic"
        assert SearchMode.HYBRID == "hybrid"
        assert SearchMode.HYBRID_RERANKED == "hybrid_reranked"


class TestModelProvider:
    """Tests for ModelProvider enum."""

    def test_provider_values(self):
        """Test all provider values are defined."""
        assert ModelProvider.GGUF == "gguf"
        assert ModelProvider.MODEL2VEC == "model2vec"
        assert ModelProvider.ONNX == "onnx"
        assert ModelProvider.SENTENCE_TRANSFORMERS == "sentence_transformers"
        assert ModelProvider.FASTEMBED == "fastembed"
        assert ModelProvider.API == "api"


class TestEmbeddingModelConfig:
    """Tests for EmbeddingModelConfig dataclass."""

    def test_basic_config(self):
        """Test creating a basic embedding config."""
        config = EmbeddingModelConfig(
            name="test-model",
            provider=ModelProvider.GGUF,
            size_mb=50,
            dimensions=384,
        )
        assert config.name == "test-model"
        assert config.provider == ModelProvider.GGUF
        assert config.size_mb == 50
        assert config.dimensions == 384
        assert config.quantization is None
        assert config.matryoshka_dims is None

    def test_config_with_matryoshka(self):
        """Test config with Matryoshka dimensions."""
        config = EmbeddingModelConfig(
            name="test-model",
            provider=ModelProvider.GGUF,
            size_mb=50,
            dimensions=768,
            matryoshka_dims=[64, 128, 256, 512, 768],
        )
        assert config.matryoshka_dims == [64, 128, 256, 512, 768]

    def test_effective_dimensions_no_matryoshka(self):
        """Test effective_dimensions without Matryoshka."""
        config = EmbeddingModelConfig(
            name="test",
            provider=ModelProvider.GGUF,
            size_mb=50,
            dimensions=384,
        )
        assert config.effective_dimensions() == 384
        assert config.effective_dimensions(256) == 384  # No Matryoshka, returns full

    def test_effective_dimensions_with_matryoshka(self):
        """Test effective_dimensions with Matryoshka."""
        config = EmbeddingModelConfig(
            name="test",
            provider=ModelProvider.GGUF,
            size_mb=50,
            dimensions=768,
            matryoshka_dims=[64, 128, 256, 512, 768],
        )
        # Returns smallest dimension >= target
        assert config.effective_dimensions(100) == 128
        assert config.effective_dimensions(128) == 128
        assert config.effective_dimensions(200) == 256
        assert config.effective_dimensions(512) == 512
        assert config.effective_dimensions(768) == 768
        assert config.effective_dimensions(1000) == 768  # Max available


class TestRerankerModelConfig:
    """Tests for RerankerModelConfig dataclass."""

    def test_basic_config(self):
        """Test creating a basic reranker config."""
        config = RerankerModelConfig(
            name="test-reranker",
            provider=ModelProvider.GGUF,
            size_mb=40,
        )
        assert config.name == "test-reranker"
        assert config.provider == ModelProvider.GGUF
        assert config.size_mb == 40
        assert config.max_length == 512  # default
        assert config.batch_size == 16  # default


class TestMobileSearchConfig:
    """Tests for MobileSearchConfig dataclass."""

    def test_basic_config(self):
        """Test creating a basic search config."""
        config = MobileSearchConfig(
            tier=DeviceTier.LOW,
            mode=SearchMode.SEMANTIC_ONLY,
        )
        assert config.tier == DeviceTier.LOW
        assert config.mode == SearchMode.SEMANTIC_ONLY
        assert config.embedding is None
        assert config.reranker is None
        assert config.server_fallback is True
        assert config.lazy_load is True

    def test_total_model_size(self):
        """Test total model size calculation."""
        embedding = EmbeddingModelConfig(
            name="test", provider=ModelProvider.GGUF, size_mb=50, dimensions=384
        )
        reranker = RerankerModelConfig(
            name="test", provider=ModelProvider.GGUF, size_mb=40
        )

        config = MobileSearchConfig(
            tier=DeviceTier.MEDIUM,
            mode=SearchMode.HYBRID_RERANKED,
            embedding=embedding,
            reranker=reranker,
        )
        assert config.total_model_size_mb() == 90

    def test_fits_memory_budget(self):
        """Test memory budget check."""
        embedding = EmbeddingModelConfig(
            name="test", provider=ModelProvider.GGUF, size_mb=100, dimensions=384
        )

        # Under budget
        config1 = MobileSearchConfig(
            tier=DeviceTier.MEDIUM,
            mode=SearchMode.SEMANTIC_ONLY,
            embedding=embedding,
            max_memory_mb=150,
        )
        assert config1.fits_memory_budget() is True

        # Over budget
        config2 = MobileSearchConfig(
            tier=DeviceTier.MEDIUM,
            mode=SearchMode.SEMANTIC_ONLY,
            embedding=embedding,
            max_memory_mb=50,
        )
        assert config2.fits_memory_budget() is False

    def test_requires_embedding(self):
        """Test requires_embedding for different modes."""
        for mode in [SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID, SearchMode.HYBRID_RERANKED]:
            config = MobileSearchConfig(tier=DeviceTier.MEDIUM, mode=mode)
            assert config.requires_embedding() is True

        config_kw = MobileSearchConfig(tier=DeviceTier.MINIMAL, mode=SearchMode.KEYWORD_ONLY)
        assert config_kw.requires_embedding() is False

    def test_requires_reranker(self):
        """Test requires_reranker for different modes."""
        config_rerank = MobileSearchConfig(
            tier=DeviceTier.MEDIUM, mode=SearchMode.HYBRID_RERANKED
        )
        assert config_rerank.requires_reranker() is True

        for mode in [SearchMode.KEYWORD_ONLY, SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID]:
            config = MobileSearchConfig(tier=DeviceTier.MEDIUM, mode=mode)
            assert config.requires_reranker() is False

    def test_requires_bm25(self):
        """Test requires_bm25 for different modes."""
        for mode in [SearchMode.KEYWORD_ONLY, SearchMode.HYBRID, SearchMode.HYBRID_RERANKED]:
            config = MobileSearchConfig(tier=DeviceTier.MEDIUM, mode=mode)
            assert config.requires_bm25() is True

        config_semantic = MobileSearchConfig(
            tier=DeviceTier.MEDIUM, mode=SearchMode.SEMANTIC_ONLY
        )
        assert config_semantic.requires_bm25() is False


class TestModelRegistries:
    """Tests for EMBEDDING_MODELS and RERANKER_MODELS registries."""

    def test_embedding_models_exist(self):
        """Test that expected embedding models are registered."""
        expected = ["arctic-xs", "nomic-v1.5", "embeddinggemma", "potion-base-8m"]
        for name in expected:
            assert name in EMBEDDING_MODELS, f"Missing embedding model: {name}"

    def test_embedding_models_valid(self):
        """Test that all embedding models have required fields."""
        for name, config in EMBEDDING_MODELS.items():
            assert config.name, f"{name}: missing name"
            assert config.provider in ModelProvider, f"{name}: invalid provider"
            assert config.size_mb > 0, f"{name}: invalid size"
            assert config.dimensions > 0, f"{name}: invalid dimensions"

    def test_reranker_models_exist(self):
        """Test that expected reranker models are registered."""
        expected = ["jina-tiny", "jina-turbo"]
        for name in expected:
            assert name in RERANKER_MODELS, f"Missing reranker model: {name}"

    def test_reranker_models_valid(self):
        """Test that all reranker models have required fields."""
        for name, config in RERANKER_MODELS.items():
            assert config.name, f"{name}: missing name"
            assert config.provider in ModelProvider, f"{name}: invalid provider"
            assert config.size_mb > 0, f"{name}: invalid size"
            assert config.max_length > 0, f"{name}: invalid max_length"


class TestTierPresets:
    """Tests for TIER_PRESETS configurations."""

    def test_all_tiers_have_presets(self):
        """Test that all device tiers have presets."""
        for tier in DeviceTier:
            assert tier in TIER_PRESETS, f"Missing preset for tier: {tier}"

    def test_minimal_tier_is_keyword_only(self):
        """Test MINIMAL tier uses keyword-only search."""
        config = TIER_PRESETS[DeviceTier.MINIMAL]
        assert config.mode == SearchMode.KEYWORD_ONLY
        assert config.embedding is None
        assert config.reranker is None
        assert config.max_memory_mb == 0

    def test_low_tier_has_small_embedding(self):
        """Test LOW tier uses a small embedding model."""
        config = TIER_PRESETS[DeviceTier.LOW]
        assert config.embedding is not None
        assert config.embedding.size_mb <= 50
        assert config.reranker is None

    def test_medium_tier_has_embedding_and_reranker(self):
        """Test MEDIUM tier uses both embedding and reranker."""
        config = TIER_PRESETS[DeviceTier.MEDIUM]
        assert config.embedding is not None
        assert config.reranker is not None
        assert config.mode == SearchMode.HYBRID_RERANKED

    def test_high_tier_has_large_embedding(self):
        """Test HIGH tier uses a larger embedding model."""
        config = TIER_PRESETS[DeviceTier.HIGH]
        assert config.embedding is not None
        assert config.embedding.size_mb >= 100
        assert config.reranker is not None

    def test_server_tier_uses_api(self):
        """Test SERVER tier is configured for API providers."""
        config = TIER_PRESETS[DeviceTier.SERVER]
        assert config.embedding is None  # Uses API
        assert config.reranker is None  # Uses API
        assert config.server_fallback is False  # Server IS primary

    def test_presets_fit_memory_budget(self):
        """Test all presets fit within their memory budgets."""
        for tier, config in TIER_PRESETS.items():
            if config.max_memory_mb > 0:  # Skip MINIMAL/SERVER
                assert config.fits_memory_budget(), f"{tier} preset exceeds memory budget"


class TestDeviceDetection:
    """Tests for device tier detection functions."""

    def test_detect_tier_minimal(self):
        """Test detection of MINIMAL tier."""
        tier = detect_device_tier(total_ram_gb=1.5)
        assert tier == DeviceTier.MINIMAL

    def test_detect_tier_low(self):
        """Test detection of LOW tier."""
        tier = detect_device_tier(total_ram_gb=4.0)
        assert tier == DeviceTier.LOW

    def test_detect_tier_medium(self):
        """Test detection of MEDIUM tier."""
        tier = detect_device_tier(total_ram_gb=8.0)
        assert tier == DeviceTier.MEDIUM

    def test_detect_tier_high(self):
        """Test detection of HIGH tier."""
        tier = detect_device_tier(total_ram_gb=16.0)
        assert tier == DeviceTier.HIGH

    def test_detect_tier_server(self):
        """Test detection of SERVER tier."""
        tier = detect_device_tier(total_ram_gb=32.0)
        assert tier == DeviceTier.SERVER

    def test_detect_tier_downgrade_on_low_available(self):
        """Test tier downgrade when available RAM is very low."""
        # HIGH tier device but almost no available RAM
        tier = detect_device_tier(total_ram_gb=16.0, available_ram_gb=0.5)
        assert tier == DeviceTier.MEDIUM  # Downgraded

    def test_get_config_for_tier(self):
        """Test getting config for a specific tier."""
        config = get_config_for_tier(DeviceTier.LOW)
        assert config.tier == DeviceTier.LOW
        assert config == TIER_PRESETS[DeviceTier.LOW]

    def test_auto_detect_config(self):
        """Test auto-detection returns valid config."""
        config = auto_detect_config()
        assert isinstance(config, MobileSearchConfig)
        assert config.tier in DeviceTier


class TestCustomConfig:
    """Tests for custom configuration creation."""

    def test_create_custom_config_basic(self):
        """Test creating a custom config with defaults."""
        config = create_custom_config(tier=DeviceTier.LOW)
        assert config.tier == DeviceTier.LOW

    def test_create_custom_config_override_mode(self):
        """Test overriding search mode."""
        config = create_custom_config(
            tier=DeviceTier.MEDIUM,
            mode=SearchMode.HYBRID,  # Without reranking
        )
        assert config.mode == SearchMode.HYBRID
        assert config.reranker is None  # Not added for HYBRID mode

    def test_create_custom_config_custom_embedding(self):
        """Test specifying a custom embedding model."""
        config = create_custom_config(
            tier=DeviceTier.LOW,
            embedding_name="potion-base-8m",
        )
        assert config.embedding is not None
        assert config.embedding.name == "minishlab/potion-base-8M"

    def test_create_custom_config_custom_reranker(self):
        """Test specifying a custom reranker model."""
        config = create_custom_config(
            tier=DeviceTier.MEDIUM,
            mode=SearchMode.HYBRID_RERANKED,
            reranker_name="jina-turbo",
        )
        assert config.reranker is not None
        assert config.reranker.name == "jinaai/jina-reranker-v1-turbo-en"

    def test_create_custom_config_invalid_embedding(self):
        """Test error on invalid embedding model name."""
        with pytest.raises(ValueError, match="Unknown embedding model"):
            create_custom_config(embedding_name="nonexistent-model")

    def test_create_custom_config_invalid_reranker(self):
        """Test error on invalid reranker model name."""
        with pytest.raises(ValueError, match="Unknown reranker model"):
            create_custom_config(reranker_name="nonexistent-reranker")

    def test_create_custom_config_memory_override(self):
        """Test overriding memory budget."""
        config = create_custom_config(
            tier=DeviceTier.MEDIUM,
            max_memory_mb=500,
        )
        assert config.max_memory_mb == 500


class TestListAvailableModels:
    """Tests for list_available_models function."""

    def test_returns_dict_with_categories(self):
        """Test return structure has embeddings and rerankers."""
        models = list_available_models()
        assert "embeddings" in models
        assert "rerankers" in models

    def test_embeddings_have_expected_fields(self):
        """Test embedding entries have expected fields."""
        models = list_available_models()
        for name, info in models["embeddings"].items():
            assert "name" in info
            assert "provider" in info
            assert "size_mb" in info
            assert "dimensions" in info

    def test_rerankers_have_expected_fields(self):
        """Test reranker entries have expected fields."""
        models = list_available_models()
        for name, info in models["rerankers"].items():
            assert "name" in info
            assert "provider" in info
            assert "size_mb" in info
            assert "max_length" in info
