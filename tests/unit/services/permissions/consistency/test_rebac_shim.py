"""Verify backward-compatibility shims for consistency module (Issue #2179).

After extraction to ``nexus.bricks.rebac.consistency``, both
``nexus.bricks.rebac.consistency`` and ``nexus.services.permissions.consistency``
should resolve to the canonical brick implementation.
"""

from __future__ import annotations


class TestShimReExports:
    """Verify all public symbols are accessible via old import paths."""

    def test_zone_isolation_error_resolves_from_rebac(self):
        from nexus.bricks.rebac.consistency.zone_manager import ZoneIsolationError

        assert ZoneIsolationError.__module__ == "nexus.bricks.rebac.consistency.zone_manager"

    def test_zone_isolation_error_resolves_from_services(self):
        from nexus.services.permissions.consistency.zone_manager import ZoneIsolationError

        assert ZoneIsolationError.__module__ == "nexus.bricks.rebac.consistency.zone_manager"

    def test_zone_manager_resolves(self):
        from nexus.bricks.rebac.consistency.zone_manager import ZoneManager

        assert ZoneManager.__module__ == "nexus.bricks.rebac.consistency.zone_manager"

    def test_increment_version_token_resolves(self):
        from nexus.bricks.rebac.consistency.revision import increment_version_token

        assert increment_version_token.__module__ == "nexus.bricks.rebac.consistency.revision"

    def test_get_zone_revision_for_grant_resolves(self):
        from nexus.bricks.rebac.consistency.revision import get_zone_revision_for_grant

        assert get_zone_revision_for_grant.__module__ == "nexus.bricks.rebac.consistency.revision"

    def test_package_init_exports_all(self):
        """Verify the package __init__.py re-exports all 4 symbols."""
        from nexus.bricks.rebac.consistency import (
            ZoneIsolationError,
            ZoneManager,
            get_zone_revision_for_grant,
            increment_version_token,
        )

        # All should be the canonical implementations from bricks
        assert ZoneIsolationError.__module__ == "nexus.bricks.rebac.consistency.zone_manager"
        assert ZoneManager.__module__ == "nexus.bricks.rebac.consistency.zone_manager"
        assert get_zone_revision_for_grant.__module__ == "nexus.bricks.rebac.consistency.revision"
        assert increment_version_token.__module__ == "nexus.bricks.rebac.consistency.revision"

    def test_canonical_import_works(self):
        """Verify direct import from canonical location."""
        from nexus.bricks.rebac.consistency import (
            ZoneIsolationError,
            ZoneManager,
            get_zone_revision_for_grant,
            increment_version_token,
        )

        assert ZoneIsolationError is not None
        assert ZoneManager is not None
        assert callable(get_zone_revision_for_grant)
        assert callable(increment_version_token)
