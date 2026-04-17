"""Tests for FederatedSearchDispatcher (Issue #3147 decision 11A).

Tests each responsibility in isolation with mocks:
- Zone discovery
- Parallel fan-out
- Cross-zone dedup via RRF fusion
- Partial failure handling
- Zone metadata in response
- Single-zone optimization
- Configuration
"""

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from nexus.bricks.search.federated_search import (
    FederatedSearchConfig,
    FederatedSearchDispatcher,
)
from nexus.bricks.search.results import BaseSearchResult


@dataclass
class MockSearchResult(BaseSearchResult):
    """Minimal search result for tests."""

    search_type: str = "hybrid"


def _make_result(path: str, score: float, zone_id: str | None = None) -> MockSearchResult:
    return MockSearchResult(
        path=path,
        chunk_text=f"content of {path}",
        score=score,
        zone_id=zone_id,
    )


def _make_daemon(zone_results: dict[str, list[MockSearchResult]]) -> AsyncMock:
    """Create a mock daemon that returns different results per zone_id."""
    daemon = AsyncMock()
    daemon.is_initialized = True

    async def mock_search(
        query,
        search_type="hybrid",
        limit=10,
        path_filter=None,
        alpha=0.5,
        fusion_method="rrf",
        zone_id=None,
        **kwargs,
    ):
        return zone_results.get(zone_id, [])

    daemon.search = mock_search
    return daemon


def _make_rebac(zones: list[str]) -> AsyncMock:
    """Create a mock rebac service that returns given zone list."""
    rebac = AsyncMock()
    rebac.list_accessible_zones = AsyncMock(return_value=zones)
    return rebac


# =============================================================================
# Zone discovery
# =============================================================================


class TestZoneDiscovery:
    @pytest.mark.asyncio
    async def test_searches_all_accessible_zones(self) -> None:
        daemon = _make_daemon(
            {
                "zone_a": [_make_result("a.txt", 5.0)],
                "zone_b": [_make_result("b.txt", 3.0)],
            }
        )
        rebac = _make_rebac(["zone_a", "zone_b"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test query", subject=("user", "alice"))

        assert set(resp.zones_searched) == {"zone_a", "zone_b"}
        assert len(resp.results) == 2

    @pytest.mark.asyncio
    async def test_no_accessible_zones(self) -> None:
        daemon = _make_daemon({})
        rebac = _make_rebac([])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test query", subject=("user", "alice"))

        assert resp.results == []
        assert resp.zones_searched == []
        assert resp.zones_failed == []

    @pytest.mark.asyncio
    async def test_zone_cache_hit(self) -> None:
        """Second call should use cached zones, not call rebac again."""
        daemon = _make_daemon({"zone_a": [_make_result("a.txt", 5.0)]})
        rebac = _make_rebac(["zone_a"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        await dispatcher.search("q1", subject=("user", "alice"))
        await dispatcher.search("q2", subject=("user", "alice"))

        # list_accessible_zones called only once (cached for second call)
        assert rebac.list_accessible_zones.call_count == 1

    @pytest.mark.asyncio
    async def test_zone_cache_different_subjects(self) -> None:
        """Different subjects should have separate cache entries."""
        daemon = _make_daemon(
            {
                "zone_a": [_make_result("a.txt", 5.0)],
                "zone_b": [_make_result("b.txt", 3.0)],
            }
        )
        rebac = _make_rebac(["zone_a"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        await dispatcher.search("q", subject=("user", "alice"))
        await dispatcher.search("q", subject=("user", "bob"))

        assert rebac.list_accessible_zones.call_count == 2

    @pytest.mark.asyncio
    async def test_zone_cache_invalidation(self) -> None:
        daemon = _make_daemon({"zone_a": [_make_result("a.txt", 5.0)]})
        rebac = _make_rebac(["zone_a"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        await dispatcher.search("q", subject=("user", "alice"))
        dispatcher.invalidate_zone_cache(subject=("user", "alice"))
        await dispatcher.search("q", subject=("user", "alice"))

        assert rebac.list_accessible_zones.call_count == 2


# =============================================================================
# Fan-out and result tagging
# =============================================================================


class TestFanOut:
    @pytest.mark.asyncio
    async def test_results_tagged_with_zone_id(self) -> None:
        daemon = _make_daemon(
            {
                "zone_a": [_make_result("a.txt", 5.0)],
                "zone_b": [_make_result("b.txt", 3.0)],
            }
        )
        rebac = _make_rebac(["zone_a", "zone_b"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test", subject=("user", "alice"))

        zone_ids = {r.get("zone_id") for r in resp.results}
        assert zone_ids == {"zone_a", "zone_b"}

    @pytest.mark.asyncio
    async def test_single_zone_skip_fusion(self) -> None:
        """Single zone should return results directly without fusion overhead."""
        daemon = _make_daemon(
            {
                "zone_a": [
                    _make_result("a1.txt", 5.0),
                    _make_result("a2.txt", 3.0),
                ],
            }
        )
        rebac = _make_rebac(["zone_a"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert len(resp.results) == 2
        assert resp.zones_searched == ["zone_a"]

    @pytest.mark.asyncio
    async def test_empty_zone_results(self) -> None:
        """Zone returning empty results should not break fusion."""
        daemon = _make_daemon(
            {
                "zone_a": [_make_result("a.txt", 5.0)],
                "zone_b": [],  # empty
            }
        )
        rebac = _make_rebac(["zone_a", "zone_b"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert len(resp.results) == 1
        assert set(resp.zones_searched) == {"zone_a", "zone_b"}


# =============================================================================
# Partial failure handling (decision 8A)
# =============================================================================


class TestPartialFailure:
    @pytest.mark.asyncio
    async def test_one_zone_fails_others_succeed(self) -> None:
        daemon = AsyncMock()
        daemon.is_initialized = True

        async def mock_search(
            query,
            search_type="hybrid",
            limit=10,
            path_filter=None,
            alpha=0.5,
            fusion_method="rrf",
            zone_id=None,
            **kwargs,
        ):
            if zone_id == "zone_bad":
                raise ConnectionError("zone offline")
            return [_make_result(f"{zone_id}/doc.txt", 5.0)]

        daemon.search = mock_search
        rebac = _make_rebac(["zone_good", "zone_bad"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert "zone_good" in resp.zones_searched
        assert len(resp.zones_failed) == 1
        assert resp.zones_failed[0].zone_id == "zone_bad"
        assert "offline" in resp.zones_failed[0].error
        assert len(resp.results) >= 1

    @pytest.mark.asyncio
    async def test_all_zones_fail(self) -> None:
        daemon = AsyncMock()
        daemon.is_initialized = True

        async def mock_search(**kwargs):
            raise TimeoutError("timed out")

        daemon.search = mock_search
        rebac = _make_rebac(["zone_a", "zone_b"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert resp.results == []
        assert resp.zones_searched == []
        assert len(resp.zones_failed) == 2

    @pytest.mark.asyncio
    async def test_single_zone_failure(self) -> None:
        """Single accessible zone that fails should return gracefully."""
        daemon = AsyncMock()
        daemon.is_initialized = True

        async def mock_search(**kwargs):
            raise RuntimeError("broken")

        daemon.search = mock_search
        rebac = _make_rebac(["zone_a"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert resp.results == []
        assert resp.zones_searched == []
        assert len(resp.zones_failed) == 1


# =============================================================================
# RRF fusion across zones (decision 5A)
# =============================================================================


class TestCrossZoneFusion:
    @pytest.mark.asyncio
    async def test_results_fused_across_zones(self) -> None:
        """Multi-zone results should be fused via RRF and limited."""
        daemon = _make_daemon(
            {
                "zone_a": [_make_result(f"a_{i}.txt", 10.0 - i) for i in range(5)],
                "zone_b": [_make_result(f"b_{i}.txt", 10.0 - i) for i in range(5)],
            }
        )
        rebac = _make_rebac(["zone_a", "zone_b"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test", subject=("user", "alice"), limit=3)

        assert len(resp.results) == 3

    @pytest.mark.asyncio
    async def test_latency_reported(self) -> None:
        daemon = _make_daemon({"zone_a": [_make_result("a.txt", 5.0)]})
        rebac = _make_rebac(["zone_a"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert resp.latency_ms > 0


# =============================================================================
# Configuration
# =============================================================================


class TestConfiguration:
    @pytest.mark.asyncio
    async def test_custom_timeout(self) -> None:
        """Very short timeout should cause zone failure."""
        daemon = AsyncMock()
        daemon.is_initialized = True

        async def slow_search(**kwargs):
            await asyncio.sleep(1.0)
            return [_make_result("a.txt", 5.0)]

        daemon.search = slow_search
        rebac = _make_rebac(["zone_a"])

        config = FederatedSearchConfig(zone_timeout_seconds=0.01)
        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac, config=config)
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert len(resp.zones_failed) == 1
        assert resp.results == []

    @pytest.mark.asyncio
    async def test_concurrency_bound(self) -> None:
        """Semaphore should limit concurrent zone searches."""
        max_concurrent = 0
        current_concurrent = 0

        daemon = AsyncMock()
        daemon.is_initialized = True

        async def tracking_search(
            query,
            search_type="hybrid",
            limit=10,
            path_filter=None,
            alpha=0.5,
            fusion_method="rrf",
            zone_id=None,
            **kwargs,
        ):
            nonlocal max_concurrent, current_concurrent
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.01)
            current_concurrent -= 1
            return [_make_result(f"{zone_id}/doc.txt", 5.0)]

        daemon.search = tracking_search
        rebac = _make_rebac([f"zone_{i}" for i in range(10)])

        config = FederatedSearchConfig(max_concurrent_zones=3)
        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac, config=config)
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert max_concurrent <= 3
        assert len(resp.zones_searched) == 10


# =============================================================================
# Phase 2: Registry-backed dispatch
# =============================================================================


class TestRegistryDispatch:
    @pytest.mark.asyncio
    async def test_uses_per_zone_daemon(self) -> None:
        """With registry, each zone should use its own daemon."""

        from nexus.bricks.search.zone_registry import (
            ZoneSearchCapabilities,
            ZoneSearchRegistry,
        )

        daemon_a = AsyncMock()
        daemon_b = AsyncMock()

        async def search_a(
            query,
            search_type="hybrid",
            limit=10,
            path_filter=None,
            alpha=0.5,
            fusion_method="rrf",
            zone_id=None,
            **kw,
        ):
            return [_make_result("a_doc.txt", 5.0)]

        async def search_b(
            query,
            search_type="hybrid",
            limit=10,
            path_filter=None,
            alpha=0.5,
            fusion_method="rrf",
            zone_id=None,
            **kw,
        ):
            return [_make_result("b_doc.txt", 3.0)]

        daemon_a.search = search_a
        daemon_b.search = search_b

        registry = ZoneSearchRegistry()
        caps = ZoneSearchCapabilities(zone_id="zone_a")
        registry.register("zone_a", daemon_a, capabilities=caps)
        registry.register("zone_b", daemon_b, capabilities=ZoneSearchCapabilities(zone_id="zone_b"))

        rebac = _make_rebac(["zone_a", "zone_b"])
        fallback = AsyncMock()

        dispatcher = FederatedSearchDispatcher(
            daemon=fallback,
            rebac=rebac,
            registry=registry,
        )
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert len(resp.results) == 2
        assert set(resp.zones_searched) == {"zone_a", "zone_b"}


# =============================================================================
# Phase 3: Capability-aware zone skipping
# =============================================================================


class TestCapabilityAwareRouting:
    @pytest.mark.asyncio
    async def test_skip_keyword_only_zone_for_semantic(self) -> None:
        """Keyword-only zones should be skipped for semantic queries."""
        from nexus.bricks.search.zone_registry import (
            ZoneSearchCapabilities,
            ZoneSearchRegistry,
        )

        daemon = _make_daemon(
            {
                "zone_server": [_make_result("server.txt", 5.0)],
                "zone_phone": [_make_result("phone.txt", 3.0)],
            }
        )

        registry = ZoneSearchRegistry(default_daemon=daemon)
        registry.register(
            "zone_server",
            daemon,
            capabilities=ZoneSearchCapabilities(
                zone_id="zone_server",
                search_modes=("keyword", "semantic", "hybrid"),
            ),
        )
        registry.register(
            "zone_phone",
            daemon,
            capabilities=ZoneSearchCapabilities(
                zone_id="zone_phone",
                device_tier="phone",
                search_modes=("keyword",),
            ),
        )

        rebac = _make_rebac(["zone_server", "zone_phone"])
        dispatcher = FederatedSearchDispatcher(
            daemon=daemon,
            rebac=rebac,
            registry=registry,
        )
        resp = await dispatcher.search(
            "test",
            subject=("user", "alice"),
            search_type="semantic",
        )

        assert "zone_server" in resp.zones_searched
        assert "zone_phone" in resp.zones_skipped

    @pytest.mark.asyncio
    async def test_no_zones_skipped_for_hybrid(self) -> None:
        """Hybrid queries should not skip keyword-only zones
        (they get routed to keyword-only mode for that zone)."""
        from nexus.bricks.search.zone_registry import (
            ZoneSearchCapabilities,
            ZoneSearchRegistry,
        )

        daemon = _make_daemon(
            {
                "zone_server": [_make_result("server.txt", 5.0)],
                "zone_phone": [_make_result("phone.txt", 3.0)],
            }
        )

        registry = ZoneSearchRegistry(default_daemon=daemon)
        registry.register(
            "zone_server",
            daemon,
            capabilities=ZoneSearchCapabilities(
                zone_id="zone_server",
                search_modes=("keyword", "semantic", "hybrid"),
            ),
        )
        registry.register(
            "zone_phone",
            daemon,
            capabilities=ZoneSearchCapabilities(
                zone_id="zone_phone",
                device_tier="phone",
                search_modes=("keyword",),
            ),
        )

        rebac = _make_rebac(["zone_server", "zone_phone"])
        dispatcher = FederatedSearchDispatcher(
            daemon=daemon,
            rebac=rebac,
            registry=registry,
        )
        resp = await dispatcher.search(
            "test",
            subject=("user", "alice"),
            search_type="hybrid",
        )

        # Hybrid search should not skip — keyword-only zones get keyword mode
        assert resp.zones_skipped == []


# =============================================================================
# Phase 3: Result caching
# =============================================================================


class TestResultCaching:
    @pytest.mark.asyncio
    async def test_cache_hit(self) -> None:
        """Second identical query should return cached result."""
        daemon = _make_daemon({"zone_a": [_make_result("a.txt", 5.0)]})
        rebac = _make_rebac(["zone_a"])

        config = FederatedSearchConfig(result_cache_enabled=True)
        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac, config=config)

        resp1 = await dispatcher.search("test", subject=("user", "alice"))
        resp2 = await dispatcher.search("test", subject=("user", "alice"))

        assert not resp1.cached
        assert resp2.cached

    @pytest.mark.asyncio
    async def test_cache_disabled_by_default(self) -> None:
        daemon = _make_daemon({"zone_a": [_make_result("a.txt", 5.0)]})
        rebac = _make_rebac(["zone_a"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)

        resp1 = await dispatcher.search("test", subject=("user", "alice"))
        resp2 = await dispatcher.search("test", subject=("user", "alice"))

        assert not resp1.cached
        assert not resp2.cached  # Cache disabled

    @pytest.mark.asyncio
    async def test_cache_invalidation(self) -> None:
        daemon = _make_daemon({"zone_a": [_make_result("a.txt", 5.0)]})
        rebac = _make_rebac(["zone_a"])

        config = FederatedSearchConfig(result_cache_enabled=True)
        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac, config=config)

        await dispatcher.search("test", subject=("user", "alice"))
        dispatcher.invalidate_result_cache()
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert not resp.cached


# =============================================================================
# filter_federated_results (Issue #3147, gap item 5)
# =============================================================================


class TestFilterFederatedResults:
    @pytest.mark.asyncio
    async def test_allows_all_when_permitted(self) -> None:
        from nexus.bricks.search.federated_search import filter_federated_results

        results = [
            _make_result("a.txt", 5.0, zone_id="zone_a"),
            _make_result("b.txt", 3.0, zone_id="zone_a"),
        ]
        rebac = AsyncMock()
        rebac.rebac_check_batch = AsyncMock(return_value=[True, True])

        filtered = await filter_federated_results(
            results,
            subject=("user", "alice"),
            rebac=rebac,
        )
        assert len(filtered) == 2

    @pytest.mark.asyncio
    async def test_filters_denied_results(self) -> None:
        from nexus.bricks.search.federated_search import filter_federated_results

        results = [
            _make_result("allowed.txt", 5.0, zone_id="zone_a"),
            _make_result("denied.txt", 3.0, zone_id="zone_a"),
            _make_result("also_allowed.txt", 2.0, zone_id="zone_a"),
        ]
        rebac = AsyncMock()
        rebac.rebac_check_batch = AsyncMock(return_value=[True, False, True])

        filtered = await filter_federated_results(
            results,
            subject=("user", "alice"),
            rebac=rebac,
        )
        assert len(filtered) == 2
        paths = [r.path for r in filtered]
        assert "allowed.txt" in paths
        assert "denied.txt" not in paths

    @pytest.mark.asyncio
    async def test_groups_by_zone(self) -> None:
        """Results from different zones should be batched per zone."""
        from nexus.bricks.search.federated_search import filter_federated_results

        results = [
            _make_result("a.txt", 5.0, zone_id="zone_a"),
            _make_result("b.txt", 3.0, zone_id="zone_b"),
        ]
        rebac = AsyncMock()
        # Two separate batch calls — one per zone
        rebac.rebac_check_batch = AsyncMock(return_value=[True])

        filtered = await filter_federated_results(
            results,
            subject=("user", "alice"),
            rebac=rebac,
        )
        # Should have been called twice (once per zone)
        assert rebac.rebac_check_batch.call_count == 2
        assert len(filtered) == 2

    @pytest.mark.asyncio
    async def test_empty_results(self) -> None:
        from nexus.bricks.search.federated_search import filter_federated_results

        filtered = await filter_federated_results(
            [],
            subject=("user", "alice"),
            rebac=AsyncMock(),
        )
        assert filtered == []

    @pytest.mark.asyncio
    async def test_fail_open_on_rebac_error(self) -> None:
        """If ReBAC is unavailable, allow results (fail-open)."""
        from nexus.bricks.search.federated_search import filter_federated_results

        results = [_make_result("a.txt", 5.0, zone_id="zone_a")]
        rebac = AsyncMock()
        rebac.rebac_check_batch = AsyncMock(side_effect=RuntimeError("DB down"))

        filtered = await filter_federated_results(
            results,
            subject=("user", "alice"),
            rebac=rebac,
        )
        # Fail-open: result allowed despite error
        assert len(filtered) == 1

    @pytest.mark.asyncio
    async def test_passes_correct_checks(self) -> None:
        """Verify the batch check receives correct (subject, perm, object) tuples."""
        from nexus.bricks.search.federated_search import filter_federated_results

        results = [_make_result("/docs/secret.txt", 5.0, zone_id="zone_x")]
        rebac = AsyncMock()
        rebac.rebac_check_batch = AsyncMock(return_value=[True])

        await filter_federated_results(
            results,
            subject=("agent", "bot_1"),
            rebac=rebac,
        )

        rebac.rebac_check_batch.assert_called_once_with(
            checks=[(("agent", "bot_1"), "viewer", ("file", "/docs/secret.txt"))],
            zone_id="zone_x",
        )


# =============================================================================
# Bug fix: cross-zone dedup uses zone_qualified_path (Codex finding #1)
# =============================================================================


class TestCrossZoneDedup:
    @pytest.mark.asyncio
    async def test_same_path_different_zones_not_collapsed(self) -> None:
        """Identical paths from different zones must NOT be merged."""
        daemon = _make_daemon(
            {
                "zone_a": [_make_result("/shared/doc.txt", 5.0)],
                "zone_b": [_make_result("/shared/doc.txt", 3.0)],
            }
        )
        rebac = _make_rebac(["zone_a", "zone_b"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test", subject=("user", "alice"))

        # Both results should survive — they're from different zones
        assert len(resp.results) == 2
        # Verify they have different zone_qualified_path values
        zqps = {r.get("zone_qualified_path") for r in resp.results}
        assert "zone_a:/shared/doc.txt" in zqps
        assert "zone_b:/shared/doc.txt" in zqps

    def test_to_dict_includes_zone_qualified_path_property(self) -> None:
        """_to_dict in fusion.py must include @property zone_qualified_path."""
        from nexus.bricks.search.fusion import _to_dict

        r = MockSearchResult(
            path="doc.txt",
            chunk_text="x",
            score=1.0,
            zone_id="zone_a",
        )
        d = _to_dict(r)
        assert "zone_qualified_path" in d
        assert d["zone_qualified_path"] == "zone_a:doc.txt"


# =============================================================================
# Bug fix: per-file ReBAC wired into dispatcher (Codex finding #2)
# =============================================================================


class TestPerFileRebacWired:
    @pytest.mark.asyncio
    async def test_single_zone_filters_when_enabled(self) -> None:
        """With enable_per_file_rebac=True, results should be filtered."""
        daemon = _make_daemon(
            {
                "zone_a": [
                    _make_result("allowed.txt", 5.0),
                    _make_result("denied.txt", 3.0),
                ],
            }
        )
        rebac = _make_rebac(["zone_a"])
        rebac.rebac_check_batch = AsyncMock(return_value=[True, False])

        dispatcher = FederatedSearchDispatcher(
            daemon=daemon,
            rebac=rebac,
            enable_per_file_rebac=True,
        )
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert len(resp.results) == 1
        assert resp.results[0]["path"] == "allowed.txt"

    @pytest.mark.asyncio
    async def test_multi_zone_filters_when_enabled(self) -> None:
        """Multi-zone path should also apply per-file ReBAC before fusion."""
        daemon = _make_daemon(
            {
                "zone_a": [_make_result("a_ok.txt", 5.0), _make_result("a_deny.txt", 4.0)],
                "zone_b": [_make_result("b_ok.txt", 3.0)],
            }
        )
        rebac = _make_rebac(["zone_a", "zone_b"])
        # zone_a: first allowed, second denied; zone_b: allowed
        rebac.rebac_check_batch = AsyncMock(
            side_effect=[[True, False], [True]],
        )

        dispatcher = FederatedSearchDispatcher(
            daemon=daemon,
            rebac=rebac,
            enable_per_file_rebac=True,
        )
        resp = await dispatcher.search("test", subject=("user", "alice"))

        result_paths = {r.get("path") or r["path"] for r in resp.results}
        assert "a_ok.txt" in result_paths
        assert "b_ok.txt" in result_paths
        assert "a_deny.txt" not in result_paths

    @pytest.mark.asyncio
    async def test_no_filter_when_disabled(self) -> None:
        """Default (enable_per_file_rebac=False) should NOT call rebac_check_batch."""
        daemon = _make_daemon({"zone_a": [_make_result("a.txt", 5.0)]})
        rebac = _make_rebac(["zone_a"])
        rebac.rebac_check_batch = AsyncMock()

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert len(resp.results) == 1
        rebac.rebac_check_batch.assert_not_called()


# =============================================================================
# Bug fix: SearchDelegation minted for remote zones (Codex finding #2)
# =============================================================================


class TestSearchDelegationMinting:
    @pytest.mark.asyncio
    async def test_mints_delegation_for_remote_zone(self) -> None:
        """Remote zones should mint a SearchDelegation and send via transport."""
        from unittest.mock import MagicMock, patch

        from nexus.bricks.search.zone_registry import (
            ZoneSearchCapabilities,
            ZoneSearchRegistry,
        )

        default_daemon = AsyncMock()
        registry = ZoneSearchRegistry(default_daemon=default_daemon)

        # Register as remote with transport
        mock_transport = MagicMock()
        mock_transport.call_rpc = MagicMock(
            return_value=[
                {"path": "remote.txt", "chunk_text": "x", "score": 0.9},
            ]
        )
        registry.register_remote(
            "zone_remote",
            mock_transport,
            capabilities=ZoneSearchCapabilities(zone_id="zone_remote"),
        )

        rebac = _make_rebac(["zone_remote"])

        dispatcher = FederatedSearchDispatcher(
            daemon=default_daemon,
            rebac=rebac,
            registry=registry,
        )

        # Track delegation minting
        mint_calls: list = []
        original_mint = dispatcher._mint_search_delegation

        def tracking_mint(subject, source_zone_id, target_zones):
            mint_calls.append((subject, target_zones))
            return original_mint(subject, source_zone_id, target_zones)

        with patch.object(dispatcher, "_mint_search_delegation", side_effect=tracking_mint):
            resp = await dispatcher.search("test", subject=("user", "alice"))

        assert len(resp.results) == 1
        assert len(mint_calls) == 1
        assert mint_calls[0][0] == ("user", "alice")
        assert "zone_remote" in mint_calls[0][1]

        # Verify transport.call_rpc was called with delegation_id as auth_token
        mock_transport.call_rpc.assert_called_once()
        call_args = mock_transport.call_rpc.call_args[0]
        assert call_args[0] == "search"  # method
        assert call_args[1]["query"] == "test"  # params
        # call_args[2] is read_timeout (None), call_args[3] is auth_token
        delegation_token = call_args[3]
        assert delegation_token is not None
        assert delegation_token.startswith("sd_")  # SearchDelegation ID format

    @pytest.mark.asyncio
    async def test_no_delegation_for_local_zone(self) -> None:
        """When a zone uses the default (local) daemon, no delegation is minted."""
        from unittest.mock import patch

        daemon = _make_daemon({"zone_local": [_make_result("local.txt", 5.0)]})
        rebac = _make_rebac(["zone_local"])

        dispatcher = FederatedSearchDispatcher(daemon=daemon, rebac=rebac)

        mint_calls: list = []
        original_mint = dispatcher._mint_search_delegation

        def tracking_mint(subject, source_zone_id, target_zones):
            mint_calls.append(True)
            return original_mint(subject, source_zone_id, target_zones)

        with patch.object(dispatcher, "_mint_search_delegation", side_effect=tracking_mint):
            resp = await dispatcher.search("test", subject=("user", "alice"))

        assert len(resp.results) == 1
        assert len(mint_calls) == 0  # No delegation minted for local


# =============================================================================
# Remote zone search via gRPC transport (Codex final finding)
# =============================================================================


class TestRemoteZoneSearch:
    @pytest.mark.asyncio
    async def test_remote_zone_uses_transport(self) -> None:
        """Remote zones should search via transport.call_rpc, not daemon.search."""
        from unittest.mock import MagicMock

        from nexus.bricks.search.zone_registry import (
            ZoneSearchCapabilities,
            ZoneSearchRegistry,
        )

        default_daemon = AsyncMock()
        registry = ZoneSearchRegistry(default_daemon=default_daemon)

        # Register a remote zone with a mock transport
        mock_transport = MagicMock()
        mock_transport.call_rpc = MagicMock(
            return_value=[
                {"path": "remote_doc.txt", "chunk_text": "remote content", "score": 0.9},
            ]
        )
        registry.register_remote(
            "zone_remote",
            mock_transport,
            capabilities=ZoneSearchCapabilities(zone_id="zone_remote"),
        )

        rebac = _make_rebac(["zone_remote"])

        dispatcher = FederatedSearchDispatcher(
            daemon=default_daemon,
            rebac=rebac,
            registry=registry,
        )
        resp = await dispatcher.search("test query", subject=("user", "alice"))

        # Transport should have been called with "search" method and delegation auth
        mock_transport.call_rpc.assert_called_once()
        call_args = mock_transport.call_rpc.call_args[0]
        assert call_args[0] == "search"  # method name
        assert call_args[1]["query"] == "test query"  # params
        assert call_args[1]["zone_id"] == "zone_remote"
        # Verify delegation_id passed as auth_token (4th positional arg)
        auth_token = call_args[3]
        assert auth_token is not None
        assert auth_token.startswith("sd_")

        # Results should be tagged with zone provenance
        assert len(resp.results) == 1
        assert resp.results[0]["zone_id"] == "zone_remote"
        assert resp.results[0]["zone_qualified_path"] == "zone_remote:remote_doc.txt"
        assert "zone_remote" in resp.zones_searched

    @pytest.mark.asyncio
    async def test_remote_failure_goes_to_zones_failed(self) -> None:
        """Transport errors should appear in zones_failed."""
        from unittest.mock import MagicMock

        from nexus.bricks.search.zone_registry import (
            ZoneSearchCapabilities,
            ZoneSearchRegistry,
        )

        default_daemon = AsyncMock()
        registry = ZoneSearchRegistry(default_daemon=default_daemon)

        mock_transport = MagicMock()
        mock_transport.call_rpc = MagicMock(side_effect=ConnectionError("node offline"))
        registry.register_remote(
            "zone_dead",
            mock_transport,
            capabilities=ZoneSearchCapabilities(zone_id="zone_dead"),
        )

        rebac = _make_rebac(["zone_dead"])
        dispatcher = FederatedSearchDispatcher(
            daemon=default_daemon,
            rebac=rebac,
            registry=registry,
        )
        resp = await dispatcher.search("test", subject=("user", "alice"))

        assert len(resp.zones_failed) == 1
        assert resp.zones_failed[0].zone_id == "zone_dead"
        assert resp.results == []


class TestResultToDictContextShape:
    """Issue #3773 Round-5 review: federated dict payload must omit the
    ``context`` key when unset so the response shape matches the
    non-federated router (which gates on ``context is not None``).
    Otherwise strict clients see the field appear/disappear based on the
    ``federated=true`` flag."""

    def test_result_to_dict_omits_context_when_none(self) -> None:
        from nexus.bricks.search.federated_search import _result_to_dict

        r = _make_result("a.md", 0.9, zone_id="root")
        assert r.context is None
        d = _result_to_dict(r)
        assert "context" not in d

    def test_result_to_dict_keeps_context_when_set(self) -> None:
        from nexus.bricks.search.federated_search import _result_to_dict

        r = _make_result("a.md", 0.9, zone_id="root")
        r.context = "Some description"
        d = _result_to_dict(r)
        assert d["context"] == "Some description"

    def test_strip_none_context_also_strips_plain_dicts(self) -> None:
        """Round-6 review: RRF fusion dicts go through ``_strip_none_context``
        directly (not via ``_result_to_dict``) because ``rrf_multi_fusion``
        emits dicts built from ``__dataclass_fields__`` verbatim."""
        from nexus.bricks.search.federated_search import _strip_none_context

        assert _strip_none_context({"path": "a", "context": None}) == {"path": "a"}
        assert _strip_none_context({"path": "a", "context": "x"}) == {
            "path": "a",
            "context": "x",
        }


class TestFederatedRRFContextShape:
    """Round-6 review regression: with fusion_strategy=RRF, the dicts
    produced by ``rrf_multi_fusion`` must not carry ``context: null`` —
    shape must match the RAW_SCORE path.
    """

    def test_rrf_multi_fusion_dicts_stripped_of_none_context(self) -> None:
        from nexus.bricks.search.federated_search import _strip_none_context
        from nexus.bricks.search.fusion import rrf_multi_fusion

        # Build two zones so rrf_multi_fusion runs (single-zone short-circuits).
        zone_a = [_make_result("a.md", 0.9, zone_id="zone_a")]
        zone_b = [_make_result("b.md", 0.8, zone_id="zone_b")]

        raw = rrf_multi_fusion(
            result_lists=[("zone_a", zone_a), ("zone_b", zone_b)],
            k=60,
            limit=10,
            id_key="zone_qualified_path",
        )
        assert any(d.get("context") is None for d in raw)
        stripped = [_strip_none_context(d) for d in raw]
        assert all("context" not in d for d in stripped)
