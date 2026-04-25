"""Tests for revision helpers — version tokens and zone revision lookups.

Covers:
- increment_version_token with MetastoreVersionStore
- increment_version_token monotonic increments
- get_zone_revision_for_grant returns 0 for missing zone
- get_zone_revision_for_grant returns revision when present

Related: Issue #1459 (decomposition), P0-1 (consistency levels)
Issue #191: Migrated from SQLAlchemy ORM to MetastoreABC.
"""

import pytest

pytest.importorskip("pyroaring")


from nexus.bricks.rebac.consistency.metastore_version_store import MetastoreVersionStore
from nexus.bricks.rebac.consistency.revision import (
    get_zone_revision_for_grant,
    increment_version_token,
)
from nexus.contracts.constants import ROOT_ZONE_ID
from tests.helpers.inmemory_nexus_fs import InMemoryNexusFS


@pytest.fixture
def version_store():
    """Create an in-memory MetastoreVersionStore."""
    return MetastoreVersionStore(InMemoryNexusFS())


class TestIncrementVersionToken:
    """Test increment_version_token function."""

    def test_first_call_returns_v1(self, version_store):
        token = increment_version_token(version_store, zone_id=ROOT_ZONE_ID)
        assert token == "v1"

    def test_second_call_returns_v2(self, version_store):
        increment_version_token(version_store, zone_id=ROOT_ZONE_ID)
        token = increment_version_token(version_store, zone_id=ROOT_ZONE_ID)
        assert token == "v2"

    def test_monotonic_increments(self, version_store):
        tokens = []
        for _ in range(5):
            tokens.append(increment_version_token(version_store, zone_id="test_zone"))
        assert tokens == ["v1", "v2", "v3", "v4", "v5"]

    def test_different_zones_independent(self, version_store):
        t1 = increment_version_token(version_store, zone_id="zone_a")
        t2 = increment_version_token(version_store, zone_id="zone_b")
        t3 = increment_version_token(version_store, zone_id="zone_a")
        assert t1 == "v1"
        assert t2 == "v1"
        assert t3 == "v2"

    def test_default_zone_id(self, version_store):
        token = increment_version_token(version_store)
        assert token == "v1"


class TestGetZoneRevisionForGrant:
    """Test get_zone_revision_for_grant function."""

    def test_missing_zone_returns_zero(self, version_store):
        revision = get_zone_revision_for_grant(version_store, zone_id="nonexistent")
        assert revision == 0

    def test_existing_zone_returns_revision(self, version_store):
        # Seed revisions
        for _ in range(42):
            version_store.increment_version("org_acme")

        revision = get_zone_revision_for_grant(version_store, zone_id="org_acme")
        assert revision == 42

    def test_returns_int(self, version_store):
        for _ in range(7):
            version_store.increment_version("test_zone")

        result = get_zone_revision_for_grant(version_store, zone_id="test_zone")
        assert isinstance(result, int)
        assert result == 7
