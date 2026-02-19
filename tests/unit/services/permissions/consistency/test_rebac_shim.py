"""Verify the rebac.consistency backward-compatibility shim re-exports all symbols.

Issue #2074: After deduplication, ``nexus.rebac.consistency`` is a thin re-export
shim pointing to ``nexus.services.permissions.consistency``. This test ensures
existing imports from the old path continue to resolve correctly.
"""

from __future__ import annotations


class TestShimReExports:
    """Verify all 4 public symbols are accessible via the old rebac path."""

    def test_zone_isolation_error_resolves(self):
        from nexus.rebac.consistency.zone_manager import ZoneIsolationError
        from nexus.services.permissions.consistency.zone_manager import (
            ZoneIsolationError as Canonical,
        )

        assert ZoneIsolationError is Canonical

    def test_zone_manager_resolves(self):
        from nexus.rebac.consistency.zone_manager import ZoneManager
        from nexus.services.permissions.consistency.zone_manager import (
            ZoneManager as Canonical,
        )

        assert ZoneManager is Canonical

    def test_increment_version_token_resolves(self):
        from nexus.rebac.consistency.revision import increment_version_token
        from nexus.services.permissions.consistency.revision import (
            increment_version_token as Canonical,
        )

        assert increment_version_token is Canonical

    def test_get_zone_revision_for_grant_resolves(self):
        from nexus.rebac.consistency.revision import get_zone_revision_for_grant
        from nexus.services.permissions.consistency.revision import (
            get_zone_revision_for_grant as Canonical,
        )

        assert get_zone_revision_for_grant is Canonical

    def test_package_init_exports_all(self):
        """Verify the package __init__.py re-exports all 4 symbols."""
        from nexus.rebac.consistency import (
            ZoneIsolationError,
            ZoneManager,
            get_zone_revision_for_grant,
            increment_version_token,
        )

        # All should be the canonical implementations
        from nexus.services.permissions.consistency import (
            ZoneIsolationError as CE,
        )
        from nexus.services.permissions.consistency import (
            ZoneManager as CZ,
        )
        from nexus.services.permissions.consistency import (
            get_zone_revision_for_grant as CG,
        )
        from nexus.services.permissions.consistency import (
            increment_version_token as CI,
        )

        assert ZoneIsolationError is CE
        assert ZoneManager is CZ
        assert get_zone_revision_for_grant is CG
        assert increment_version_token is CI
