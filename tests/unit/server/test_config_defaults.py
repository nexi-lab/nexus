"""Tests for security-sensitive config defaults (Issue #3063).

Verifies that NexusConfig defaults are secure-by-default and that
values propagate correctly to downstream components.
"""

from nexus.config import NexusConfig


class TestSecurityDefaults:
    """Verify that security-sensitive defaults are restrictive."""

    def test_allow_admin_bypass_defaults_false(self) -> None:
        """Issue #3063 §3: admin bypass must default to False (secure-by-default)."""
        config = NexusConfig()
        assert config.allow_admin_bypass is False

    def test_enforce_permissions_defaults_true(self) -> None:
        config = NexusConfig()
        assert config.enforce_permissions is True

    def test_enforce_zone_isolation_defaults_true(self) -> None:
        config = NexusConfig()
        assert config.enforce_zone_isolation is True

    def test_allow_admin_bypass_explicit_true(self) -> None:
        """Opt-in to bypass must be explicit."""
        config = NexusConfig(allow_admin_bypass=True)
        assert config.allow_admin_bypass is True

    def test_allow_admin_bypass_explicit_false(self) -> None:
        config = NexusConfig(allow_admin_bypass=False)
        assert config.allow_admin_bypass is False
