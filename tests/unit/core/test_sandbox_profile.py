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
