"""End-to-end tests for Mobile Search Configuration (Issue #1213).

These tests verify that the mobile search configuration integrates correctly
with the actual search infrastructure using different device tiers.
"""

from __future__ import annotations

import tempfile

import pytest

from nexus.search.mobile_config import (
    EMBEDDING_MODELS,
    TIER_PRESETS,
    DeviceTier,
    MobileSearchConfig,
    SearchMode,
    auto_detect_config,
    create_custom_config,
    detect_device_tier,
    get_config_for_tier,
    list_available_models,
)


class TestDeviceDetectionE2E:
    """E2E tests for device tier detection on real hardware."""

    def test_auto_detect_returns_valid_tier(self):
        """Test that auto-detection works on real hardware."""
        tier = detect_device_tier()
        assert tier in DeviceTier
        print(f"\nDetected device tier: {tier}")

    def test_auto_detect_config_returns_valid_config(self):
        """Test that auto_detect_config returns a usable configuration."""
        config = auto_detect_config()
        assert isinstance(config, MobileSearchConfig)
        assert config.tier in DeviceTier
        assert config.mode in SearchMode
        print("\nAuto-detected config:")
        print(f"  Tier: {config.tier}")
        print(f"  Mode: {config.mode}")
        print(f"  Embedding: {config.embedding.name if config.embedding else 'API'}")
        print(f"  Reranker: {config.reranker.name if config.reranker else 'None'}")
        print(f"  Memory budget: {config.max_memory_mb}MB")


class TestTierPresetsE2E:
    """E2E tests for tier preset configurations."""

    @pytest.mark.parametrize("tier", list(DeviceTier))
    def test_tier_preset_is_valid(self, tier: DeviceTier):
        """Test that each tier preset is valid and consistent."""
        config = get_config_for_tier(tier)

        # Verify basic structure
        assert config.tier == tier
        assert config.mode in SearchMode

        # Verify mode-model consistency
        if config.mode == SearchMode.KEYWORD_ONLY:
            assert config.embedding is None, f"{tier}: keyword-only should have no embedding"
        elif (
            config.mode in (SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID)
            and tier != DeviceTier.SERVER
        ):
            assert config.embedding is not None, f"{tier}: semantic modes need embedding"
        elif config.mode == SearchMode.HYBRID_RERANKED and tier != DeviceTier.SERVER:
            assert config.embedding is not None, f"{tier}: reranked mode needs embedding"

        # Verify memory budget
        if config.embedding or config.reranker:
            assert config.fits_memory_budget(), f"{tier}: preset exceeds memory budget"

        print(f"\n{tier}: mode={config.mode}, memory={config.total_model_size_mb()}MB")


class TestModelRegistryE2E:
    """E2E tests for model registry completeness."""

    def test_all_preset_models_exist_in_registry(self):
        """Test that all models referenced in presets exist in registries."""
        for tier, config in TIER_PRESETS.items():
            if config.embedding:
                # Find the model in registry
                found = False
                for model in EMBEDDING_MODELS.values():
                    if model.name == config.embedding.name:
                        found = True
                        break
                assert found, f"{tier}: embedding {config.embedding.name} not in registry"

    def test_list_available_models_complete(self):
        """Test that list_available_models returns all registered models."""
        models = list_available_models()

        assert len(models["embeddings"]) == len(EMBEDDING_MODELS)
        assert len(models["rerankers"]) > 0

        print("\nAvailable models:")
        print(f"  Embeddings: {len(models['embeddings'])}")
        for name, info in models["embeddings"].items():
            print(f"    - {name}: {info['size_mb']}MB, {info['dimensions']}d")
        print(f"  Rerankers: {len(models['rerankers'])}")
        for name, info in models["rerankers"].items():
            print(f"    - {name}: {info['size_mb']}MB")


class TestCustomConfigE2E:
    """E2E tests for custom configuration creation."""

    def test_create_low_memory_config(self):
        """Test creating a config optimized for low memory."""
        config = create_custom_config(
            tier=DeviceTier.LOW,
            embedding_name="potion-base-8m",  # Smallest model
            max_memory_mb=20,
        )

        assert config.embedding is not None
        assert config.embedding.size_mb <= 20
        assert config.fits_memory_budget()
        print(f"\nLow memory config: {config.embedding.name}, {config.embedding.size_mb}MB")

    def test_create_high_quality_config(self):
        """Test creating a config optimized for quality."""
        config = create_custom_config(
            tier=DeviceTier.HIGH,
            mode=SearchMode.HYBRID_RERANKED,
            embedding_name="embeddinggemma",
            reranker_name="jina-tiny",
            max_memory_mb=300,
        )

        assert config.embedding is not None
        assert config.reranker is not None
        assert config.mode == SearchMode.HYBRID_RERANKED
        print("\nHigh quality config:")
        print(f"  Embedding: {config.embedding.name}")
        print(f"  Reranker: {config.reranker.name}")
        print(f"  Total size: {config.total_model_size_mb()}MB")

    def test_create_hybrid_without_reranking(self):
        """Test creating hybrid config without reranking for speed."""
        config = create_custom_config(
            tier=DeviceTier.MEDIUM,
            mode=SearchMode.HYBRID,  # No reranking
            embedding_name="nomic-v1.5",
        )

        assert config.mode == SearchMode.HYBRID
        assert config.reranker is None  # Should not add reranker for HYBRID mode
        assert config.embedding is not None
        print(f"\nHybrid (no rerank) config: {config.embedding.name}")


class TestMatryoshkaE2E:
    """E2E tests for Matryoshka embedding dimension selection."""

    def test_nomic_matryoshka_dimensions(self):
        """Test Matryoshka dimension selection for Nomic model."""
        model = EMBEDDING_MODELS["nomic-v1.5"]

        assert model.matryoshka_dims is not None
        assert 768 in model.matryoshka_dims  # Full dimension
        assert 64 in model.matryoshka_dims  # Smallest

        # Test dimension selection
        assert model.effective_dimensions(100) == 128
        assert model.effective_dimensions(256) == 256
        assert model.effective_dimensions(500) == 512
        assert model.effective_dimensions(768) == 768

        print(f"\nNomic Matryoshka dims: {model.matryoshka_dims}")

    def test_embeddinggemma_matryoshka_dimensions(self):
        """Test Matryoshka dimension selection for EmbeddingGemma."""
        model = EMBEDDING_MODELS["embeddinggemma"]

        assert model.matryoshka_dims is not None
        assert 768 in model.matryoshka_dims

        print(f"\nEmbeddingGemma Matryoshka dims: {model.matryoshka_dims}")

    def test_config_with_target_dimensions(self):
        """Test config with target dimensions for memory savings."""
        config = create_custom_config(
            tier=DeviceTier.MEDIUM,
            embedding_name="nomic-v1.5",
            target_dimensions=256,  # Use smaller embeddings
        )

        assert config.target_dimensions == 256
        if config.embedding and config.embedding.matryoshka_dims:
            effective = config.embedding.effective_dimensions(256)
            assert effective == 256
            print(f"\nTarget dims: 256, Effective: {effective}")


class TestSearchModeRequirementsE2E:
    """E2E tests for search mode requirements."""

    def test_keyword_only_requirements(self):
        """Test KEYWORD_ONLY mode requirements."""
        config = MobileSearchConfig(
            tier=DeviceTier.MINIMAL,
            mode=SearchMode.KEYWORD_ONLY,
        )

        assert config.requires_bm25() is True
        assert config.requires_embedding() is False
        assert config.requires_reranker() is False

    def test_semantic_only_requirements(self):
        """Test SEMANTIC_ONLY mode requirements."""
        config = get_config_for_tier(DeviceTier.LOW)

        assert config.requires_bm25() is False
        assert config.requires_embedding() is True
        assert config.requires_reranker() is False

    def test_hybrid_requirements(self):
        """Test HYBRID mode requirements."""
        config = create_custom_config(
            tier=DeviceTier.MEDIUM,
            mode=SearchMode.HYBRID,
        )

        assert config.requires_bm25() is True
        assert config.requires_embedding() is True
        assert config.requires_reranker() is False

    def test_hybrid_reranked_requirements(self):
        """Test HYBRID_RERANKED mode requirements."""
        config = get_config_for_tier(DeviceTier.MEDIUM)

        assert config.requires_bm25() is True
        assert config.requires_embedding() is True
        assert config.requires_reranker() is True


class TestMemoryBudgetE2E:
    """E2E tests for memory budget management."""

    def test_minimal_tier_zero_memory(self):
        """Test MINIMAL tier has zero model memory."""
        config = get_config_for_tier(DeviceTier.MINIMAL)
        assert config.total_model_size_mb() == 0
        assert config.max_memory_mb == 0

    def test_low_tier_under_50mb(self):
        """Test LOW tier stays under 50MB."""
        config = get_config_for_tier(DeviceTier.LOW)
        assert config.total_model_size_mb() <= 50
        assert config.fits_memory_budget()

    def test_medium_tier_under_200mb(self):
        """Test MEDIUM tier stays under 200MB."""
        config = get_config_for_tier(DeviceTier.MEDIUM)
        assert config.total_model_size_mb() <= 200
        assert config.fits_memory_budget()

    def test_high_tier_under_300mb(self):
        """Test HIGH tier stays under 300MB."""
        config = get_config_for_tier(DeviceTier.HIGH)
        assert config.total_model_size_mb() <= 300
        assert config.fits_memory_budget()

    def test_memory_budget_exceeded_detection(self):
        """Test detection of exceeded memory budget."""
        config = create_custom_config(
            tier=DeviceTier.LOW,
            embedding_name="embeddinggemma",  # 150MB - too big for LOW
            max_memory_mb=50,
        )

        assert config.fits_memory_budget() is False
        print(f"\nBudget test: {config.total_model_size_mb()}MB > {config.max_memory_mb}MB budget")


class TestProviderE2E:
    """E2E tests for model provider configurations."""

    def test_gguf_models_have_quantization(self):
        """Test GGUF models specify quantization."""
        from nexus.search.mobile_config import ModelProvider

        for name, model in EMBEDDING_MODELS.items():
            if model.provider == ModelProvider.GGUF:
                assert model.quantization is not None, f"{name}: GGUF model missing quantization"
                print(f"\n{name}: {model.quantization}")

    def test_model2vec_models_are_fast(self):
        """Test Model2Vec models are configured for speed."""
        from nexus.search.mobile_config import ModelProvider

        for name, model in EMBEDDING_MODELS.items():
            if model.provider == ModelProvider.MODEL2VEC:
                # Model2Vec should have large batch sizes (fast inference)
                assert model.batch_size >= 128, f"{name}: Model2Vec should support large batches"
                # Model2Vec should be small
                assert model.size_mb <= 50, f"{name}: Model2Vec models should be small"
                print(f"\n{name}: batch_size={model.batch_size}, size={model.size_mb}MB")


class TestIntegrationWithBM25:
    """Integration tests with BM25S search."""

    def test_bm25_available_for_keyword_mode(self):
        """Test BM25S is available for keyword-only search."""
        from nexus.search import is_bm25s_available

        config = get_config_for_tier(DeviceTier.MINIMAL)
        assert config.mode == SearchMode.KEYWORD_ONLY

        # BM25S should be available
        available = is_bm25s_available()
        print(f"\nBM25S available: {available}")
        if not available:
            pytest.skip("BM25S not available")

    @pytest.mark.asyncio
    async def test_bm25_index_creation(self):
        """Test BM25S index can be created for keyword search."""
        from nexus.search import BM25SIndex, is_bm25s_available

        if not is_bm25s_available():
            pytest.skip("BM25S not available")

        config = get_config_for_tier(DeviceTier.MINIMAL)
        assert config.requires_bm25()

        # Create a simple index - format is (path_id, path, content)
        documents = [
            ("doc1", "/test/doc1.txt", "The quick brown fox jumps over the lazy dog"),
            ("doc2", "/test/doc2.txt", "Machine learning models for search"),
            ("doc3", "/test/doc3.txt", "Mobile edge computing with small models"),
            ("doc4", "/test/doc4.txt", "Embedding vectors for semantic search"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            index = BM25SIndex(index_dir=tmpdir)
            count = await index.index_documents_bulk(documents)
            assert count == 4, f"Expected 4 documents indexed, got {count}"

            # Search
            results = await index.search("mobile models", limit=2)
            assert len(results) > 0
            print(f"\nBM25 search results: {[r.path_id for r in results]}")


class TestPerformanceE2E:
    """Performance tests for configuration operations."""

    def test_config_creation_speed(self):
        """Test that config creation is fast."""
        import time

        iterations = 1000
        start = time.perf_counter()

        for _ in range(iterations):
            _config = create_custom_config(
                tier=DeviceTier.MEDIUM,
                embedding_name="nomic-v1.5",
            )

        elapsed = time.perf_counter() - start
        per_config_us = (elapsed / iterations) * 1_000_000

        print(f"\nConfig creation: {per_config_us:.1f}μs per config")
        assert per_config_us < 1000, "Config creation should be <1ms"

    def test_tier_detection_speed(self):
        """Test that tier detection is fast."""
        import time

        iterations = 100
        start = time.perf_counter()

        for _ in range(iterations):
            _tier = detect_device_tier()

        elapsed = time.perf_counter() - start
        per_detection_ms = (elapsed / iterations) * 1000

        print(f"\nTier detection: {per_detection_ms:.1f}ms per detection")
        # Allow more time since it may involve system calls
        assert per_detection_ms < 100, "Tier detection should be <100ms"


class TestEdgeCasesE2E:
    """E2E tests for edge cases and error handling."""

    def test_invalid_embedding_name_error(self):
        """Test error on invalid embedding name."""
        with pytest.raises(ValueError, match="Unknown embedding model"):
            create_custom_config(embedding_name="nonexistent-model-xyz")

    def test_invalid_reranker_name_error(self):
        """Test error on invalid reranker name."""
        with pytest.raises(ValueError, match="Unknown reranker model"):
            create_custom_config(reranker_name="nonexistent-reranker-xyz")

    def test_extreme_ram_values(self):
        """Test tier detection with extreme RAM values."""
        # Very low RAM
        tier = detect_device_tier(total_ram_gb=0.5)
        assert tier == DeviceTier.MINIMAL

        # Very high RAM
        tier = detect_device_tier(total_ram_gb=256)
        assert tier == DeviceTier.SERVER

    def test_none_embedding_for_server(self):
        """Test SERVER tier has no local embedding config."""
        config = get_config_for_tier(DeviceTier.SERVER)
        assert config.embedding is None
        assert config.reranker is None
        assert config.server_fallback is False  # Server IS primary
