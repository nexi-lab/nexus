"""Tests for SearchDelegation credential (Issue #3147 Phase 2).

Tests method allowlist, zone validation, TTL expiry, and the
validate() convenience method.
"""

import time

import pytest

from nexus.contracts.search_delegation import (
    SEARCH_DELEGATION_METHODS,
    SearchDelegation,
)


class TestSearchDelegationMethods:
    def test_allowlist_contains_search(self) -> None:
        assert "search" in SEARCH_DELEGATION_METHODS

    def test_allowlist_contains_semantic_search(self) -> None:
        assert "semantic_search" in SEARCH_DELEGATION_METHODS

    def test_allowlist_rejects_write(self) -> None:
        assert "sys_write" not in SEARCH_DELEGATION_METHODS
        assert "sys_unlink" not in SEARCH_DELEGATION_METHODS
        assert "sys_rename" not in SEARCH_DELEGATION_METHODS


class TestSearchDelegation:
    def _make_delegation(self, **kwargs) -> SearchDelegation:
        defaults = {
            "delegation_id": "sd_123",
            "source_zone_id": "zone_origin",
            "target_zones": frozenset({"zone_a", "zone_b"}),
            "subject": ("user", "alice"),
            "ttl_seconds": 30,
        }
        defaults.update(kwargs)
        return SearchDelegation(**defaults)

    def test_is_method_permitted_search(self) -> None:
        sd = self._make_delegation()
        assert sd.is_method_permitted("search")
        assert sd.is_method_permitted("semantic_search")

    def test_is_method_rejected_write(self) -> None:
        sd = self._make_delegation()
        assert not sd.is_method_permitted("sys_write")
        assert not sd.is_method_permitted("sys_read")
        assert not sd.is_method_permitted("glob")

    def test_is_zone_permitted(self) -> None:
        sd = self._make_delegation()
        assert sd.is_zone_permitted("zone_a")
        assert sd.is_zone_permitted("zone_b")
        assert not sd.is_zone_permitted("zone_c")

    def test_not_expired(self) -> None:
        sd = self._make_delegation(ttl_seconds=30)
        assert not sd.is_expired()

    def test_expired(self) -> None:
        sd = self._make_delegation(
            created_at=time.monotonic() - 60,
            ttl_seconds=30,
        )
        assert sd.is_expired()

    def test_validate_success(self) -> None:
        sd = self._make_delegation()
        # Should not raise
        sd.validate("search", "zone_a")

    def test_validate_bad_method(self) -> None:
        sd = self._make_delegation()
        with pytest.raises(PermissionError, match="SearchDelegation permits only"):
            sd.validate("sys_write", "zone_a")

    def test_validate_bad_zone(self) -> None:
        sd = self._make_delegation()
        with pytest.raises(PermissionError, match="not in delegation scope"):
            sd.validate("search", "zone_unauthorized")

    def test_validate_expired(self) -> None:
        sd = self._make_delegation(
            created_at=time.monotonic() - 60,
            ttl_seconds=30,
        )
        with pytest.raises(PermissionError, match="expired"):
            sd.validate("search", "zone_a")

    def test_frozen(self) -> None:
        sd = self._make_delegation()
        with pytest.raises(AttributeError):
            sd.delegation_id = "changed"  # noqa: B003

    def test_expires_at(self) -> None:
        sd = self._make_delegation(ttl_seconds=30)
        assert sd.expires_at > sd.created_at
        assert sd.expires_at == sd.created_at + 30
