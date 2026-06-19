"""Characterization tests for DeploymentProfile.FULL (Issue #4132).

These lock the FULL contract that docs/deployment/full-profile.md cites.
FULL = LITE bricks + the full feature set, EXCLUDING federation
(federation is cloud = full ∪ {federation}).
"""

from nexus.contracts.deployment_profile import (
    BRICK_ACCESS_MANIFEST,
    BRICK_FEDERATION,
    BRICK_LLM,
    BRICK_MCP,
    BRICK_PAY,
    BRICK_SEARCH,
    BRICK_SNAPSHOT,
    BRICK_VERSIONING,
    BRICK_WORKSPACE,
    DRIVER_GCS,
    DRIVER_GDRIVE,
    DRIVER_REMOTE,
    DRIVER_S3,
    DeploymentProfile,
)


class TestFullProfileContract:
    def test_enum_value(self) -> None:
        assert DeploymentProfile.FULL == "full"
        assert DeploymentProfile("full") is DeploymentProfile.FULL

    def test_superset_over_lite(self) -> None:
        full = DeploymentProfile.FULL.default_bricks()
        lite = DeploymentProfile.LITE.default_bricks()
        assert lite.issubset(full)

    def test_includes_feature_bricks(self) -> None:
        bricks = DeploymentProfile.FULL.default_bricks()
        for b in (
            BRICK_SEARCH,
            BRICK_PAY,
            BRICK_LLM,
            BRICK_MCP,
            BRICK_WORKSPACE,
            BRICK_SNAPSHOT,
            BRICK_VERSIONING,
            BRICK_ACCESS_MANIFEST,
        ):
            assert b in bricks, f"{b} must be enabled in FULL"

    def test_excludes_federation(self) -> None:
        # FULL excludes federation; CLOUD = FULL ∪ {federation}
        assert BRICK_FEDERATION not in DeploymentProfile.FULL.default_bricks()
        assert BRICK_FEDERATION in DeploymentProfile.CLOUD.default_bricks()

    def test_cloud_is_full_plus_federation(self) -> None:
        full = DeploymentProfile.FULL.default_bricks()
        cloud = DeploymentProfile.CLOUD.default_bricks()
        assert cloud == full | {BRICK_FEDERATION}

    def test_drivers_include_cloud_storage(self) -> None:
        drivers = DeploymentProfile.FULL.default_drivers()
        for d in (DRIVER_S3, DRIVER_GCS, DRIVER_GDRIVE, DRIVER_REMOTE):
            assert d in drivers
