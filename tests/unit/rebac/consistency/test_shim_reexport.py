"""Verify rebac.consistency re-exports point to the canonical location.

After Issue #2074, the canonical implementation lives in
nexus.services.permissions.consistency. The rebac.consistency module
is a backward-compat shim that re-exports all public symbols.

This test ensures the shims are wired correctly — both import paths
must resolve to the exact same objects (identity, not just equality).
"""

from __future__ import annotations

from nexus.rebac.consistency import (
    ZoneIsolationError as ReBACZoneIsolationError,
)
from nexus.rebac.consistency import (
    ZoneManager as ReBACZoneManager,
)
from nexus.rebac.consistency import (
    get_zone_revision_for_grant as rebac_get_zone_revision,
)
from nexus.rebac.consistency import (
    increment_version_token as rebac_increment_version,
)
from nexus.rebac.consistency.revision import (
    get_zone_revision_for_grant as rebac_rev_get_zone_revision,
)
from nexus.rebac.consistency.revision import (
    increment_version_token as rebac_rev_increment_version,
)
from nexus.rebac.consistency.zone_manager import (
    ZoneIsolationError as ReBACZMZoneIsolationError,
)
from nexus.rebac.consistency.zone_manager import (
    ZoneManager as ReBACZMZoneManager,
)
from nexus.services.permissions.consistency import (
    ZoneIsolationError as CanonicalZoneIsolationError,
)
from nexus.services.permissions.consistency import (
    ZoneManager as CanonicalZoneManager,
)
from nexus.services.permissions.consistency import (
    get_zone_revision_for_grant as canonical_get_zone_revision,
)
from nexus.services.permissions.consistency import (
    increment_version_token as canonical_increment_version,
)


class TestConsistencyShimReexports:
    """Verify all rebac.consistency re-exports are identity-identical to canonical."""

    def test_zone_manager_is_same_class(self):
        assert ReBACZoneManager is CanonicalZoneManager

    def test_zone_isolation_error_is_same_class(self):
        assert ReBACZoneIsolationError is CanonicalZoneIsolationError

    def test_increment_version_token_is_same_function(self):
        assert rebac_increment_version is canonical_increment_version

    def test_get_zone_revision_for_grant_is_same_function(self):
        assert rebac_get_zone_revision is canonical_get_zone_revision

    def test_submodule_revision_reexports(self):
        assert rebac_rev_increment_version is canonical_increment_version
        assert rebac_rev_get_zone_revision is canonical_get_zone_revision

    def test_submodule_zone_manager_reexports(self):
        assert ReBACZMZoneManager is CanonicalZoneManager
        assert ReBACZMZoneIsolationError is CanonicalZoneIsolationError
