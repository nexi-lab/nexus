"""Tests for DeploymentProfile.SANDBOX (Issue #3778).

SANDBOX is the lightweight profile for agent sandboxes — boots with zero
external services (SQLite + in-mem LRU + BM25S), exposes only MCP +
/health + /api/v2/features.
"""

from nexus.contracts.deployment_profile import (
    BRICK_EVENTLOG,
    BRICK_LLM,
    BRICK_MCP,
    BRICK_NAMESPACE,
    BRICK_OBSERVABILITY,
    BRICK_PARSERS,
    BRICK_PAY,
    BRICK_PERMISSIONS,
    BRICK_SANDBOX,
    BRICK_SEARCH,
    BRICK_WORKFLOWS,
    DeploymentProfile,
)


class TestSandboxProfileEnum:
    def test_enum_value(self) -> None:
        assert DeploymentProfile.SANDBOX == "sandbox"
        assert DeploymentProfile("sandbox") is DeploymentProfile.SANDBOX

    def test_default_bricks_includes_core(self) -> None:
        bricks = DeploymentProfile.SANDBOX.default_bricks()
        assert BRICK_EVENTLOG in bricks
        assert BRICK_NAMESPACE in bricks
        assert BRICK_PERMISSIONS in bricks
        assert BRICK_SEARCH in bricks
        assert BRICK_MCP in bricks
        assert BRICK_PARSERS in bricks

    def test_default_bricks_excludes_heavy(self) -> None:
        bricks = DeploymentProfile.SANDBOX.default_bricks()
        assert BRICK_LLM not in bricks
        assert BRICK_PAY not in bricks
        assert BRICK_SANDBOX not in bricks  # sandbox provisioning brick
        assert BRICK_WORKFLOWS not in bricks
        assert BRICK_OBSERVABILITY not in bricks

    def test_sandbox_superset_of_lite(self) -> None:
        sandbox = DeploymentProfile.SANDBOX.default_bricks()
        lite = DeploymentProfile.LITE.default_bricks()
        assert lite.issubset(sandbox)

    def test_sandbox_subset_of_full(self) -> None:
        sandbox = DeploymentProfile.SANDBOX.default_bricks()
        full = DeploymentProfile.FULL.default_bricks()
        assert sandbox.issubset(full)

    def test_sandbox_size(self) -> None:
        """SANDBOX = LITE (7) + 3 adds (SEARCH, MCP, PARSERS) = 10 bricks."""
        assert len(DeploymentProfile.SANDBOX.default_bricks()) == 10


class TestSandboxTuning:
    def test_tuning_resolves(self) -> None:
        from nexus.lib.performance_tuning import resolve_profile_tuning

        tuning = resolve_profile_tuning(DeploymentProfile.SANDBOX)
        assert tuning is not None

    def test_tuning_is_small(self) -> None:
        """SANDBOX should have smaller pools than FULL."""
        from nexus.lib.performance_tuning import resolve_profile_tuning

        sandbox = resolve_profile_tuning(DeploymentProfile.SANDBOX)
        full = resolve_profile_tuning(DeploymentProfile.FULL)
        assert sandbox.concurrency.default_workers < full.concurrency.default_workers
        assert sandbox.storage.db_pool_size < full.storage.db_pool_size

    def test_tuning_disables_asyncpg_pool(self) -> None:
        """SANDBOX uses SQLite — no asyncpg pool."""
        from nexus.lib.performance_tuning import resolve_profile_tuning

        tuning = resolve_profile_tuning(DeploymentProfile.SANDBOX)
        assert tuning.pool.asyncpg_max_size == 0
