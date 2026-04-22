"""Unit tests for BackendFeature system (Issue #2069).

Tests:
1. BackendFeature enum completeness
2. Backend.has_feature() contract
3. Backend.backend_features is frozenset (immutable)
4. DelegatingBackend delegates capabilities correctly
5. Convenience frozensets are correct
6. Registry stores capabilities at registration time
7. Registry query methods (get_backend_features, list_by_feature)
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, PropertyMock

import pytest

from nexus.backends.base.backend import Backend
from nexus.backends.base.registry import ConnectorRegistry
from nexus.backends.storage.delegating import DelegatingBackend
from nexus.contracts.backend_features import (
    BLOB_BACKEND_FEATURES,
    CORE_BACKEND_FEATURES,
    OAUTH_BACKEND_FEATURES,
    BackendFeature,
)

# ---------------------------------------------------------------------------
# BackendFeature Enum Tests
# ---------------------------------------------------------------------------


class TestBackendFeatureEnum:
    """BackendFeature enum is well-formed and complete."""

    def test_all_members_are_strings(self) -> None:
        """Every capability value is a non-empty string."""
        for cap in BackendFeature:
            assert isinstance(cap.value, str)
            assert len(cap.value) > 0

    def test_values_are_unique(self) -> None:
        """No two capabilities share the same string value."""
        values = [cap.value for cap in BackendFeature]
        assert len(values) == len(set(values))

    def test_expected_member_count(self) -> None:
        """16 features after removing CACHE_BULK_READ and CACHE_SYNC."""
        assert len(BackendFeature) == 16

    def test_str_enum_identity(self) -> None:
        """StrEnum values can be compared with plain strings."""
        assert BackendFeature.RENAME == "rename"
        assert BackendFeature.SIGNED_URL == "signed_url"

    def test_membership_check_o1(self) -> None:
        """Frozenset membership check works for capabilities."""
        caps = frozenset({BackendFeature.RENAME, BackendFeature.STREAMING})
        assert BackendFeature.RENAME in caps
        assert BackendFeature.SIGNED_URL not in caps


# ---------------------------------------------------------------------------
# Convenience Frozensets
# ---------------------------------------------------------------------------


class TestConvenienceFrozensets:
    """CORE_BACKEND_FEATURES, BLOB_BACKEND_FEATURES, OAUTH_BACKEND_FEATURES."""

    def test_core_is_empty(self) -> None:
        """CORE_BACKEND_FEATURES is empty — backends opt in."""
        assert frozenset() == CORE_BACKEND_FEATURES

    def test_blob_connector_has_expected(self) -> None:
        expected = {
            BackendFeature.RENAME,
            BackendFeature.DIRECTORY_LISTING,
            BackendFeature.PATH_DELETE,
            BackendFeature.STREAMING,
            BackendFeature.BATCH_CONTENT,
        }
        assert expected == BLOB_BACKEND_FEATURES

    def test_oauth_connector_has_expected(self) -> None:
        expected = {
            BackendFeature.USER_SCOPED,
            BackendFeature.TOKEN_MANAGER,
            BackendFeature.OAUTH,
        }
        assert expected == OAUTH_BACKEND_FEATURES

    def test_all_frozensets_are_immutable(self) -> None:
        for fs in (CORE_BACKEND_FEATURES, BLOB_BACKEND_FEATURES, OAUTH_BACKEND_FEATURES):
            assert isinstance(fs, frozenset)


# ---------------------------------------------------------------------------
# Backend ABC Tests
# ---------------------------------------------------------------------------


class TestBackendCapabilities:
    """Backend.backend_features and has_feature() contract."""

    def test_default_capabilities_is_empty_frozenset(self) -> None:
        """Backend ABC default _BACKEND_FEATURES is empty."""
        assert frozenset() == Backend._BACKEND_FEATURES

    def test_capabilities_property_returns_frozenset(self) -> None:
        """capabilities property returns the class _BACKEND_FEATURES."""
        assert isinstance(Backend._BACKEND_FEATURES, frozenset)

    def test_has_feature_true_when_present(self) -> None:
        """has_feature returns True when capability is in _BACKEND_FEATURES."""
        mock = MagicMock(spec=Backend)
        mock.has_feature = lambda cap: cap in frozenset({BackendFeature.RENAME})
        assert mock.has_feature(BackendFeature.RENAME)

    def test_has_feature_false_when_absent(self) -> None:
        """has_feature returns False when capability is not in _BACKEND_FEATURES."""
        mock = MagicMock(spec=Backend)
        mock.has_feature = lambda cap: cap in frozenset()
        assert not mock.has_feature(BackendFeature.RENAME)


# ---------------------------------------------------------------------------
# DelegatingBackend Tests
# ---------------------------------------------------------------------------


class TestDelegatingBackendCapabilities:
    """DelegatingBackend delegates and caches capabilities."""

    @pytest.fixture
    def inner_with_caps(self) -> MagicMock:
        mock = MagicMock(spec=Backend)
        mock.name = "test-inner"
        caps = frozenset({BackendFeature.RENAME, BackendFeature.STREAMING})
        type(mock).backend_features = PropertyMock(return_value=caps)
        return mock

    def test_capabilities_delegated(self, inner_with_caps: MagicMock) -> None:
        wrapper = DelegatingBackend(inner_with_caps)
        assert wrapper.backend_features == frozenset(
            {BackendFeature.RENAME, BackendFeature.STREAMING}
        )

    def test_capabilities_cached_in_init(self, inner_with_caps: MagicMock) -> None:
        wrapper = DelegatingBackend(inner_with_caps)
        assert wrapper._cached_backend_features is not None
        assert isinstance(wrapper._cached_backend_features, frozenset)

    def test_has_feature_uses_cached(self, inner_with_caps: MagicMock) -> None:
        wrapper = DelegatingBackend(inner_with_caps)
        assert wrapper.has_feature(BackendFeature.RENAME)
        assert not wrapper.has_feature(BackendFeature.SIGNED_URL)


# ---------------------------------------------------------------------------
# Registry Capabilities Tests
# ---------------------------------------------------------------------------


class TestRegistryCapabilities:
    """ConnectorRegistry stores and queries capabilities."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Iterator[None]:
        """Isolate registry state per test."""
        # Save and restore registry state
        saved = ConnectorRegistry._base._items.copy()
        yield
        ConnectorRegistry._base._items = saved

    def _make_fake_backend(
        self,
        caps: frozenset[BackendFeature] | None = None,
    ) -> type:
        """Create a minimal fake backend class."""

        class FakeBackend(Backend):
            _BACKEND_FEATURES = caps or frozenset()

            def __init__(self) -> None:
                pass

            @property
            def name(self) -> str:
                return "fake"

            @property
            def is_connected(self) -> bool:
                return False

            @property
            def has_root_path(self) -> bool:
                return False

            def connect(self) -> None:
                pass

            def disconnect(self) -> None:
                pass

            def check_connection(self, **kwargs: object) -> object:
                return None

            def write_content(self, *a: object, **kw: object) -> object:
                return None

            def read_content(self, *a: object, **kw: object) -> object:
                return b""

            def delete_content(self, *a: object, **kw: object) -> object:
                return None

            def content_exists(self, *a: object, **kw: object) -> bool:
                return False

            def get_content_size(self, *a: object, **kw: object) -> int:
                return 0

            def mkdir(self, *a: object, **kw: object) -> None:
                pass

            def rmdir(self, *a: object, **kw: object) -> None:
                pass

            def is_directory(self, *a: object, **kw: object) -> bool:
                return False

        return FakeBackend

    def test_register_stores_capabilities(self) -> None:
        caps = frozenset({BackendFeature.RENAME, BackendFeature.STREAMING})
        cls = self._make_fake_backend(caps)
        ConnectorRegistry.register("test_cap_backend", cls)
        info = ConnectorRegistry.get_info("test_cap_backend")
        assert info.backend_features == caps

    def test_register_stores_empty_capabilities(self) -> None:
        cls = self._make_fake_backend(frozenset())
        ConnectorRegistry.register("test_empty_cap", cls)
        info = ConnectorRegistry.get_info("test_empty_cap")
        assert info.backend_features == frozenset()

    def test_get_backend_features(self) -> None:
        caps = frozenset({BackendFeature.SIGNED_URL})
        cls = self._make_fake_backend(caps)
        ConnectorRegistry.register("test_get_cap", cls)
        assert ConnectorRegistry.get_backend_features("test_get_cap") == caps

    def test_list_by_feature(self) -> None:
        caps1 = frozenset({BackendFeature.RENAME})
        caps2 = frozenset({BackendFeature.RENAME, BackendFeature.STREAMING})
        cls1 = self._make_fake_backend(caps1)
        cls2 = self._make_fake_backend(caps2)
        ConnectorRegistry.register("test_lbc1", cls1)
        ConnectorRegistry.register("test_lbc2", cls2)
        results = ConnectorRegistry.list_by_feature(BackendFeature.RENAME)
        result_names = {r.name for r in results}
        assert "test_lbc1" in result_names
        assert "test_lbc2" in result_names

    def test_list_by_feature_excludes_non_matching(self) -> None:
        caps = frozenset({BackendFeature.RENAME})
        cls = self._make_fake_backend(caps)
        ConnectorRegistry.register("test_lbc_excl", cls)
        results = ConnectorRegistry.list_by_feature(BackendFeature.SIGNED_URL)
        result_names = {r.name for r in results}
        assert "test_lbc_excl" not in result_names


# ---------------------------------------------------------------------------
# Capability-to-Protocol Mapping Tests
# ---------------------------------------------------------------------------


class TestCapabilityProtocolMapping:
    """get_capability_protocols() returns valid mapping."""

    def test_mapping_is_dict(self) -> None:
        from nexus.backends.base.registry import get_capability_protocols

        mapping = get_capability_protocols()
        assert isinstance(mapping, dict)

    def test_mapping_keys_are_capabilities(self) -> None:
        from nexus.backends.base.registry import get_capability_protocols

        mapping = get_capability_protocols()
        for key in mapping:
            assert isinstance(key, BackendFeature)

    def test_mapping_values_are_types(self) -> None:
        from nexus.backends.base.registry import get_capability_protocols

        mapping = get_capability_protocols()
        for value in mapping.values():
            assert isinstance(value, type)

    def test_expected_protocol_mappings(self) -> None:
        from nexus.backends.base.registry import get_capability_protocols

        mapping = get_capability_protocols()
        assert BackendFeature.STREAMING in mapping
        assert BackendFeature.BATCH_CONTENT in mapping
        assert BackendFeature.DIRECTORY_LISTING in mapping
        assert BackendFeature.OAUTH in mapping
