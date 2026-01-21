"""Unit tests for attribute-based ranking (Issue #1092)."""

import os

from nexus.search.ranking import (
    AttributeWeights,
    RankingConfig,
    apply_attribute_boosting,
    check_exact_match,
    check_prefix_match,
    detect_matched_field,
    get_ranking_config_from_env,
)


class TestAttributeWeights:
    """Tests for AttributeWeights dataclass."""

    def test_default_weights(self):
        """Test default weight values."""
        weights = AttributeWeights()

        assert weights.filename == 3.0
        assert weights.title == 2.5
        assert weights.path == 2.0
        assert weights.tags == 2.0
        assert weights.description == 1.5
        assert weights.content == 1.0
        assert weights.exact_match_boost == 1.5

    def test_custom_weights(self):
        """Test custom weight values."""
        weights = AttributeWeights(filename=5.0, content=0.5)

        assert weights.filename == 5.0
        assert weights.content == 0.5
        # Unchanged values
        assert weights.title == 2.5

    def test_get_weight_known_field(self):
        """Test get_weight for known fields."""
        weights = AttributeWeights()

        assert weights.get_weight("filename") == 3.0
        assert weights.get_weight("title") == 2.5
        assert weights.get_weight("content") == 1.0

    def test_get_weight_unknown_field(self):
        """Test get_weight returns 1.0 for unknown fields."""
        weights = AttributeWeights()

        assert weights.get_weight("unknown_field") == 1.0
        assert weights.get_weight("") == 1.0


class TestRankingConfig:
    """Tests for RankingConfig dataclass."""

    def test_default_config(self):
        """Test default configuration."""
        config = RankingConfig()

        assert config.enable_attribute_boosting is True
        assert config.enable_exactness_boost is True
        assert isinstance(config.attribute_weights, AttributeWeights)

    def test_custom_config(self):
        """Test custom configuration."""
        config = RankingConfig(
            enable_attribute_boosting=False,
            enable_exactness_boost=False,
        )

        assert config.enable_attribute_boosting is False
        assert config.enable_exactness_boost is False


class TestGetRankingConfigFromEnv:
    """Tests for environment variable configuration."""

    def test_default_values(self):
        """Test default values when env vars not set."""
        # Clear any existing env vars
        env_vars = [
            "NEXUS_SEARCH_WEIGHT_FILENAME",
            "NEXUS_SEARCH_WEIGHT_TITLE",
            "NEXUS_SEARCH_ATTRIBUTE_BOOST",
        ]
        original_values = {k: os.environ.pop(k, None) for k in env_vars}

        try:
            config = get_ranking_config_from_env()

            assert config.attribute_weights.filename == 3.0
            assert config.attribute_weights.title == 2.5
            assert config.enable_attribute_boosting is True
        finally:
            # Restore env vars
            for k, v in original_values.items():
                if v is not None:
                    os.environ[k] = v

    def test_custom_env_values(self):
        """Test custom values from env vars."""
        original_values = {}
        try:
            # Set custom env vars
            original_values["NEXUS_SEARCH_WEIGHT_FILENAME"] = os.environ.get(
                "NEXUS_SEARCH_WEIGHT_FILENAME"
            )
            original_values["NEXUS_SEARCH_ATTRIBUTE_BOOST"] = os.environ.get(
                "NEXUS_SEARCH_ATTRIBUTE_BOOST"
            )

            os.environ["NEXUS_SEARCH_WEIGHT_FILENAME"] = "5.0"
            os.environ["NEXUS_SEARCH_ATTRIBUTE_BOOST"] = "false"

            config = get_ranking_config_from_env()

            assert config.attribute_weights.filename == 5.0
            assert config.enable_attribute_boosting is False
        finally:
            # Restore env vars
            for k, v in original_values.items():
                if v is not None:
                    os.environ[k] = v
                elif k in os.environ:
                    del os.environ[k]


class TestDetectMatchedField:
    """Tests for detect_matched_field function."""

    def test_filename_match(self):
        """Test detection of filename matches."""
        result = detect_matched_field(
            query="auth",
            path="/src/auth.py",
            content="some content",
        )
        assert result == "filename"

    def test_filename_without_extension_match(self):
        """Test detection of filename match without extension."""
        result = detect_matched_field(
            query="authentication",
            path="/src/authentication.py",
            content="some content",
        )
        assert result == "filename"

    def test_path_match(self):
        """Test detection of path matches."""
        result = detect_matched_field(
            query="services",
            path="/src/services/handler.py",
            content="some content",
        )
        assert result == "path"

    def test_title_match(self):
        """Test detection of title matches."""
        result = detect_matched_field(
            query="authentication",
            path="/docs/readme.md",
            content="some content",
            title="Authentication Guide",
        )
        assert result == "title"

    def test_tags_match(self):
        """Test detection of tag matches."""
        result = detect_matched_field(
            query="security",
            path="/docs/readme.md",
            content="some content",
            tags=["security", "auth"],
        )
        assert result == "tags"

    def test_description_match(self):
        """Test detection of description matches."""
        result = detect_matched_field(
            query="authentication",
            path="/src/handler.py",
            content="some content",
            description="Handles authentication flow",
        )
        assert result == "description"

    def test_content_fallback(self):
        """Test fallback to content when no other field matches."""
        result = detect_matched_field(
            query="foobar",
            path="/src/handler.py",
            content="This is foobar content",
        )
        assert result == "content"

    def test_multi_term_query_filename(self):
        """Test multi-term query matching filename."""
        result = detect_matched_field(
            query="user auth",
            path="/src/user_auth.py",
            content="some content",
        )
        assert result == "filename"


class TestCheckExactMatch:
    """Tests for check_exact_match function."""

    def test_exact_match_found(self):
        """Test exact phrase match."""
        assert check_exact_match("authentication", "User authentication flow")
        assert check_exact_match("auth", "auth system")

    def test_exact_match_word_boundary(self):
        """Test exact match respects word boundaries."""
        # "auth" should not match inside "authentication"
        assert not check_exact_match("auth", "authentication system")

    def test_exact_match_case_insensitive(self):
        """Test exact match is case insensitive."""
        assert check_exact_match("AUTH", "auth system")
        assert check_exact_match("auth", "AUTH system")

    def test_exact_match_empty_input(self):
        """Test exact match with empty inputs."""
        assert not check_exact_match("", "some text")
        assert not check_exact_match("query", "")
        assert not check_exact_match("", "")


class TestCheckPrefixMatch:
    """Tests for check_prefix_match function."""

    def test_prefix_match_found(self):
        """Test prefix match."""
        assert check_prefix_match("auth", "authentication system")

    def test_prefix_match_not_found(self):
        """Test prefix match not found."""
        assert not check_prefix_match("xyz", "authentication system")

    def test_prefix_match_empty_input(self):
        """Test prefix match with empty inputs."""
        assert not check_prefix_match("", "some text")
        assert not check_prefix_match("query", "")


class TestApplyAttributeBoosting:
    """Tests for apply_attribute_boosting function."""

    def test_boost_filename_match(self):
        """Test boosting for filename match."""
        results = [
            {"path": "/src/handler.py", "score": 0.8, "chunk_text": "handler code"},
            {"path": "/src/auth.py", "score": 0.7, "chunk_text": "authentication"},
        ]

        boosted = apply_attribute_boosting(results, "auth")

        # auth.py should be ranked higher due to filename match
        assert boosted[0]["path"] == "/src/auth.py"
        assert boosted[0]["matched_field"] == "filename"
        assert boosted[0]["attribute_boost"] > 1.0

    def test_boost_with_config(self):
        """Test boosting with custom config."""
        results = [
            {"path": "/src/handler.py", "score": 0.8, "chunk_text": "some code"},
            {"path": "/src/auth.py", "score": 0.7, "chunk_text": "some code"},
        ]

        # Disable exactness boost to test only filename weight
        config = RankingConfig(
            attribute_weights=AttributeWeights(filename=10.0, content=1.0),
            enable_exactness_boost=False,
        )
        boosted = apply_attribute_boosting(results, "auth", config)

        # With 10x filename boost, auth.py should definitely be first
        assert boosted[0]["path"] == "/src/auth.py"
        assert boosted[0]["score"] == 0.7 * 10.0  # Original score * filename weight

    def test_boost_disabled(self):
        """Test boosting can be disabled."""
        results = [
            {"path": "/src/handler.py", "score": 0.8, "chunk_text": "some code"},
            {"path": "/src/auth.py", "score": 0.7, "chunk_text": "some code"},
        ]

        config = RankingConfig(enable_attribute_boosting=False)
        boosted = apply_attribute_boosting(results, "auth", config)

        # Order should be unchanged when boosting disabled
        assert boosted[0]["path"] == "/src/handler.py"
        assert boosted[1]["path"] == "/src/auth.py"

    def test_exact_match_bonus(self):
        """Test exact match bonus is applied."""
        results = [
            {"path": "/src/handler.py", "score": 0.8, "chunk_text": "authentication system"},
            {"path": "/src/auth.py", "score": 0.7, "chunk_text": "auth module"},
        ]

        boosted = apply_attribute_boosting(results, "auth")

        # auth.py has exact "auth" in filename, should get boost
        auth_result = next(r for r in boosted if r["path"] == "/src/auth.py")
        assert auth_result.get("is_exact_match") is True

    def test_empty_results(self):
        """Test handling of empty results."""
        results: list[dict] = []
        boosted = apply_attribute_boosting(results, "query")
        assert boosted == []

    def test_empty_query(self):
        """Test handling of empty query."""
        results = [
            {"path": "/src/handler.py", "score": 0.8, "chunk_text": "some code"},
        ]
        boosted = apply_attribute_boosting(results, "")
        assert boosted == results

    def test_preserves_original_score(self):
        """Test that original score is preserved."""
        results = [
            {"path": "/src/auth.py", "score": 0.7, "chunk_text": "some code"},
        ]

        boosted = apply_attribute_boosting(results, "auth")

        assert boosted[0]["original_score"] == 0.7
        assert boosted[0]["score"] != 0.7  # Score should be boosted

    def test_results_sorted_by_boosted_score(self):
        """Test that results are sorted by boosted score."""
        results = [
            {"path": "/src/handler.py", "score": 0.9, "chunk_text": "handler"},
            {"path": "/src/utils.py", "score": 0.8, "chunk_text": "utils"},
            {
                "path": "/src/auth.py",
                "score": 0.3,
                "chunk_text": "auth",
            },  # Low score but filename match
        ]

        boosted = apply_attribute_boosting(results, "auth")

        # auth.py should be boosted to top despite low original score
        # 0.3 * 3.0 (filename) * 1.5 (exact) = 1.35 vs 0.9 * 1.0 = 0.9
        assert boosted[0]["path"] == "/src/auth.py"
