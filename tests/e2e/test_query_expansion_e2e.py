"""End-to-end tests for query expansion with real LLM (Issue #1174).

These tests require a valid OPENROUTER_API_KEY environment variable.
They make real API calls to test the full query expansion pipeline.

Run with:
    OPENROUTER_API_KEY=sk-or-... pytest tests/e2e/test_query_expansion_e2e.py -v -s

Or to run a quick single test:
    OPENROUTER_API_KEY=sk-or-... python tests/e2e/test_query_expansion_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

# Skip all tests if no API key
pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY not set",
)


class TestQueryExpansionE2E:
    """End-to-end tests with real LLM."""

    @pytest.fixture
    def api_key(self) -> str:
        """Get API key from environment."""
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            pytest.skip("OPENROUTER_API_KEY not set")
        return key

    @pytest.mark.asyncio
    async def test_basic_expansion_deepseek(self, api_key: str):
        """Test basic query expansion with DeepSeek model."""
        from nexus.search.query_expansion import (
            ExpansionType,
            OpenRouterQueryExpander,
            QueryExpansionConfig,
        )

        config = QueryExpansionConfig(
            model="deepseek/deepseek-chat",
            max_lex_variants=2,
            max_vec_variants=2,
            max_hyde_passages=2,
            timeout=10.0,
        )
        expander = OpenRouterQueryExpander(config=config, api_key=api_key)

        try:
            start = time.perf_counter()
            expansions = await expander.expand("kubernetes deployment troubleshooting")
            latency = (time.perf_counter() - start) * 1000

            print("\n=== DeepSeek Expansion Test ===")
            print(f"Latency: {latency:.0f}ms")
            print(f"Expansions ({len(expansions)}):")
            for exp in expansions:
                print(f"  {exp.expansion_type.value}: {exp.text}")

            # Verify we got expansions
            assert len(expansions) >= 3, f"Expected at least 3 expansions, got {len(expansions)}"

            # Verify we have different types
            types = {e.expansion_type for e in expansions}
            assert ExpansionType.LEX in types, "Missing lex expansions"
            assert ExpansionType.VEC in types or ExpansionType.HYDE in types, "Missing vec/hyde expansions"

            # Verify expansions are relevant (contain related terms)
            all_text = " ".join(e.text.lower() for e in expansions)
            assert any(
                term in all_text for term in ["kubernetes", "k8s", "deployment", "pod", "container"]
            ), "Expansions don't seem relevant to the query"

        finally:
            await expander.close()

    @pytest.mark.asyncio
    async def test_expansion_with_context(self, api_key: str):
        """Test expansion with collection context."""
        from nexus.search.query_expansion import (
            OpenRouterQueryExpander,
            QueryExpansionConfig,
        )

        config = QueryExpansionConfig(
            model="deepseek/deepseek-chat",
            timeout=10.0,
        )
        expander = OpenRouterQueryExpander(config=config, api_key=api_key)

        try:
            expansions = await expander.expand(
                query="authentication flow",
                context="OAuth 2.0 and JWT documentation for a Python web application",
            )

            print("\n=== Context-Aware Expansion Test ===")
            print("Context: OAuth 2.0 and JWT documentation")
            print(f"Expansions ({len(expansions)}):")
            for exp in expansions:
                print(f"  {exp.expansion_type.value}: {exp.text}")

            # Verify context influenced the expansions
            all_text = " ".join(e.text.lower() for e in expansions)
            assert any(
                term in all_text for term in ["oauth", "jwt", "token", "authorization", "python"]
            ), "Context didn't influence expansions"

        finally:
            await expander.close()

    @pytest.mark.asyncio
    async def test_expansion_service_smart_triggering(self, api_key: str):
        """Test smart triggering with signal detection."""
        from nexus.search.query_expansion import (
            OpenRouterQueryExpander,
            QueryExpansionConfig,
            QueryExpansionService,
        )

        config = QueryExpansionConfig(
            model="deepseek/deepseek-chat",
            strong_signal_threshold=0.85,
            signal_separation_threshold=0.10,
            timeout=10.0,
        )
        expander = OpenRouterQueryExpander(config=config, api_key=api_key)
        service = QueryExpansionService(expander, config=config)

        try:
            # Test 1: Weak signal should trigger expansion
            print("\n=== Smart Triggering Test ===")
            weak_results = [{"score": 0.5}, {"score": 0.45}]
            result = await service.expand_if_needed(
                "database connection pooling",
                initial_results=weak_results,
            )
            print(f"Weak signal (0.5, 0.45): expanded={result.was_expanded}, "
                  f"expansions={len(result.expansions)}, latency={result.latency_ms:.0f}ms")
            assert result.was_expanded is True
            assert len(result.expansions) >= 3

            # Test 2: Strong signal should skip expansion
            strong_results = [{"score": 0.95}, {"score": 0.70}]
            result = await service.expand_if_needed(
                "database connection pooling",
                initial_results=strong_results,
            )
            print(f"Strong signal (0.95, 0.70): expanded={result.was_expanded}, "
                  f"reason={result.skip_reason}")
            assert result.was_expanded is False
            assert result.skip_reason == "strong_bm25_signal"

        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_free_model_fallback(self, api_key: str):
        """Test fallback to free models."""
        from nexus.search.query_expansion import (
            OpenRouterQueryExpander,
            QueryExpansionConfig,
        )

        # Try free model first
        config = QueryExpansionConfig(
            model="deepseek/deepseek-chat-v3-0324:free",
            fallback_models=[
                "google/gemini-2.0-flash-exp:free",
                "deepseek/deepseek-chat",
            ],
            timeout=15.0,
        )
        expander = OpenRouterQueryExpander(config=config, api_key=api_key)

        try:
            start = time.perf_counter()
            expansions = await expander.expand("React hooks best practices")
            latency = (time.perf_counter() - start) * 1000

            print("\n=== Free Model Test ===")
            print(f"Latency: {latency:.0f}ms")
            print(f"Expansions ({len(expansions)}):")
            for exp in expansions:
                print(f"  {exp.expansion_type.value}: {exp.text}")

            # Should get results from one of the models
            assert len(expansions) >= 1, "Expected at least 1 expansion"

        finally:
            await expander.close()

    @pytest.mark.asyncio
    async def test_technical_query_preservation(self, api_key: str):
        """Test that technical terms are preserved in expansions."""
        from nexus.search.query_expansion import (
            OpenRouterQueryExpander,
            QueryExpansionConfig,
        )

        config = QueryExpansionConfig(
            model="deepseek/deepseek-chat",
            timeout=10.0,
        )
        expander = OpenRouterQueryExpander(config=config, api_key=api_key)

        try:
            # Query with specific technical terms
            expansions = await expander.expand("FastAPI SQLAlchemy async session management")

            print("\n=== Technical Term Preservation Test ===")
            print(f"Expansions ({len(expansions)}):")
            for exp in expansions:
                print(f"  {exp.expansion_type.value}: {exp.text}")

            # Check that key technical terms are preserved
            all_text = " ".join(e.text.lower() for e in expansions)
            preserved_terms = []
            for term in ["fastapi", "sqlalchemy", "async", "session"]:
                if term in all_text:
                    preserved_terms.append(term)

            print(f"Preserved terms: {preserved_terms}")
            assert len(preserved_terms) >= 2, (
                f"Expected at least 2 technical terms preserved, got {preserved_terms}"
            )

        finally:
            await expander.close()

    @pytest.mark.asyncio
    async def test_multiple_queries_latency(self, api_key: str):
        """Test latency across multiple queries."""
        from nexus.search.query_expansion import (
            OpenRouterQueryExpander,
            QueryExpansionConfig,
        )

        config = QueryExpansionConfig(
            model="deepseek/deepseek-chat",
            timeout=10.0,
        )
        expander = OpenRouterQueryExpander(config=config, api_key=api_key)

        queries = [
            "python memory leak debugging",
            "git rebase vs merge",
            "docker compose networking",
            "REST API pagination",
            "typescript generics constraints",
        ]

        try:
            print("\n=== Multi-Query Latency Test ===")
            latencies = []

            for query in queries:
                start = time.perf_counter()
                expansions = await expander.expand(query)
                latency = (time.perf_counter() - start) * 1000
                latencies.append(latency)
                print(f"  '{query[:30]}...': {latency:.0f}ms, {len(expansions)} expansions")

            avg_latency = sum(latencies) / len(latencies)
            p99_latency = sorted(latencies)[int(len(latencies) * 0.99)]

            print("\nStats:")
            print(f"  Avg latency: {avg_latency:.0f}ms")
            print(f"  P99 latency: {p99_latency:.0f}ms")
            print(f"  Min: {min(latencies):.0f}ms, Max: {max(latencies):.0f}ms")

            # Reasonable latency expectations
            assert avg_latency < 5000, f"Average latency too high: {avg_latency}ms"

        finally:
            await expander.close()


async def run_quick_test():
    """Run a quick test without pytest."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: Set OPENROUTER_API_KEY environment variable")
        print("  export OPENROUTER_API_KEY=sk-or-...")
        sys.exit(1)

    # Add src to path if needed
    src_path = os.path.join(os.path.dirname(__file__), "..", "..", "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    from nexus.search.query_expansion import (
        ExpansionType,
        OpenRouterQueryExpander,
        QueryExpansionConfig,
        QueryExpansionService,
    )

    print("=" * 60)
    print("Query Expansion E2E Test (Real LLM)")
    print("=" * 60)

    # Test 1: Basic expansion
    print("\n[Test 1] Basic DeepSeek Expansion")
    print("-" * 40)

    config = QueryExpansionConfig(
        model="deepseek/deepseek-chat",
        max_lex_variants=2,
        max_vec_variants=2,
        max_hyde_passages=2,
        timeout=15.0,
    )
    expander = OpenRouterQueryExpander(config=config, api_key=api_key)

    try:
        query = "kubernetes deployment troubleshooting"
        print(f"Query: {query}")

        start = time.perf_counter()
        expansions = await expander.expand(query)
        latency = (time.perf_counter() - start) * 1000

        print(f"Latency: {latency:.0f}ms")
        print(f"Expansions ({len(expansions)}):")
        for exp in expansions:
            print(f"  {exp.expansion_type.value}: {exp.text}")

        # Verify
        assert len(expansions) >= 3, f"Expected >= 3 expansions, got {len(expansions)}"
        types = {e.expansion_type for e in expansions}
        assert ExpansionType.LEX in types, "Missing LEX expansions"
        print("✓ Test 1 PASSED")

    except Exception as e:
        print(f"✗ Test 1 FAILED: {e}")
        raise

    # Test 2: Smart triggering
    print("\n[Test 2] Smart Signal Detection")
    print("-" * 40)

    service = QueryExpansionService(expander, config=config)

    try:
        # Weak signal - should expand
        weak_results = [{"score": 0.5}, {"score": 0.4}]
        result = await service.expand_if_needed(
            "database connection pooling",
            initial_results=weak_results,
        )
        print(f"Weak signal (0.5, 0.4): expanded={result.was_expanded}")
        assert result.was_expanded is True, "Should expand on weak signal"

        # Strong signal - should skip
        strong_results = [{"score": 0.95}, {"score": 0.70}]
        result = await service.expand_if_needed(
            "database connection pooling",
            initial_results=strong_results,
        )
        print(f"Strong signal (0.95, 0.70): expanded={result.was_expanded}, reason={result.skip_reason}")
        assert result.was_expanded is False, "Should skip on strong signal"
        assert result.skip_reason == "strong_bm25_signal"

        print("✓ Test 2 PASSED")

    except Exception as e:
        print(f"✗ Test 2 FAILED: {e}")
        raise

    finally:
        await expander.close()

    print("\n" + "=" * 60)
    print("All E2E tests PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_quick_test())
