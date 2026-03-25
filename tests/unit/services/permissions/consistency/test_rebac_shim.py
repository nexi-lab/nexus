"""Verify canonical ReBAC consistency imports (Issue #2179).

After consolidation, all imports come from ``nexus.bricks.rebac.consistency``.
"""

import pytest

pytest.importorskip("pyroaring")


class TestCanonicalImports:
    """Verify all public symbols are accessible from bricks.rebac.consistency."""

    def test_zone_isolation_error_resolves(self):
        from nexus.bricks.rebac.consistency.zone_manager import ZoneIsolationError

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

        assert ZoneIsolationError.__module__ == "nexus.bricks.rebac.consistency.zone_manager"
        assert ZoneManager.__module__ == "nexus.bricks.rebac.consistency.zone_manager"
        assert get_zone_revision_for_grant.__module__ == "nexus.bricks.rebac.consistency.revision"
        assert increment_version_token.__module__ == "nexus.bricks.rebac.consistency.revision"
