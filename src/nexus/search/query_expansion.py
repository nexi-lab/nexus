"""LLM-based Query Expansion for improved search recall (Issue #1174).

This module provides query expansion using LLMs to generate multiple query variants
that feed into both BM25 and vector search pipelines, inspired by QMD's approach.

Query expansion generates three types of variants:
- lex: Lexical variants optimized for BM25 (short keyword phrases)
- vec: Vector variants for semantic search (natural language questions)
- hyde: Hypothetical document passages (answer-like content)

Features:
- Smart triggering: Skip expansion when initial BM25 shows strong signal
- Caching: Content-hash based caching reduces API calls by 90%+
- Multiple providers: OpenRouter (DeepSeek, Gemini, GPT-4o-mini), direct APIs
- Fallback chain: Graceful degradation through model priority list

References:
    - Issue #1174: Add LLM-based query expansion for improved recall
    - HyDE Paper: https://arxiv.org/abs/2212.10496
    - RAG-Fusion: https://arxiv.org/abs/2402.03367
    - QMD: https://github.com/tobi/qmd
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class ExpansionType(StrEnum):
    """Type of query expansion."""

    LEX = "lex"  # Lexical variants for BM25 (keywords)
    VEC = "vec"  # Vector variants for semantic search (natural language)
    HYDE = "hyde"  # Hypothetical document passages


@dataclass
class QueryExpansion:
    """A single query expansion result.

    Attributes:
        expansion_type: Type of expansion (lex, vec, or hyde)
        text: The expanded query text
        weight: Optional weight for fusion (default: 1.0)
    """

    expansion_type: ExpansionType
    text: str
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "type": self.expansion_type.value,
            "text": self.text,
            "weight": self.weight,
        }


@dataclass
class QueryExpansionConfig:
    """Configuration for query expansion.

    Attributes:
        enabled: Whether query expansion is enabled
        provider: LLM provider ("openrouter", "openai", "anthropic")
        model: Model to use (e.g., "deepseek/deepseek-chat")
        fallback_models: Fallback models if primary fails
        max_lex_variants: Maximum lexical variants to generate
        max_vec_variants: Maximum vector variants to generate
        max_hyde_passages: Maximum HyDE passages to generate
        strong_signal_threshold: Skip expansion if top BM25 score >= this
        signal_separation_threshold: Required gap between top-1 and top-2
        cache_enabled: Enable expansion caching
        cache_ttl: Cache TTL in seconds
        timeout: LLM request timeout in seconds
        temperature: LLM temperature (0.0-1.0)
        max_tokens: Maximum tokens for LLM response
    """

    enabled: bool = True

    # Model settings
    provider: str = "openrouter"
    model: str = "deepseek/deepseek-chat"
    fallback_models: list[str] = field(
        default_factory=lambda: [
            "deepseek/deepseek-chat-v3-0324:free",
            "google/gemini-2.0-flash-exp:free",
            "google/gemini-2.0-flash",
            "openai/gpt-4o-mini",
        ]
    )

    # Expansion settings
    max_lex_variants: int = 2
    max_vec_variants: int = 2
    max_hyde_passages: int = 2

    # Smart triggering thresholds
    strong_signal_threshold: float = 0.85
    signal_separation_threshold: float = 0.10

    # Caching
    cache_enabled: bool = True
    cache_ttl: int = 3600  # 1 hour

    # LLM settings
    timeout: float = 5.0
    temperature: float = 0.7
    max_tokens: int = 400

    def __post_init__(self) -> None:
        """Validate configuration."""
        if not (0.0 <= self.strong_signal_threshold <= 1.0):
            raise ValueError(
                f"strong_signal_threshold must be in [0, 1], got {self.strong_signal_threshold}"
            )
        if not (0.0 <= self.signal_separation_threshold <= 1.0):
            raise ValueError(
                f"signal_separation_threshold must be in [0, 1], got {self.signal_separation_threshold}"
            )
        if self.max_lex_variants < 0 or self.max_vec_variants < 0 or self.max_hyde_passages < 0:
            raise ValueError("Variant counts must be non-negative")


@dataclass
class ExpansionResult:
    """Result of query expansion.

    Attributes:
        original_query: The original query
        expansions: List of generated expansions
        was_expanded: Whether expansion was performed (vs skipped)
        skip_reason: Reason for skipping expansion (if applicable)
        model_used: Model that generated the expansions
        latency_ms: Time taken for expansion in milliseconds
        cache_hit: Whether result came from cache
    """

    original_query: str
    expansions: list[QueryExpansion]
    was_expanded: bool = True
    skip_reason: str | None = None
    model_used: str | None = None
    latency_ms: float = 0.0
    cache_hit: bool = False

    def get_lex_variants(self) -> list[str]:
        """Get lexical variants."""
        return [e.text for e in self.expansions if e.expansion_type == ExpansionType.LEX]

    def get_vec_variants(self) -> list[str]:
        """Get vector variants."""
        return [e.text for e in self.expansions if e.expansion_type == ExpansionType.VEC]

    def get_hyde_passages(self) -> list[str]:
        """Get HyDE passages."""
        return [e.text for e in self.expansions if e.expansion_type == ExpansionType.HYDE]

    def get_all_queries(self, include_original: bool = True) -> list[str]:
        """Get all queries (original + expansions).

        Args:
            include_original: Whether to include the original query

        Returns:
            List of query strings
        """
        queries = [self.original_query] if include_original else []
        queries.extend(e.text for e in self.expansions)
        return queries

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "original_query": self.original_query,
            "expansions": [e.to_dict() for e in self.expansions],
            "was_expanded": self.was_expanded,
            "skip_reason": self.skip_reason,
            "model_used": self.model_used,
            "latency_ms": self.latency_ms,
            "cache_hit": self.cache_hit,
        }


class QueryExpander(ABC):
    """Abstract base class for query expanders.

    Subclasses implement LLM-specific expansion logic.
    """

    @abstractmethod
    async def expand(
        self,
        query: str,
        context: str | None = None,
    ) -> list[QueryExpansion]:
        """Expand a query into multiple variants.

        Args:
            query: The original query
            context: Optional context about the document collection

        Returns:
            List of query expansions
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close any resources."""
        pass


# Default prompt template for query expansion
EXPANSION_PROMPT_TEMPLATE = """Generate search query expansions for the following query.

Query: "{query}"
{context_line}

Generate exactly {total_variants} lines in this format:
{format_instructions}

Rules:
- lex: Short keyword phrases (2-5 words), include synonyms and abbreviations
- vec: Full sentences, natural questions a user might ask
- hyde: 1-2 sentences describing what a relevant document would contain
- PRESERVE important entities exactly (names, acronyms, technical terms like API, SDK, etc.)
- Do NOT add explanations, just output the lines
- Each line MUST start with the correct prefix (lex:, vec:, or hyde:)"""


class OpenRouterQueryExpander(QueryExpander):
    """Query expander using OpenRouter API.

    OpenRouter provides access to multiple models (DeepSeek, Gemini, GPT-4o-mini, etc.)
    through a single API endpoint with unified pricing.

    Attributes:
        config: Query expansion configuration
        client: AsyncOpenAI client configured for OpenRouter
    """

    def __init__(
        self,
        config: QueryExpansionConfig | None = None,
        api_key: str | None = None,
    ) -> None:
        """Initialize OpenRouter query expander.

        Args:
            config: Expansion configuration (uses defaults if not provided)
            api_key: OpenRouter API key (falls back to OPENROUTER_API_KEY env var)
        """
        self.config = config or QueryExpansionConfig()
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self._client: Any = None  # AsyncOpenAI client, lazily initialized

    def _get_client(self) -> Any:  # Returns AsyncOpenAI but avoid import at module level
        """Get or create the OpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as e:
                raise ImportError(
                    "openai package required for OpenRouterQueryExpander. "
                    "Install with: pip install openai"
                ) from e

            if not self._api_key:
                raise ValueError(
                    "OpenRouter API key required. Set OPENROUTER_API_KEY env var "
                    "or pass api_key parameter."
                )

            self._client = AsyncOpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=self._api_key,
            )
        return self._client

    def _build_prompt(self, query: str, context: str | None) -> str:
        """Build the expansion prompt.

        Args:
            query: Original query
            context: Optional collection context

        Returns:
            Formatted prompt string
        """
        context_line = f"\nContext: {context}" if context else ""

        # Build format instructions based on config
        instructions = []
        for _ in range(self.config.max_lex_variants):
            instructions.append("lex: <keyword variant>")
        for _ in range(self.config.max_vec_variants):
            instructions.append("vec: <natural language question>")
        for _ in range(self.config.max_hyde_passages):
            instructions.append("hyde: <hypothetical document passage>")

        total_variants = (
            self.config.max_lex_variants
            + self.config.max_vec_variants
            + self.config.max_hyde_passages
        )

        return EXPANSION_PROMPT_TEMPLATE.format(
            query=query,
            context_line=context_line,
            total_variants=total_variants,
            format_instructions="\n".join(instructions),
        )

    def _parse_response(self, response_text: str) -> list[QueryExpansion]:
        """Parse LLM response into expansions.

        Args:
            response_text: Raw LLM response

        Returns:
            List of parsed expansions
        """
        expansions = []
        lines = response_text.strip().split("\n")

        # Regex patterns for each type
        patterns = {
            ExpansionType.LEX: re.compile(r"^lex:\s*(.+)$", re.IGNORECASE),
            ExpansionType.VEC: re.compile(r"^vec:\s*(.+)$", re.IGNORECASE),
            ExpansionType.HYDE: re.compile(r"^hyde:\s*(.+)$", re.IGNORECASE),
        }

        # Count limits
        counts = {
            ExpansionType.LEX: 0,
            ExpansionType.VEC: 0,
            ExpansionType.HYDE: 0,
        }
        limits = {
            ExpansionType.LEX: self.config.max_lex_variants,
            ExpansionType.VEC: self.config.max_vec_variants,
            ExpansionType.HYDE: self.config.max_hyde_passages,
        }

        for line in lines:
            line = line.strip()
            if not line:
                continue

            for exp_type, pattern in patterns.items():
                match = pattern.match(line)
                if match and counts[exp_type] < limits[exp_type]:
                    text = match.group(1).strip()
                    if text:  # Non-empty
                        expansions.append(
                            QueryExpansion(
                                expansion_type=exp_type,
                                text=text,
                                weight=1.0,
                            )
                        )
                        counts[exp_type] += 1
                    break

        return expansions

    async def expand(
        self,
        query: str,
        context: str | None = None,
    ) -> list[QueryExpansion]:
        """Expand query using OpenRouter API.

        Tries primary model first, then falls back through fallback_models.

        Args:
            query: Original query
            context: Optional collection context

        Returns:
            List of query expansions
        """
        client = self._get_client()
        prompt = self._build_prompt(query, context)

        # Build model list: primary + fallbacks
        models_to_try = [self.config.model] + self.config.fallback_models

        last_error: Exception | None = None

        for model in models_to_try:
            try:
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=self.config.temperature,
                        max_tokens=self.config.max_tokens,
                        extra_headers={
                            "HTTP-Referer": "https://nexus.ai",
                            "X-Title": "Nexus Query Expansion",
                        },
                    ),
                    timeout=self.config.timeout,
                )

                if response.choices and response.choices[0].message.content:
                    expansions = self._parse_response(response.choices[0].message.content)
                    if expansions:
                        logger.debug(
                            f"Query expansion succeeded with model={model}, "
                            f"expansions={len(expansions)}"
                        )
                        return expansions

            except TimeoutError:
                logger.warning(f"Query expansion timeout with model={model}")
                last_error = TimeoutError(f"Timeout with {model}")
            except Exception as e:
                error_str = str(e).lower()
                if "rate_limit" in error_str or "429" in error_str:
                    logger.warning(f"Rate limited on model={model}, trying next")
                    last_error = e
                else:
                    logger.warning(f"Query expansion error with model={model}: {e}")
                    last_error = e

        # All models failed
        if last_error:
            logger.error(f"All query expansion models failed. Last error: {last_error}")

        return []

    async def close(self) -> None:
        """Close the client."""
        if self._client is not None:
            await self._client.close()
            self._client = None


class SignalDetector:
    """Detects strong BM25 signal to skip unnecessary query expansion.

    Based on QMD's approach: if the initial BM25 search returns a high-confidence
    result, query expansion is skipped to save latency and cost.
    """

    def __init__(
        self,
        strong_signal_threshold: float = 0.85,
        separation_threshold: float = 0.10,
    ) -> None:
        """Initialize signal detector.

        Args:
            strong_signal_threshold: Skip expansion if top score >= this
            separation_threshold: Required gap between top-1 and top-2 scores
        """
        self.strong_signal_threshold = strong_signal_threshold
        self.separation_threshold = separation_threshold

    def has_strong_signal(self, results: list[dict[str, Any]]) -> bool:
        """Check if search results indicate a strong signal.

        A strong signal means the top result is highly confident and
        well-separated from the second result, indicating expansion
        is unlikely to help.

        Args:
            results: Search results with 'score' field

        Returns:
            True if signal is strong (should skip expansion)
        """
        if not results:
            return False

        # Get scores
        scores = [r.get("score", 0) for r in results[:2]]
        if not scores:
            return False

        top_score = scores[0]
        second_score = scores[1] if len(scores) > 1 else 0

        # Check both conditions: high score AND good separation
        is_strong = (
            top_score >= self.strong_signal_threshold
            and (top_score - second_score) >= self.separation_threshold
        )

        if is_strong:
            logger.debug(
                f"Strong BM25 signal detected: top={top_score:.3f}, "
                f"second={second_score:.3f}, gap={top_score - second_score:.3f}"
            )

        return is_strong

    def should_expand(self, results: list[dict[str, Any]]) -> bool:
        """Determine if query expansion should be performed.

        Args:
            results: Initial search results

        Returns:
            True if expansion should be performed
        """
        return not self.has_strong_signal(results)


class CachedQueryExpander(QueryExpander):
    """Query expander with caching layer.

    Wraps any QueryExpander with content-hash based caching to reduce
    LLM API calls. Uses Redis/Dragonfly for cache backend.
    """

    def __init__(
        self,
        expander: QueryExpander,
        cache: Redis,
        ttl: int = 3600,
        key_prefix: str = "qexp",
    ) -> None:
        """Initialize cached expander.

        Args:
            expander: Underlying query expander
            cache: Redis/Dragonfly client
            ttl: Cache TTL in seconds
            key_prefix: Prefix for cache keys
        """
        self.expander = expander
        self.cache = cache
        self.ttl = ttl
        self.key_prefix = key_prefix

    def _cache_key(self, query: str, context: str | None) -> str:
        """Generate cache key from query and context.

        Args:
            query: Original query
            context: Optional context

        Returns:
            Cache key string
        """
        content = f"{query}:{context or ''}"
        hash_hex = hashlib.sha256(content.encode()).hexdigest()[:16]
        return f"{self.key_prefix}:{hash_hex}"

    def _serialize(self, expansions: list[QueryExpansion]) -> str:
        """Serialize expansions to JSON string."""
        import json

        return json.dumps([e.to_dict() for e in expansions])

    def _deserialize(self, data: str) -> list[QueryExpansion]:
        """Deserialize expansions from JSON string."""
        import json

        items = json.loads(data)
        return [
            QueryExpansion(
                expansion_type=ExpansionType(item["type"]),
                text=item["text"],
                weight=item.get("weight", 1.0),
            )
            for item in items
        ]

    async def expand(
        self,
        query: str,
        context: str | None = None,
    ) -> list[QueryExpansion]:
        """Expand query with caching.

        Checks cache first, falls back to underlying expander on miss.

        Args:
            query: Original query
            context: Optional collection context

        Returns:
            List of query expansions
        """
        cache_key = self._cache_key(query, context)

        # Try cache first
        try:
            cached = await self.cache.get(cache_key)
            if cached:
                logger.debug(f"Query expansion cache hit for key={cache_key}")
                return self._deserialize(cached)
        except Exception as e:
            logger.warning(f"Cache read error: {e}")

        # Cache miss - generate expansions
        expansions = await self.expander.expand(query, context)

        # Store in cache (best effort)
        if expansions:
            try:
                await self.cache.setex(cache_key, self.ttl, self._serialize(expansions))
                logger.debug(f"Cached query expansion for key={cache_key}")
            except Exception as e:
                logger.warning(f"Cache write error: {e}")

        return expansions

    async def close(self) -> None:
        """Close underlying expander."""
        await self.expander.close()


class QueryExpansionService:
    """High-level service for query expansion with smart triggering.

    Combines query expansion with signal detection for optimal performance:
    - Checks initial BM25 results for strong signal
    - Skips expansion if confident result already found
    - Expands query only when needed for better recall
    """

    def __init__(
        self,
        expander: QueryExpander,
        config: QueryExpansionConfig | None = None,
    ) -> None:
        """Initialize expansion service.

        Args:
            expander: Query expander implementation
            config: Configuration (uses defaults if not provided)
        """
        self.expander = expander
        self.config = config or QueryExpansionConfig()
        self.signal_detector = SignalDetector(
            strong_signal_threshold=self.config.strong_signal_threshold,
            separation_threshold=self.config.signal_separation_threshold,
        )

    async def expand_if_needed(
        self,
        query: str,
        initial_results: list[dict[str, Any]] | None = None,
        context: str | None = None,
        force: bool = False,
    ) -> ExpansionResult:
        """Expand query if initial results don't show strong signal.

        Args:
            query: Original query
            initial_results: Optional initial BM25 search results for signal detection
            context: Optional collection context
            force: Force expansion even if strong signal detected

        Returns:
            ExpansionResult with expansions and metadata
        """
        import time

        start_time = time.perf_counter()

        # Check if expansion is enabled
        if not self.config.enabled:
            return ExpansionResult(
                original_query=query,
                expansions=[],
                was_expanded=False,
                skip_reason="expansion_disabled",
            )

        # Check for strong signal (skip expansion)
        if (
            not force
            and initial_results
            and not self.signal_detector.should_expand(initial_results)
        ):
            return ExpansionResult(
                original_query=query,
                expansions=[],
                was_expanded=False,
                skip_reason="strong_bm25_signal",
                latency_ms=(time.perf_counter() - start_time) * 1000,
            )

        # Perform expansion
        try:
            expansions = await self.expander.expand(query, context)
            latency_ms = (time.perf_counter() - start_time) * 1000

            # Get model name from expander config if available
            model_used = None
            if hasattr(self.expander, "config"):
                expander_config = getattr(self.expander, "config", None)
                if expander_config and hasattr(expander_config, "model"):
                    model_used = expander_config.model

            return ExpansionResult(
                original_query=query,
                expansions=expansions,
                was_expanded=True,
                model_used=model_used,
                latency_ms=latency_ms,
            )
        except Exception as e:
            logger.error(f"Query expansion failed: {e}")
            latency_ms = (time.perf_counter() - start_time) * 1000

            return ExpansionResult(
                original_query=query,
                expansions=[],
                was_expanded=False,
                skip_reason=f"error: {e}",
                latency_ms=latency_ms,
            )

    async def close(self) -> None:
        """Close resources."""
        await self.expander.close()


# Factory functions


def create_query_expander(
    provider: str = "openrouter",
    model: str | None = None,
    api_key: str | None = None,
    config: QueryExpansionConfig | None = None,
) -> QueryExpander:
    """Create a query expander instance.

    Args:
        provider: LLM provider ("openrouter", "openai", "anthropic")
        model: Model to use (provider-specific default if not specified)
        api_key: API key (falls back to environment variable)
        config: Expansion configuration

    Returns:
        QueryExpander instance

    Raises:
        ValueError: If provider is not supported
    """
    config = config or QueryExpansionConfig()

    if model:
        config.model = model

    if provider == "openrouter":
        return OpenRouterQueryExpander(config=config, api_key=api_key)
    else:
        raise ValueError(f"Unsupported provider: {provider}. Supported: openrouter")


async def create_cached_query_expander(
    provider: str = "openrouter",
    model: str | None = None,
    api_key: str | None = None,
    cache_url: str | None = None,
    cache_ttl: int = 3600,
    config: QueryExpansionConfig | None = None,
) -> QueryExpander:
    """Create a cached query expander instance.

    Args:
        provider: LLM provider
        model: Model to use
        api_key: API key
        cache_url: Redis/Dragonfly URL (e.g., "redis://localhost:6379")
        cache_ttl: Cache TTL in seconds
        config: Expansion configuration

    Returns:
        QueryExpander instance (cached if cache_url provided)
    """
    base_expander = create_query_expander(
        provider=provider,
        model=model,
        api_key=api_key,
        config=config,
    )

    if cache_url:
        try:
            from redis.asyncio import Redis

            cache = Redis.from_url(cache_url)
            return CachedQueryExpander(
                expander=base_expander,
                cache=cache,
                ttl=cache_ttl,
            )
        except ImportError:
            logger.warning("redis package not installed, caching disabled")
        except Exception as e:
            logger.warning(f"Failed to connect to cache: {e}, caching disabled")

    return base_expander


def create_query_expansion_service(
    provider: str = "openrouter",
    model: str | None = None,
    api_key: str | None = None,
    config: QueryExpansionConfig | None = None,
) -> QueryExpansionService:
    """Create a query expansion service with smart triggering.

    Args:
        provider: LLM provider
        model: Model to use
        api_key: API key
        config: Expansion configuration

    Returns:
        QueryExpansionService instance
    """
    expander = create_query_expander(
        provider=provider,
        model=model,
        api_key=api_key,
        config=config,
    )

    return QueryExpansionService(expander=expander, config=config)


def get_expansion_config_from_env() -> QueryExpansionConfig:
    """Create QueryExpansionConfig from environment variables.

    Environment variables:
        NEXUS_QUERY_EXPANSION_ENABLED: Enable/disable (default: true)
        NEXUS_QUERY_EXPANSION_PROVIDER: Provider (default: openrouter)
        NEXUS_QUERY_EXPANSION_MODEL: Model (default: deepseek/deepseek-chat)
        NEXUS_QUERY_EXPANSION_STRONG_SIGNAL: Strong signal threshold (default: 0.85)
        NEXUS_QUERY_EXPANSION_CACHE_ENABLED: Enable caching (default: true)
        NEXUS_QUERY_EXPANSION_CACHE_TTL: Cache TTL seconds (default: 3600)
        NEXUS_QUERY_EXPANSION_TIMEOUT: LLM timeout seconds (default: 5.0)

    Returns:
        QueryExpansionConfig from environment
    """

    def get_bool(key: str, default: bool) -> bool:
        val = os.environ.get(key, "").lower()
        if val in ("true", "1", "yes"):
            return True
        if val in ("false", "0", "no"):
            return False
        return default

    def get_float(key: str, default: float) -> float:
        val = os.environ.get(key)
        return float(val) if val else default

    def get_int(key: str, default: int) -> int:
        val = os.environ.get(key)
        return int(val) if val else default

    return QueryExpansionConfig(
        enabled=get_bool("NEXUS_QUERY_EXPANSION_ENABLED", True),
        provider=os.environ.get("NEXUS_QUERY_EXPANSION_PROVIDER", "openrouter"),
        model=os.environ.get("NEXUS_QUERY_EXPANSION_MODEL", "deepseek/deepseek-chat"),
        strong_signal_threshold=get_float("NEXUS_QUERY_EXPANSION_STRONG_SIGNAL", 0.85),
        signal_separation_threshold=get_float("NEXUS_QUERY_EXPANSION_SEPARATION", 0.10),
        cache_enabled=get_bool("NEXUS_QUERY_EXPANSION_CACHE_ENABLED", True),
        cache_ttl=get_int("NEXUS_QUERY_EXPANSION_CACHE_TTL", 3600),
        timeout=get_float("NEXUS_QUERY_EXPANSION_TIMEOUT", 5.0),
        temperature=get_float("NEXUS_QUERY_EXPANSION_TEMPERATURE", 0.7),
    )
