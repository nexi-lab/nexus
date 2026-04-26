"""Tests for optional brick import decoupling (Issue #3230).

Verifies that:
1. nexus.config can be imported without bricks.auth installed.
2. nexus.sdk can be imported without bricks.rebac installed.
3. Lazy SDK symbols give clear errors when their brick is absent.
4. Lazy SDK symbols work normally when their brick is present.
"""

from unittest.mock import patch

import pytest


class TestConfigWithoutAuthBrick:
    """Verify nexus.config works when bricks.auth is absent (#3230)."""

    def test_config_imports_oauth_from_contracts(self) -> None:
        """NexusConfig.oauth field uses OAuthConfig from contracts, not bricks."""
        from nexus.config import NexusConfig

        # The oauth field's annotation should reference the contracts type
        field_info = NexusConfig.model_fields["oauth"]
        assert field_info is not None

        # Verify we can create a config with oauth=None (no auth brick needed)
        cfg = NexusConfig(profile="embedded")
        assert cfg.oauth is None

    def test_config_parses_oauth_dict(self) -> None:
        """Config can parse oauth config from dict using contracts types."""
        from nexus.config import NexusConfig

        cfg = NexusConfig(
            profile="embedded",
            oauth={
                "providers": [
                    {
                        "name": "test",
                        "display_name": "Test Provider",
                        "provider_class": "test.TestProvider",
                        "client_id_env": "TEST_CLIENT_ID",
                        "client_secret_env": "TEST_CLIENT_SECRET",
                    }
                ]
            },
        )
        assert cfg.oauth is not None
        assert cfg.oauth.get_provider_config("test") is not None


class TestSDKWithoutReBACBrick:
    """Verify nexus.sdk lazy ReBAC imports (#3230)."""

    def test_sdk_contracts_types_always_available(self) -> None:
        """Entity, WILDCARD_SUBJECT, CheckResult, GraphLimitExceeded are always importable."""
        from nexus.sdk import (
            WILDCARD_SUBJECT,
            CheckResult,
            Entity,
            GraphLimitExceeded,
        )

        assert Entity is not None
        assert WILDCARD_SUBJECT == ("*", "*")
        assert CheckResult is not None
        assert GraphLimitExceeded is not None

    def test_sdk_lazy_symbols_work_when_brick_present(self) -> None:
        """ReBACManager, PermissionEnforcer, ReBACTuple load when brick is installed."""
        from nexus.sdk import PermissionEnforcer, ReBACManager, ReBACTuple

        assert ReBACManager is not None
        assert PermissionEnforcer is not None
        assert ReBACTuple is not None

    def test_sdk_lazy_rebac_manager_clear_error(self) -> None:
        """Accessing ReBACManager when brick absent gives ImportError, not AttributeError."""
        import nexus.sdk as sdk

        # Clear any cached lazy import
        sdk._lazy_imports_cache.pop("ReBACManager", None)

        with (
            patch.dict(
                "sys.modules",
                {"nexus.bricks.rebac.manager": None},
            ),
            pytest.raises(ImportError, match="requires the ReBAC brick"),
        ):
            sdk.__getattr__("ReBACManager")

    def test_sdk_lazy_permission_enforcer_clear_error(self) -> None:
        """Accessing PermissionEnforcer when brick absent gives ImportError."""
        import nexus.sdk as sdk

        sdk._lazy_imports_cache.pop("PermissionEnforcer", None)

        with (
            patch.dict(
                "sys.modules",
                {"nexus.bricks.rebac.enforcer": None},
            ),
            pytest.raises(ImportError, match="requires the ReBAC brick"),
        ):
            sdk.__getattr__("PermissionEnforcer")

    def test_sdk_lazy_rebac_tuple_clear_error(self) -> None:
        """Accessing ReBACTuple when brick absent gives ImportError."""
        import nexus.sdk as sdk

        sdk._lazy_imports_cache.pop("ReBACTuple", None)

        with (
            patch.dict(
                "sys.modules",
                {"nexus.bricks.rebac.domain": None},
            ),
            pytest.raises(ImportError, match="requires the ReBAC brick"),
        ):
            sdk.__getattr__("ReBACTuple")

    def test_sdk_unknown_attr_raises_attribute_error(self) -> None:
        """Accessing a truly nonexistent attribute raises AttributeError."""
        import nexus.sdk as sdk

        with pytest.raises(AttributeError, match="has no attribute"):
            sdk.__getattr__("NonexistentThing")

    def test_sdk_dir_includes_lazy_symbols(self) -> None:
        """dir(nexus.sdk) includes lazy ReBAC symbols for discoverability."""
        import nexus.sdk as sdk

        attrs = dir(sdk)
        assert "ReBACManager" in attrs
        assert "PermissionEnforcer" in attrs
        assert "ReBACTuple" in attrs

    def test_sdk_lazy_import_caching(self) -> None:
        """Lazy imports are cached after first access."""
        import nexus.sdk as sdk

        # Clear cache
        sdk._lazy_imports_cache.pop("ReBACManager", None)

        # First access populates cache
        manager1 = sdk.ReBACManager
        assert "ReBACManager" in sdk._lazy_imports_cache

        # Second access returns same object
        manager2 = sdk.ReBACManager
        assert manager1 is manager2
