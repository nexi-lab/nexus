"""Characterization tests for DeploymentProfile.FULL (Issue #4132).

These lock the FULL contract that docs/deployment/full-profile.md cites.
FULL = LITE services + the full feature set, EXCLUDING federation
(federation is cloud = full ∪ {federation}).
"""

from nexus.contracts.deployment_profile import (
    DRIVER_GCS,
    DRIVER_GDRIVE,
    DRIVER_REMOTE,
    DRIVER_S3,
    SERVICE_ACCESS_MANIFEST,
    SERVICE_FEDERATION,
    SERVICE_LLM,
    SERVICE_MCP,
    SERVICE_PAY,
    SERVICE_SEARCH,
    SERVICE_SNAPSHOT,
    SERVICE_VERSIONING,
    SERVICE_WORKSPACE,
    DeploymentProfile,
)


class TestFullProfileContract:
    def test_enum_value(self) -> None:
        assert DeploymentProfile.FULL == "full"
        assert DeploymentProfile("full") is DeploymentProfile.FULL

    def test_superset_over_lite(self) -> None:
        full = DeploymentProfile.FULL.default_services()
        lite = DeploymentProfile.LITE.default_services()
        assert lite.issubset(full)

    def test_includes_feature_services(self) -> None:
        services = DeploymentProfile.FULL.default_services()
        for b in (
            SERVICE_SEARCH,
            SERVICE_PAY,
            SERVICE_LLM,
            SERVICE_MCP,
            SERVICE_WORKSPACE,
            SERVICE_SNAPSHOT,
            SERVICE_VERSIONING,
            SERVICE_ACCESS_MANIFEST,
        ):
            assert b in services, f"{b} must be enabled in FULL"

    def test_excludes_federation(self) -> None:
        # FULL excludes federation; CLOUD = FULL ∪ {federation}
        assert SERVICE_FEDERATION not in DeploymentProfile.FULL.default_services()
        assert SERVICE_FEDERATION in DeploymentProfile.CLOUD.default_services()

    def test_cloud_is_full_plus_federation(self) -> None:
        full = DeploymentProfile.FULL.default_services()
        cloud = DeploymentProfile.CLOUD.default_services()
        assert cloud == full | {SERVICE_FEDERATION}

    def test_drivers_include_cloud_storage(self) -> None:
        drivers = DeploymentProfile.FULL.default_drivers()
        for d in (DRIVER_S3, DRIVER_GCS, DRIVER_GDRIVE, DRIVER_REMOTE):
            assert d in drivers
