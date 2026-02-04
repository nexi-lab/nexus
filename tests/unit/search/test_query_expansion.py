"""Tests for query expansion module (Issue #1174).

Tests cover:
- QueryExpansion and ExpansionResult dataclasses
- QueryExpansionConfig validation
- SignalDetector for smart triggering
- Prompt building and response parsing
- OpenRouterQueryExpander (mocked)
- CachedQueryExpander (mocked)
- QueryExpansionService integration
- Factory functions
"""

from __future__ import annotations

import pytest

from nexus.search.query_expansion import (
    CachedQueryExpander,
    ExpansionResult,
    ExpansionType,
    OpenRouterQueryExpander,
    QueryExpansion,
    QueryExpansionConfig,
    QueryExpansionService,
    SignalDetector,
    create_query_expander,
    get_expansion_config_from_env,
)


class TestQueryExpansion:
    """Tests for QueryExpansion dataclass."""

    def test_query_expansion_creation(self):
        """Test basic creation."""
        expansion = QueryExpansion(
            expansion_type=ExpansionType.LEX,
            text="kubernetes pod restart",
            weight=1.0,
        )
        assert expansion.expansion_type == ExpansionType.LEX
        assert expansion.text == "kubernetes pod restart"
        assert expansion.weight == 1.0

    def test_query_expansion_to_dict(self):
        """Test to_dict conversion."""
        expansion = QueryExpansion(
            expansion_type=ExpansionType.VEC,
            text="How do I fix a failing deployment?",
            weight=0.8,
        )
        d = expansion.to_dict()
        assert d["type"] == "vec"
        assert d["text"] == "How do I fix a failing deployment?"
        assert d["weight"] == 0.8

    def test_expansion_types(self):
        """Test all expansion types."""
        assert ExpansionType.LEX == "lex"
        assert ExpansionType.VEC == "vec"
        assert ExpansionType.HYDE == "hyde"


class TestExpansionResult:
    """Tests for ExpansionResult dataclass."""

    def test_expansion_result_creation(self):
        """Test basic creation."""
        expansions = [
            QueryExpansion(ExpansionType.LEX, "k8s deployment"),
            QueryExpansion(ExpansionType.VEC, "How to deploy?"),
            QueryExpansion(ExpansionType.HYDE, "Deployments in Kubernetes..."),
        ]
        result = ExpansionResult(
            original_query="kubernetes deployment",
            expansions=expansions,
            was_expanded=True,
            model_used="deepseek/deepseek-chat",
            latency_ms=150.5,
        )
        assert result.original_query == "kubernetes deployment"
        assert len(result.expansions) == 3
        assert result.was_expanded is True
        assert result.model_used == "deepseek/deepseek-chat"
        assert result.latency_ms == 150.5

    def test_get_lex_variants(self):
        """Test getting lexical variants."""
        expansions = [
            QueryExpansion(ExpansionType.LEX, "lex1"),
            QueryExpansion(ExpansionType.LEX, "lex2"),
            QueryExpansion(ExpansionType.VEC, "vec1"),
        ]
        result = ExpansionResult("query", expansions)
        lex = result.get_lex_variants()
        assert lex == ["lex1", "lex2"]

    def test_get_vec_variants(self):
        """Test getting vector variants."""
        expansions = [
            QueryExpansion(ExpansionType.LEX, "lex1"),
            QueryExpansion(ExpansionType.VEC, "vec1"),
            QueryExpansion(ExpansionType.VEC, "vec2"),
        ]
        result = ExpansionResult("query", expansions)
        vec = result.get_vec_variants()
        assert vec == ["vec1", "vec2"]

    def test_get_hyde_passages(self):
        """Test getting HyDE passages."""
        expansions = [
            QueryExpansion(ExpansionType.HYDE, "hyde1"),
            QueryExpansion(ExpansionType.VEC, "vec1"),
            QueryExpansion(ExpansionType.HYDE, "hyde2"),
        ]
        result = ExpansionResult("query", expansions)
        hyde = result.get_hyde_passages()
        assert hyde == ["hyde1", "hyde2"]

    def test_get_all_queries_with_original(self):
        """Test getting all queries including original."""
        expansions = [
            QueryExpansion(ExpansionType.LEX, "exp1"),
            QueryExpansion(ExpansionType.VEC, "exp2"),
        ]
        result = ExpansionResult("original", expansions)
        all_queries = result.get_all_queries(include_original=True)
        assert all_queries == ["original", "exp1", "exp2"]

    def test_get_all_queries_without_original(self):
        """Test getting all queries excluding original."""
        expansions = [
            QueryExpansion(ExpansionType.LEX, "exp1"),
            QueryExpansion(ExpansionType.VEC, "exp2"),
        ]
        result = ExpansionResult("original", expansions)
        all_queries = result.get_all_queries(include_original=False)
        assert all_queries == ["exp1", "exp2"]

    def test_to_dict(self):
        """Test to_dict conversion."""
        expansions = [QueryExpansion(ExpansionType.LEX, "lex1")]
        result = ExpansionResult(
            original_query="query",
            expansions=expansions,
            was_expanded=True,
            skip_reason=None,
            model_used="model",
            latency_ms=100.0,
            cache_hit=False,
        )
        d = result.to_dict()
        assert d["original_query"] == "query"
        assert len(d["expansions"]) == 1
        assert d["was_expanded"] is True
        assert d["model_used"] == "model"
        assert d["latency_ms"] == 100.0
        assert d["cache_hit"] is False


class TestQueryExpansionConfig:
    """Tests for QueryExpansionConfig."""

    def test_default_config(self):
        """Test default configuration."""
        config = QueryExpansionConfig()
        assert config.enabled is True
        assert config.provider == "openrouter"
        assert config.model == "deepseek/deepseek-chat"
        assert config.max_lex_variants == 2
        assert config.max_vec_variants == 2
        assert config.max_hyde_passages == 2
        assert config.strong_signal_threshold == 0.85
        assert config.cache_enabled is True
        assert config.timeout == 5.0

    def test_custom_config(self):
        """Test custom configuration."""
        config = QueryExpansionConfig(
            enabled=False,
            model="openai/gpt-4o-mini",
            max_lex_variants=3,
            strong_signal_threshold=0.9,
        )
        assert config.enabled is False
        assert config.model == "openai/gpt-4o-mini"
        assert config.max_lex_variants == 3
        assert config.strong_signal_threshold == 0.9

    def test_invalid_threshold_raises(self):
        """Test that invalid threshold raises error."""
        with pytest.raises(ValueError, match="strong_signal_threshold"):
            QueryExpansionConfig(strong_signal_threshold=1.5)

    def test_invalid_separation_raises(self):
        """Test that invalid separation threshold raises error."""
        with pytest.raises(ValueError, match="signal_separation_threshold"):
            QueryExpansionConfig(signal_separation_threshold=-0.1)

    def test_invalid_variant_count_raises(self):
        """Test that negative variant count raises error."""
        with pytest.raises(ValueError, match="non-negative"):
            QueryExpansionConfig(max_lex_variants=-1)


class TestSignalDetector:
    """Tests for SignalDetector."""

    def test_strong_signal_detected(self):
        """Test detection of strong signal."""
        detector = SignalDetector(
            strong_signal_threshold=0.85,
            separation_threshold=0.10,
        )
        results = [
            {"score": 0.95},  # High score
            {"score": 0.75},  # Good separation (0.20)
        ]
        assert detector.has_strong_signal(results) is True
        assert detector.should_expand(results) is False

    def test_weak_signal_low_score(self):
        """Test weak signal due to low score."""
        detector = SignalDetector(
            strong_signal_threshold=0.85,
            separation_threshold=0.10,
        )
        results = [
            {"score": 0.70},  # Below threshold
            {"score": 0.60},
        ]
        assert detector.has_strong_signal(results) is False
        assert detector.should_expand(results) is True

    def test_weak_signal_poor_separation(self):
        """Test weak signal due to poor separation."""
        detector = SignalDetector(
            strong_signal_threshold=0.85,
            separation_threshold=0.10,
        )
        results = [
            {"score": 0.90},  # High score
            {"score": 0.88},  # Poor separation (0.02)
        ]
        assert detector.has_strong_signal(results) is False
        assert detector.should_expand(results) is True

    def test_empty_results(self):
        """Test with empty results."""
        detector = SignalDetector()
        assert detector.has_strong_signal([]) is False
        assert detector.should_expand([]) is True

    def test_single_result_strong(self):
        """Test with single strong result."""
        detector = SignalDetector(
            strong_signal_threshold=0.85,
            separation_threshold=0.10,
        )
        results = [{"score": 0.95}]  # Strong, separation from 0 is 0.95
        assert detector.has_strong_signal(results) is True

    def test_single_result_weak(self):
        """Test with single weak result."""
        detector = SignalDetector(
            strong_signal_threshold=0.85,
            separation_threshold=0.10,
        )
        results = [{"score": 0.70}]  # Weak
        assert detector.has_strong_signal(results) is False


class TestOpenRouterQueryExpander:
    """Tests for OpenRouterQueryExpander."""

    def test_build_prompt(self):
        """Test prompt building."""
        config = QueryExpansionConfig(
            max_lex_variants=2,
            max_vec_variants=2,
            max_hyde_passages=2,
        )
        expander = OpenRouterQueryExpander(config=config, api_key="test")
        prompt = expander._build_prompt("kubernetes deployment", None)

        assert "kubernetes deployment" in prompt
        assert "lex:" in prompt
        assert "vec:" in prompt
        assert "hyde:" in prompt
        assert "6 lines" in prompt  # 2+2+2

    def test_build_prompt_with_context(self):
        """Test prompt building with context."""
        config = QueryExpansionConfig()
        expander = OpenRouterQueryExpander(config=config, api_key="test")
        prompt = expander._build_prompt("query", "DevOps documentation")

        assert "query" in prompt
        assert "DevOps documentation" in prompt

    def test_parse_response_valid(self):
        """Test parsing valid LLM response."""
        config = QueryExpansionConfig()
        expander = OpenRouterQueryExpander(config=config, api_key="test")

        response = """lex: k8s pod restart
lex: kubernetes container debug
vec: How do I troubleshoot Kubernetes pods?
vec: What causes pod crashes in K8s?
hyde: When a pod crashes, check the logs using kubectl logs.
hyde: Pod restarts are often caused by OOMKilled or CrashLoopBackOff."""

        expansions = expander._parse_response(response)

        assert len(expansions) == 6
        lex = [e for e in expansions if e.expansion_type == ExpansionType.LEX]
        vec = [e for e in expansions if e.expansion_type == ExpansionType.VEC]
        hyde = [e for e in expansions if e.expansion_type == ExpansionType.HYDE]

        assert len(lex) == 2
        assert len(vec) == 2
        assert len(hyde) == 2

        assert lex[0].text == "k8s pod restart"
        assert vec[0].text == "How do I troubleshoot Kubernetes pods?"

    def test_parse_response_case_insensitive(self):
        """Test parsing handles case variations."""
        config = QueryExpansionConfig(max_lex_variants=1, max_vec_variants=1, max_hyde_passages=1)
        expander = OpenRouterQueryExpander(config=config, api_key="test")

        response = """LEX: uppercase
Vec: mixed case
HYDE: all caps"""

        expansions = expander._parse_response(response)
        assert len(expansions) == 3

    def test_parse_response_respects_limits(self):
        """Test parsing respects configured limits."""
        config = QueryExpansionConfig(max_lex_variants=1, max_vec_variants=1, max_hyde_passages=0)
        expander = OpenRouterQueryExpander(config=config, api_key="test")

        response = """lex: first
lex: second (should be ignored)
vec: question
hyde: passage (should be ignored)"""

        expansions = expander._parse_response(response)
        lex = [e for e in expansions if e.expansion_type == ExpansionType.LEX]
        hyde = [e for e in expansions if e.expansion_type == ExpansionType.HYDE]

        assert len(lex) == 1
        assert len(hyde) == 0

    def test_parse_response_handles_empty(self):
        """Test parsing handles empty response."""
        config = QueryExpansionConfig()
        expander = OpenRouterQueryExpander(config=config, api_key="test")

        expansions = expander._parse_response("")
        assert expansions == []

    def test_parse_response_handles_invalid_lines(self):
        """Test parsing skips invalid lines."""
        config = QueryExpansionConfig()
        expander = OpenRouterQueryExpander(config=config, api_key="test")

        response = """This is not a valid line
lex: valid expansion
another invalid line
vec: another valid one"""

        expansions = expander._parse_response(response)
        assert len(expansions) == 2

    def test_missing_api_key_raises(self):
        """Test that missing API key raises error on client access."""
        import os

        # Temporarily unset the env var
        old_key = os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            expander = OpenRouterQueryExpander(config=QueryExpansionConfig())
            with pytest.raises(ValueError, match="API key required"):
                expander._get_client()
        finally:
            if old_key:
                os.environ["OPENROUTER_API_KEY"] = old_key


class TestQueryExpansionService:
    """Tests for QueryExpansionService."""

    @pytest.fixture
    def mock_expander(self):
        """Create a mock expander."""

        class MockExpander(OpenRouterQueryExpander):
            def __init__(self):
                self.config = QueryExpansionConfig()
                self._expand_called = False

            async def expand(self, query, context=None):
                self._expand_called = True
                return [
                    QueryExpansion(ExpansionType.LEX, f"lex: {query}"),
                    QueryExpansion(ExpansionType.VEC, f"vec: {query}?"),
                ]

            async def close(self):
                pass

        return MockExpander()

    @pytest.mark.asyncio
    async def test_expand_if_needed_weak_signal(self, mock_expander):
        """Test expansion happens on weak signal."""
        service = QueryExpansionService(mock_expander)

        weak_results = [{"score": 0.5}, {"score": 0.4}]
        result = await service.expand_if_needed(
            "test query",
            initial_results=weak_results,
        )

        assert result.was_expanded is True
        assert len(result.expansions) == 2
        assert mock_expander._expand_called is True

    @pytest.mark.asyncio
    async def test_expand_if_needed_strong_signal(self, mock_expander):
        """Test expansion skipped on strong signal."""
        service = QueryExpansionService(mock_expander)

        strong_results = [{"score": 0.95}, {"score": 0.75}]
        result = await service.expand_if_needed(
            "test query",
            initial_results=strong_results,
        )

        assert result.was_expanded is False
        assert result.skip_reason == "strong_bm25_signal"
        assert mock_expander._expand_called is False

    @pytest.mark.asyncio
    async def test_expand_if_needed_force(self, mock_expander):
        """Test force expansion ignores signal."""
        service = QueryExpansionService(mock_expander)

        strong_results = [{"score": 0.95}, {"score": 0.75}]
        result = await service.expand_if_needed(
            "test query",
            initial_results=strong_results,
            force=True,
        )

        assert result.was_expanded is True
        assert mock_expander._expand_called is True

    @pytest.mark.asyncio
    async def test_expand_if_needed_disabled(self, mock_expander):
        """Test expansion when disabled."""
        config = QueryExpansionConfig(enabled=False)
        service = QueryExpansionService(mock_expander, config=config)

        result = await service.expand_if_needed("test query")

        assert result.was_expanded is False
        assert result.skip_reason == "expansion_disabled"

    @pytest.mark.asyncio
    async def test_expand_if_needed_no_initial_results(self, mock_expander):
        """Test expansion when no initial results provided."""
        service = QueryExpansionService(mock_expander)

        result = await service.expand_if_needed("test query")

        assert result.was_expanded is True
        assert mock_expander._expand_called is True


class TestFactoryFunctions:
    """Tests for factory functions."""

    def test_create_query_expander_openrouter(self):
        """Test creating OpenRouter expander."""
        import os

        os.environ["OPENROUTER_API_KEY"] = "test-key"
        try:
            expander = create_query_expander(
                provider="openrouter",
                model="deepseek/deepseek-chat",
            )
            assert isinstance(expander, OpenRouterQueryExpander)
            assert expander.config.model == "deepseek/deepseek-chat"
        finally:
            del os.environ["OPENROUTER_API_KEY"]

    def test_create_query_expander_invalid_provider(self):
        """Test that invalid provider raises error."""
        with pytest.raises(ValueError, match="Unsupported provider"):
            create_query_expander(provider="invalid")

    def test_get_expansion_config_from_env(self):
        """Test loading config from environment."""
        import os

        os.environ["NEXUS_QUERY_EXPANSION_ENABLED"] = "false"
        os.environ["NEXUS_QUERY_EXPANSION_MODEL"] = "custom-model"
        os.environ["NEXUS_QUERY_EXPANSION_STRONG_SIGNAL"] = "0.9"

        try:
            config = get_expansion_config_from_env()
            assert config.enabled is False
            assert config.model == "custom-model"
            assert config.strong_signal_threshold == 0.9
        finally:
            del os.environ["NEXUS_QUERY_EXPANSION_ENABLED"]
            del os.environ["NEXUS_QUERY_EXPANSION_MODEL"]
            del os.environ["NEXUS_QUERY_EXPANSION_STRONG_SIGNAL"]

    def test_get_expansion_config_defaults(self):
        """Test config defaults when env vars not set."""
        config = get_expansion_config_from_env()
        assert config.enabled is True
        assert config.provider == "openrouter"
        assert config.model == "deepseek/deepseek-chat"


class TestCachedQueryExpander:
    """Tests for CachedQueryExpander."""

    @pytest.fixture
    def mock_cache(self):
        """Create a mock Redis cache."""

        class MockRedis:
            def __init__(self):
                self._store = {}

            async def get(self, key):
                return self._store.get(key)

            async def setex(self, key, ttl, value):
                self._store[key] = value

        return MockRedis()

    @pytest.fixture
    def mock_base_expander(self):
        """Create a mock base expander."""

        class MockExpander(OpenRouterQueryExpander):
            def __init__(self):
                self.config = QueryExpansionConfig()
                self.expand_count = 0

            async def expand(self, query, context=None):
                self.expand_count += 1
                return [QueryExpansion(ExpansionType.LEX, f"expanded: {query}")]

            async def close(self):
                pass

        return MockExpander()

    @pytest.mark.asyncio
    async def test_cache_miss_calls_expander(self, mock_cache, mock_base_expander):
        """Test that cache miss calls underlying expander."""
        cached = CachedQueryExpander(
            expander=mock_base_expander,
            cache=mock_cache,
            ttl=3600,
        )

        result = await cached.expand("test query")

        assert len(result) == 1
        assert mock_base_expander.expand_count == 1

    @pytest.mark.asyncio
    async def test_cache_hit_skips_expander(self, mock_cache, mock_base_expander):
        """Test that cache hit skips underlying expander."""
        cached = CachedQueryExpander(
            expander=mock_base_expander,
            cache=mock_cache,
            ttl=3600,
        )

        # First call - cache miss
        await cached.expand("test query")
        assert mock_base_expander.expand_count == 1

        # Second call - cache hit
        result = await cached.expand("test query")
        assert mock_base_expander.expand_count == 1  # Still 1, not called again
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_different_queries_different_cache_keys(self, mock_cache, mock_base_expander):
        """Test that different queries use different cache keys."""
        cached = CachedQueryExpander(
            expander=mock_base_expander,
            cache=mock_cache,
            ttl=3600,
        )

        await cached.expand("query 1")
        await cached.expand("query 2")

        assert mock_base_expander.expand_count == 2

    @pytest.mark.asyncio
    async def test_context_affects_cache_key(self, mock_cache, mock_base_expander):
        """Test that context is included in cache key."""
        cached = CachedQueryExpander(
            expander=mock_base_expander,
            cache=mock_cache,
            ttl=3600,
        )

        await cached.expand("query", context="context1")
        await cached.expand("query", context="context2")

        assert mock_base_expander.expand_count == 2
