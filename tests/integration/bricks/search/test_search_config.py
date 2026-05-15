"""Tests for SearchConfig and environment helpers (Issue #1520).

Validates:
- SearchConfig default values
- Frozen (immutable) enforcement
- search_config_from_env() reads env vars correctly
- get_env_bool/float/int helpers
"""

import dataclasses

import pytest

from nexus.bricks.search.config import (
    SearchConfig,
    get_env_bool,
    get_env_float,
    get_env_int,
    search_config_from_env,
)

# =============================================================================
# SearchConfig defaults
# =============================================================================


class TestSearchConfigDefaults:
    """Verify all default values are correct."""

    def test_chunk_size_default(self) -> None:
        config = SearchConfig()
        assert config.chunk_size == 1024

    def test_chunk_strategy_default(self) -> None:
        config = SearchConfig()
        assert config.chunk_strategy == "semantic"

    def test_overlap_size_default(self) -> None:
        config = SearchConfig()
        assert config.overlap_size == 128

    def test_entropy_filtering_default(self) -> None:
        config = SearchConfig()
        assert config.entropy_filtering is False

    def test_entropy_threshold_default(self) -> None:
        config = SearchConfig()
        assert config.entropy_threshold == 0.35

    def test_entropy_alpha_default(self) -> None:
        config = SearchConfig()
        assert config.entropy_alpha == 0.5

    def test_fusion_method_default(self) -> None:
        config = SearchConfig()
        assert config.fusion_method == "rrf"

    def test_fusion_alpha_default(self) -> None:
        config = SearchConfig()
        assert config.fusion_alpha == 0.5

    def test_rrf_k_default(self) -> None:
        config = SearchConfig()
        assert config.rrf_k == 60

    def test_embedding_provider_default(self) -> None:
        config = SearchConfig()
        assert config.embedding_provider == "openai"

    def test_embedding_model_default(self) -> None:
        config = SearchConfig()
        assert config.embedding_model is None

    def test_pool_min_size_default(self) -> None:
        config = SearchConfig()
        assert config.pool_min_size == 10

    def test_pool_max_size_default(self) -> None:
        config = SearchConfig()
        assert config.pool_max_size == 50

    def test_pool_recycle_default(self) -> None:
        config = SearchConfig()
        assert config.pool_recycle == 1800

    def test_search_mode_default(self) -> None:
        config = SearchConfig()
        assert config.search_mode == "hybrid"

    def test_contextual_chunking_default(self) -> None:
        config = SearchConfig()
        assert config.contextual_chunking is False

    def test_enable_attribute_boosting_default(self) -> None:
        config = SearchConfig()
        assert config.enable_attribute_boosting is True


# =============================================================================
# Frozen (immutable)
# =============================================================================


class TestSearchConfigFrozen:
    """SearchConfig must be frozen — mutation raises FrozenInstanceError."""

    def test_frozen_raises_on_assignment(self) -> None:
        config = SearchConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.chunk_size = 2048  # type: ignore[misc]

    def test_frozen_raises_on_new_attribute(self) -> None:
        config = SearchConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.new_field = "value"  # type: ignore[attr-defined]

    def test_is_dataclass(self) -> None:
        config = SearchConfig()
        assert dataclasses.is_dataclass(config)

    def test_custom_values_preserved(self) -> None:
        config = SearchConfig(chunk_size=512, fusion_method="weighted")
        assert config.chunk_size == 512
        assert config.fusion_method == "weighted"


# =============================================================================
# Environment variable helpers
# =============================================================================


class TestGetEnvBool:
    """Test get_env_bool() helper."""

    def test_true_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("true", "True", "TRUE", "1", "yes", "Yes"):
            monkeypatch.setenv("TEST_BOOL", val)
            assert get_env_bool("TEST_BOOL") is True

    def test_false_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("false", "False", "0", "no"):
            monkeypatch.setenv("TEST_BOOL", val)
            assert get_env_bool("TEST_BOOL") is False

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_BOOL", raising=False)
        assert get_env_bool("TEST_BOOL") is False
        assert get_env_bool("TEST_BOOL", True) is True

    def test_empty_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOOL", "")
        assert get_env_bool("TEST_BOOL") is False
        assert get_env_bool("TEST_BOOL", True) is True


class TestGetEnvFloat:
    """Test get_env_float() helper."""

    def test_valid_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_FLOAT", "0.75")
        assert get_env_float("TEST_FLOAT", 0.5) == 0.75

    def test_integer_as_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_FLOAT", "3")
        assert get_env_float("TEST_FLOAT", 0.5) == 3.0

    def test_invalid_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_FLOAT", "not-a-number")
        assert get_env_float("TEST_FLOAT", 0.5) == 0.5

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_FLOAT", raising=False)
        assert get_env_float("TEST_FLOAT", 1.23) == 1.23


class TestGetEnvInt:
    """Test get_env_int() helper."""

    def test_valid_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "42")
        assert get_env_int("TEST_INT", 0) == 42

    def test_invalid_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "abc")
        assert get_env_int("TEST_INT", 10) == 10

    def test_float_string_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INT", "3.14")
        assert get_env_int("TEST_INT", 10) == 10

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_INT", raising=False)
        assert get_env_int("TEST_INT", 99) == 99


# =============================================================================
# search_config_from_env()
# =============================================================================


class TestSearchConfigFromEnv:
    """Test search_config_from_env() factory."""

    def test_default_config_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no env vars set, should return all defaults."""
        env_vars = [
            "NEXUS_CHUNK_SIZE",
            "NEXUS_CHUNK_STRATEGY",
            "NEXUS_ENTROPY_FILTERING",
            "NEXUS_ENTROPY_THRESHOLD",
            "NEXUS_ENTROPY_ALPHA",
            "NEXUS_FUSION_METHOD",
            "NEXUS_FUSION_ALPHA",
            "NEXUS_EMBEDDING_PROVIDER",
            "NEXUS_EMBEDDING_MODEL",
            "NEXUS_SEARCH_POOL_MIN",
            "NEXUS_SEARCH_POOL_MAX",
            "NEXUS_SEARCH_POOL_RECYCLE",
            "NEXUS_SEARCH_MODE",
            "NEXUS_CONTEXTUAL_CHUNKING",
            "NEXUS_ATTRIBUTE_BOOSTING",
        ]
        for var in env_vars:
            monkeypatch.delenv(var, raising=False)

        config = search_config_from_env()
        assert config.chunk_size == 1024
        assert config.fusion_method == "rrf"
        assert config.embedding_provider == "openai"

    def test_reads_chunk_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_CHUNK_SIZE", "512")
        config = search_config_from_env()
        assert config.chunk_size == 512

    def test_reads_entropy_filtering(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_ENTROPY_FILTERING", "true")
        config = search_config_from_env()
        assert config.entropy_filtering is True

    def test_reads_pool_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_SEARCH_POOL_MIN", "5")
        monkeypatch.setenv("NEXUS_SEARCH_POOL_MAX", "100")
        monkeypatch.setenv("NEXUS_SEARCH_POOL_RECYCLE", "3600")
        config = search_config_from_env()
        assert config.pool_min_size == 5
        assert config.pool_max_size == 100
        assert config.pool_recycle == 3600

    def test_config_is_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = search_config_from_env()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.chunk_size = 2048  # type: ignore[misc]
