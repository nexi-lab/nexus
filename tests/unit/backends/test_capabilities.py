"""Unit tests for ConnectorCapability system (Issue #2069).

Tests:
1. ConnectorCapability enum completeness
2. Backend.has_capability() contract
3. Backend.capabilities is frozenset (immutable)
4. DelegatingBackend delegates capabilities correctly
5. Convenience frozensets are correct
6. Registry stores capabilities at registration time
7. Registry query methods (get_capabilities, list_by_capability)
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, PropertyMock

import pytest

from nexus.backends.base.backend import Backend
from nexus.backends.base.registry import ConnectorRegistry
from nexus.backends.storage.delegating import DelegatingBackend
from nexus.contracts.capabilities import (
    BLOB_CONNECTOR_CAPABILITIES,
    CORE_CAPABILITIES,
    OAUTH_CONNECTOR_CAPABILITIES,
    ConnectorCapability,
)

# ---------------------------------------------------------------------------
# ConnectorCapability Enum Tests
# ---------------------------------------------------------------------------


class TestConnectorCapabilityEnum:
    """ConnectorCapability enum is well-formed and complete."""

    def test_all_members_are_strings(self) -> None:
        """Every capability value is a non-empty string."""
        for cap in ConnectorCapability:
            assert isinstance(cap.value, str)
            assert len(cap.value) > 0

    def test_values_are_unique(self) -> None:
        """No two capabilities share the same string value."""
        values = [cap.value for cap in ConnectorCapability]
        assert len(values) == len(set(values))

    def test_expected_member_count(self) -> None:
        """We have exactly 18 capabilities defined."""
        assert len(ConnectorCapability) == 18

    def test_str_enum_identity(self) -> None:
        """StrEnum values can be compared with plain strings."""
        assert ConnectorCapability.RENAME == "rename"
        assert ConnectorCapability.SIGNED_URL == "signed_url"

    def test_membership_check_o1(self) -> None:
        """Frozenset membership check works for capabilities."""
        caps = frozenset({ConnectorCapability.RENAME, ConnectorCapability.STREAMING})
        assert ConnectorCapability.RENAME in caps
        assert ConnectorCapability.SIGNED_URL not in caps


# ---------------------------------------------------------------------------
# Convenience Frozensets
# ---------------------------------------------------------------------------


class TestConvenienceFrozensets:
    """CORE_CAPABILITIES, BLOB_CONNECTOR_CAPABILITIES, OAUTH_CONNECTOR_CAPABILITIES."""

    def test_core_is_empty(self) -> None:
        """CORE_CAPABILITIES is empty — backends opt in."""
        assert frozenset() == CORE_CAPABILITIES

    def test_blob_connector_has_expected(self) -> None:
        expected = {
            ConnectorCapability.RENAME,
            ConnectorCapability.DIRECTORY_LISTING,
            ConnectorCapability.PATH_DELETE,
            ConnectorCapability.STREAMING,
            ConnectorCapability.BATCH_CONTENT,
        }
        assert expected == BLOB_CONNECTOR_CAPABILITIES

    def test_oauth_connector_has_expected(self) -> None:
        expected = {
            ConnectorCapability.USER_SCOPED,
            ConnectorCapability.TOKEN_MANAGER,
            ConnectorCapability.OAUTH,
        }
        assert expected == OAUTH_CONNECTOR_CAPABILITIES

    def test_all_frozensets_are_immutable(self) -> None:
        for fs in (CORE_CAPABILITIES, BLOB_CONNECTOR_CAPABILITIES, OAUTH_CONNECTOR_CAPABILITIES):
            assert isinstance(fs, frozenset)


# ---------------------------------------------------------------------------
# Backend ABC Tests
# ---------------------------------------------------------------------------


class TestBackendCapabilities:
    """Backend.capabilities and has_capability() contract."""

    def test_default_capabilities_is_empty_frozenset(self) -> None:
        """Backend ABC default _CAPABILITIES is empty."""
        assert frozenset() == Backend._CAPABILITIES

    def test_capabilities_property_returns_frozenset(self) -> None:
        """capabilities property returns the class _CAPABILITIES."""
        assert isinstance(Backend._CAPABILITIES, frozenset)

    def test_has_capability_true_when_present(self) -> None:
        """has_capability returns True when capability is in _CAPABILITIES."""
        mock = MagicMock(spec=Backend)
        mock.has_capability = lambda cap: cap in frozenset({ConnectorCapability.RENAME})
        assert mock.has_capability(ConnectorCapability.RENAME)

    def test_has_capability_false_when_absent(self) -> None:
        """has_capability returns False when capability is not in _CAPABILITIES."""
        mock = MagicMock(spec=Backend)
        mock.has_capability = lambda cap: cap in frozenset()
        assert not mock.has_capability(ConnectorCapability.RENAME)


# ---------------------------------------------------------------------------
# DelegatingBackend Tests
# ---------------------------------------------------------------------------


class TestDelegatingBackendCapabilities:
    """DelegatingBackend delegates and caches capabilities."""

    @pytest.fixture
    def inner_with_caps(self) -> MagicMock:
        mock = MagicMock(spec=Backend)
        mock.name = "test-inner"
        caps = frozenset({ConnectorCapability.RENAME, ConnectorCapability.STREAMING})
        type(mock).capabilities = PropertyMock(return_value=caps)
        return mock

    def test_capabilities_delegated(self, inner_with_caps: MagicMock) -> None:
        wrapper = DelegatingBackend(inner_with_caps)
        assert wrapper.capabilities == frozenset(
            {ConnectorCapability.RENAME, ConnectorCapability.STREAMING}
        )

    def test_capabilities_cached_in_init(self, inner_with_caps: MagicMock) -> None:
        wrapper = DelegatingBackend(inner_with_caps)
        assert wrapper._cached_capabilities is not None
        assert isinstance(wrapper._cached_capabilities, frozenset)

    def test_has_capability_uses_cached(self, inner_with_caps: MagicMock) -> None:
        wrapper = DelegatingBackend(inner_with_caps)
        assert wrapper.has_capability(ConnectorCapability.RENAME)
        assert not wrapper.has_capability(ConnectorCapability.SIGNED_URL)


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
        caps: frozenset[ConnectorCapability] | None = None,
    ) -> type:
        """Create a minimal fake backend class."""

        class FakeBackend(Backend):
            _CAPABILITIES = caps or frozenset()

            def __init__(self) -> None:
                pass

            @property
            def name(self) -> str:
                return "fake"

            @property
            def user_scoped(self) -> bool:
                return False

            @property
            def is_connected(self) -> bool:
                return False

            @property
            def has_root_path(self) -> bool:
                return False

            @property
            def has_token_manager(self) -> bool:
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

            def get_ref_count(self, *a: object, **kw: object) -> int:
                return 0

            def mkdir(self, *a: object, **kw: object) -> None:
                pass

            def rmdir(self, *a: object, **kw: object) -> None:
                pass

            def is_directory(self, *a: object, **kw: object) -> bool:
                return False

        return FakeBackend

    def test_register_stores_capabilities(self) -> None:
        caps = frozenset({ConnectorCapability.RENAME, ConnectorCapability.STREAMING})
        cls = self._make_fake_backend(caps)
        ConnectorRegistry.register("test_cap_backend", cls)
        info = ConnectorRegistry.get_info("test_cap_backend")
        assert info.capabilities == caps

    def test_register_stores_empty_capabilities(self) -> None:
        cls = self._make_fake_backend(frozenset())
        ConnectorRegistry.register("test_empty_cap", cls)
        info = ConnectorRegistry.get_info("test_empty_cap")
        assert info.capabilities == frozenset()

    def test_get_capabilities(self) -> None:
        caps = frozenset({ConnectorCapability.SIGNED_URL})
        cls = self._make_fake_backend(caps)
        ConnectorRegistry.register("test_get_cap", cls)
        assert ConnectorRegistry.get_capabilities("test_get_cap") == caps

    def test_list_by_capability(self) -> None:
        caps1 = frozenset({ConnectorCapability.RENAME})
        caps2 = frozenset({ConnectorCapability.RENAME, ConnectorCapability.STREAMING})
        cls1 = self._make_fake_backend(caps1)
        cls2 = self._make_fake_backend(caps2)
        ConnectorRegistry.register("test_lbc1", cls1)
        ConnectorRegistry.register("test_lbc2", cls2)
        results = ConnectorRegistry.list_by_capability(ConnectorCapability.RENAME)
        result_names = {r.name for r in results}
        assert "test_lbc1" in result_names
        assert "test_lbc2" in result_names

    def test_list_by_capability_excludes_non_matching(self) -> None:
        caps = frozenset({ConnectorCapability.RENAME})
        cls = self._make_fake_backend(caps)
        ConnectorRegistry.register("test_lbc_excl", cls)
        results = ConnectorRegistry.list_by_capability(ConnectorCapability.SIGNED_URL)
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
            assert isinstance(key, ConnectorCapability)

    def test_mapping_values_are_types(self) -> None:
        from nexus.backends.base.registry import get_capability_protocols

        mapping = get_capability_protocols()
        for value in mapping.values():
            assert isinstance(value, type)

    def test_expected_protocol_mappings(self) -> None:
        from nexus.backends.base.registry import get_capability_protocols

        mapping = get_capability_protocols()
        assert ConnectorCapability.STREAMING in mapping
        assert ConnectorCapability.BATCH_CONTENT in mapping
        assert ConnectorCapability.DIRECTORY_LISTING in mapping
        assert ConnectorCapability.OAUTH in mapping
