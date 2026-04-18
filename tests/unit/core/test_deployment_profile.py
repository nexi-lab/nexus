"""Tests for DeploymentProfile enum and brick resolution.

Issue #1389: Feature flags for deployment modes (full/lite/embedded).

Tests cover:
- Enum values and string representation
- Default brick sets per profile
- Superset hierarchy (embedded ⊂ lite ⊂ full ⊆ cloud)
- Override behavior (explicit overrides win with warning)
- Invalid override detection
"""

import logging

import pytest

from nexus.contracts.deployment_profile import (
    ALL_BRICK_NAMES,
    BRICK_CACHE,
    BRICK_EVENTLOG,
    BRICK_FEDERATION,
    BRICK_IPC,
    BRICK_LLM,
    BRICK_NAMESPACE,
    BRICK_PAY,
    BRICK_PERMISSIONS,
    BRICK_SANDBOX,
    BRICK_SEARCH,
    BRICK_WORKFLOWS,
    DeploymentProfile,
    resolve_enabled_bricks,
)


class TestDeploymentProfileEnum:
    """Tests for DeploymentProfile enum values."""

    def test_enum_values(self) -> None:
        assert DeploymentProfile.SLIM == "slim"
        assert DeploymentProfile.EMBEDDED == "embedded"
        assert DeploymentProfile.LITE == "lite"
        assert DeploymentProfile.FULL == "full"
        assert DeploymentProfile.CLOUD == "cloud"
        assert DeploymentProfile.REMOTE == "remote"

    def test_enum_from_string(self) -> None:
        assert DeploymentProfile("embedded") is DeploymentProfile.EMBEDDED
        assert DeploymentProfile("full") is DeploymentProfile.FULL

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            DeploymentProfile("invalid")

    def test_all_profiles_have_brick_mappings(self) -> None:
        for profile in DeploymentProfile:
            bricks = profile.default_bricks()
            assert isinstance(bricks, frozenset)
            # SLIM and REMOTE have zero bricks (kernel-only / NFS-client model)
            if profile not in (DeploymentProfile.SLIM, DeploymentProfile.REMOTE):
                assert len(bricks) > 0


class TestDefaultBrickSets:
    """Tests for per-profile default brick sets."""

    def test_cluster_minimal_multinode(self) -> None:
        bricks = DeploymentProfile.CLUSTER.default_bricks()
        assert BRICK_IPC in bricks
        assert BRICK_FEDERATION in bricks
        assert BRICK_EVENTLOG not in bricks  # No audit/events
        assert len(bricks) == 2

    def test_embedded_minimal(self) -> None:
        bricks = DeploymentProfile.EMBEDDED.default_bricks()
        assert BRICK_EVENTLOG in bricks
        assert len(bricks) == 1

    def test_lite_includes_core_services(self) -> None:
        bricks = DeploymentProfile.LITE.default_bricks()
        assert BRICK_EVENTLOG in bricks
        assert BRICK_NAMESPACE in bricks
        assert BRICK_PERMISSIONS in bricks
        assert BRICK_CACHE in bricks
        # Should NOT include heavy bricks
        assert BRICK_SEARCH not in bricks
        assert BRICK_PAY not in bricks
        assert BRICK_LLM not in bricks
        assert BRICK_SANDBOX not in bricks

    def test_full_includes_all_except_federation(self) -> None:
        bricks = DeploymentProfile.FULL.default_bricks()
        assert BRICK_SEARCH in bricks
        assert BRICK_PAY in bricks
        assert BRICK_LLM in bricks
        assert BRICK_SANDBOX in bricks
        assert BRICK_WORKFLOWS in bricks
        # Federation is a system service (not a brick), auto-detected from ZoneManager

    def test_cloud_is_superset_of_full(self) -> None:
        cloud = DeploymentProfile.CLOUD.default_bricks()
        full = DeploymentProfile.FULL.default_bricks()
        assert full.issubset(cloud)

    def test_full_is_superset_of_lite(self) -> None:
        full = DeploymentProfile.FULL.default_bricks()
        lite = DeploymentProfile.LITE.default_bricks()
        assert lite.issubset(full)

    def test_lite_is_superset_of_embedded(self) -> None:
        lite = DeploymentProfile.LITE.default_bricks()
        embedded = DeploymentProfile.EMBEDDED.default_bricks()
        assert embedded.issubset(lite)

    def test_hierarchy_chain(self) -> None:
        """embedded ⊂ lite ⊂ full ⊆ cloud."""
        embedded = DeploymentProfile.EMBEDDED.default_bricks()
        lite = DeploymentProfile.LITE.default_bricks()
        full = DeploymentProfile.FULL.default_bricks()
        cloud = DeploymentProfile.CLOUD.default_bricks()

        assert embedded < lite < full <= cloud

    def test_is_brick_enabled(self) -> None:
        assert DeploymentProfile.FULL.is_brick_enabled(BRICK_SEARCH)
        assert not DeploymentProfile.LITE.is_brick_enabled(BRICK_SEARCH)
        assert not DeploymentProfile.EMBEDDED.is_brick_enabled(BRICK_NAMESPACE)


class TestResolveEnabledBricks:
    """Tests for override resolution logic."""

    def test_no_overrides_returns_defaults(self) -> None:
        result = resolve_enabled_bricks(DeploymentProfile.FULL)
        assert result == DeploymentProfile.FULL.default_bricks()

    def test_empty_overrides_returns_defaults(self) -> None:
        result = resolve_enabled_bricks(DeploymentProfile.FULL, overrides={})
        assert result == DeploymentProfile.FULL.default_bricks()

    def test_override_enables_brick(self) -> None:
        """Enable search in lite profile (not enabled by default)."""
        result = resolve_enabled_bricks(
            DeploymentProfile.LITE,
            overrides={BRICK_SEARCH: True},
        )
        assert BRICK_SEARCH in result

    def test_override_disables_brick(self) -> None:
        """Disable search in full profile (enabled by default)."""
        result = resolve_enabled_bricks(
            DeploymentProfile.FULL,
            overrides={BRICK_SEARCH: False},
        )
        assert BRICK_SEARCH not in result

    def test_override_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Overrides that differ from profile default should log a warning."""
        with caplog.at_level(logging.WARNING):
            resolve_enabled_bricks(
                DeploymentProfile.LITE,
                overrides={BRICK_SEARCH: True},
            )
        assert "enabling" in caplog.text.lower()
        assert BRICK_SEARCH in caplog.text

    def test_override_matching_default_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Override matching the default should NOT warn."""
        with caplog.at_level(logging.WARNING):
            resolve_enabled_bricks(
                DeploymentProfile.FULL,
                overrides={BRICK_SEARCH: True},
            )
        assert caplog.text == ""

    def test_unknown_brick_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown brick names"):
            resolve_enabled_bricks(
                DeploymentProfile.FULL,
                overrides={"nonexistent_brick": True},
            )

    def test_multiple_overrides(self) -> None:
        result = resolve_enabled_bricks(
            DeploymentProfile.LITE,
            overrides={BRICK_SEARCH: True, BRICK_PAY: True, BRICK_CACHE: False},
        )
        assert BRICK_SEARCH in result
        assert BRICK_PAY in result
        assert BRICK_CACHE not in result

    def test_result_is_frozen(self) -> None:
        result = resolve_enabled_bricks(DeploymentProfile.FULL)
        assert isinstance(result, frozenset)

    def test_all_brick_names_comprehensive(self) -> None:
        """ALL_BRICK_NAMES should include every brick from every profile."""
        all_from_profiles: set[str] = set()
        for profile in DeploymentProfile:
            all_from_profiles |= profile.default_bricks()
        assert all_from_profiles.issubset(ALL_BRICK_NAMES)


class TestFeaturesConfigOverrides:
    """Tests for FeaturesConfig.to_overrides() integration."""

    def test_none_fields_produce_empty_overrides(self) -> None:
        from nexus.config import FeaturesConfig

        fc = FeaturesConfig()
        assert fc.to_overrides() == {}

    def test_explicit_fields_produce_overrides(self) -> None:
        from nexus.config import FeaturesConfig

        fc = FeaturesConfig(search=True, pay=False)
        overrides = fc.to_overrides()
        assert overrides == {"search": True, "pay": False}

    def test_semantic_search_is_config_only_not_brick_override(self) -> None:
        from nexus.config import FeaturesConfig

        fc = FeaturesConfig(search=True, semantic_search=True)
        overrides = fc.to_overrides()
        assert overrides == {"search": True}

    def test_overrides_integrate_with_resolve(self) -> None:
        from nexus.config import FeaturesConfig

        fc = FeaturesConfig(search=True)
        result = resolve_enabled_bricks(
            DeploymentProfile.LITE,
            overrides=fc.to_overrides(),
        )
        assert BRICK_SEARCH in result


class TestNexusConfigProfile:
    """Tests for profile field in NexusConfig."""

    def test_default_profile_is_full(self) -> None:
        from nexus.config import NexusConfig

        cfg = NexusConfig()
        assert cfg.profile == "full"

    def test_valid_profiles(self) -> None:
        from nexus.config import NexusConfig

        for p in ["slim", "embedded", "lite", "sandbox", "full", "cloud"]:
            cfg = NexusConfig(profile=p)
            assert cfg.profile == p
        # "remote" requires url
        cfg = NexusConfig(profile="remote", url="grpc://localhost:50051")
        assert cfg.profile == "remote"

    def test_invalid_profile_raises(self) -> None:
        from nexus.config import NexusConfig

        with pytest.raises(ValueError, match="profile must be one of"):
            NexusConfig(profile="invalid")

    def test_semantic_search_feature_flag_is_accepted(self) -> None:
        from nexus.config import NexusConfig

        cfg = NexusConfig(features={"semantic_search": True, "search": True})
        assert cfg.features.semantic_search is True
        assert cfg.features.search is True
