"""Protocol conformance tests for storage backends (Issue #1703, #2362, #2367).

Verifies that:
1. Backend ABC structurally satisfies all connector protocols
2. Concrete backends satisfy ConnectorProtocol via isinstance()
3. New layered protocols (Streaming, Batch, DirListing) are satisfied
4. OAuthCapableProtocol is satisfied by OAuth connectors
5. Non-compliant classes are correctly rejected
6. DelegatingBackend satisfies ConnectorProtocol (Issue #2362)
7. SearchableConnector conformance (Issue #2367)
8. CachingConnectorContract and CacheConfigContract conformance (Issue #2362)
"""

import hashlib
from typing import Any

import pytest

from nexus.backends.base.backend import Backend
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.core.object_store import WriteResult
from nexus.core.protocols.connector import (
    BatchContentProtocol,
    ConnectorProtocol,
    ContentStoreProtocol,
    DirectoryListingProtocol,
    DirectoryOpsProtocol,
    OAuthCapableProtocol,
    StreamingProtocol,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockBackend(Backend):
    """In-memory Backend for conformance testing."""

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}
        self._ref_counts: dict[str, int] = {}
        self._dirs: set[str] = set()

    @property
    def name(self) -> str:
        return "mock"

    def _hash(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def write_content(
        self, content: bytes, content_id: str = "", *, offset: int = 0, context: Any = None
    ) -> WriteResult:
        h = self._hash(content)
        if h in self._content:
            self._ref_counts[h] += 1
        else:
            self._content[h] = content
            self._ref_counts[h] = 1
        return WriteResult(content_id=h, size=len(content))

    def read_content(self, content_hash: str, context: Any = None) -> bytes:
        if content_hash not in self._content:
            raise NexusFileNotFoundError(content_hash)
        return self._content[content_hash]

    def delete_content(self, content_hash: str, context: Any = None) -> None:
        if content_hash not in self._content:
            raise NexusFileNotFoundError(content_hash)
        self._ref_counts[content_hash] -= 1
        if self._ref_counts[content_hash] <= 0:
            del self._content[content_hash]
            del self._ref_counts[content_hash]

    def content_exists(self, content_hash: str, context: Any = None) -> bool:
        return content_hash in self._content

    def get_content_size(self, content_hash: str, context: Any = None) -> int:
        if content_hash not in self._content:
            raise NexusFileNotFoundError(content_hash)
        return len(self._content[content_hash])

    def mkdir(
        self, path: str, parents: bool = False, exist_ok: bool = False, context: Any = None
    ) -> None:
        self._dirs.add(path)

    def rmdir(self, path: str, recursive: bool = False, context: Any = None) -> None:
        self._dirs.discard(path)

    def is_directory(self, path: str, context: Any = None) -> bool:
        return path in self._dirs


class _IncompleteClass:
    """Missing most protocol methods — should fail all checks."""

    @property
    def name(self) -> str:
        return "incomplete"


class _PartialClass:
    """Has content methods but missing directory ops — should fail ConnectorProtocol."""

    @property
    def name(self) -> str:
        return "partial"

    def write_content(
        self, content: bytes, content_id: str = "", *, offset: int = 0, context: Any = None
    ) -> Any:
        return None

    def read_content(self, content_hash: str, context: Any = None) -> Any:
        return None

    def delete_content(self, content_hash: str, context: Any = None) -> Any:
        return None

    def content_exists(self, content_hash: str, context: Any = None) -> Any:
        return None

    def get_content_size(self, content_hash: str, context: Any = None) -> Any:
        return None


# ---------------------------------------------------------------------------
# Test: Backend ABC satisfies all connector protocols
# ---------------------------------------------------------------------------


class TestBackendProtocolConformance:
    """Verify Backend ABC satisfies all connector protocols structurally."""

    def test_backend_satisfies_connector_protocol(self) -> None:
        """Backend instances satisfy the full ConnectorProtocol."""
        backend = _MockBackend()
        assert isinstance(backend, ConnectorProtocol)

    def test_backend_satisfies_content_store_protocol(self) -> None:
        backend = _MockBackend()
        assert isinstance(backend, ContentStoreProtocol)

    def test_backend_satisfies_directory_ops_protocol(self) -> None:
        backend = _MockBackend()
        assert isinstance(backend, DirectoryOpsProtocol)

    def test_backend_satisfies_streaming_protocol(self) -> None:
        """Backend provides stream_content, stream_range, write_stream."""
        backend = _MockBackend()
        assert isinstance(backend, StreamingProtocol)

    def test_backend_satisfies_batch_content_protocol(self) -> None:
        """Backend provides batch_read_content."""
        backend = _MockBackend()
        assert isinstance(backend, BatchContentProtocol)

    def test_backend_satisfies_directory_listing_protocol(self) -> None:
        """Backend provides list_dir and get_file_info."""
        backend = _MockBackend()
        assert isinstance(backend, DirectoryListingProtocol)


class TestBackendProtocolConformanceViaHelper:
    """Use assert_protocol_conformance for deeper method+param checking."""

    def test_connector_protocol_conformance(self) -> None:
        from tests.unit.core.protocols.test_conformance import assert_protocol_conformance

        assert_protocol_conformance(Backend, ConnectorProtocol)

    def test_streaming_protocol_conformance(self) -> None:
        from tests.unit.core.protocols.test_conformance import assert_protocol_conformance

        assert_protocol_conformance(Backend, StreamingProtocol)

    def test_batch_content_protocol_conformance(self) -> None:
        from tests.unit.core.protocols.test_conformance import assert_protocol_conformance

        assert_protocol_conformance(Backend, BatchContentProtocol)

    def test_directory_listing_protocol_conformance(self) -> None:
        from tests.unit.core.protocols.test_conformance import assert_protocol_conformance

        assert_protocol_conformance(Backend, DirectoryListingProtocol)


# ---------------------------------------------------------------------------
# Test: Concrete backends satisfy ConnectorProtocol
# ---------------------------------------------------------------------------


class TestConcreteBackendConformance:
    """Verify concrete backends satisfy ConnectorProtocol via isinstance."""

    def test_local_backend(self, tmp_path: Any) -> None:
        from nexus.backends.storage.cas_local import CASLocalBackend

        backend = CASLocalBackend(root_path=str(tmp_path / "data"))
        assert isinstance(backend, ConnectorProtocol)
        assert isinstance(backend, StreamingProtocol)
        assert isinstance(backend, BatchContentProtocol)


# ---------------------------------------------------------------------------
# Test: Negative conformance (non-compliant classes)
# ---------------------------------------------------------------------------


class TestNegativeConformance:
    """Incomplete objects must NOT satisfy protocols."""

    def test_incomplete_not_content_store(self) -> None:
        obj = _IncompleteClass()
        assert not isinstance(obj, ContentStoreProtocol)

    def test_incomplete_not_connector(self) -> None:
        obj = _IncompleteClass()
        assert not isinstance(obj, ConnectorProtocol)

    def test_incomplete_not_streaming(self) -> None:
        obj = _IncompleteClass()
        assert not isinstance(obj, StreamingProtocol)

    def test_partial_not_connector(self) -> None:
        """Has content methods but missing directory ops."""
        obj = _PartialClass()
        assert isinstance(obj, ContentStoreProtocol)  # Has all CAS methods
        assert not isinstance(obj, ConnectorProtocol)  # Missing dir ops + lifecycle


# ---------------------------------------------------------------------------
# Test: OAuthCapableProtocol
# ---------------------------------------------------------------------------


class TestOAuthCapableProtocol:
    """Verify OAuthCapableProtocol detects OAuth connectors."""

    def test_non_oauth_backend_not_capable(self) -> None:
        """Non-OAuth backends should NOT satisfy OAuthCapableProtocol."""
        backend = _MockBackend()
        assert not isinstance(backend, OAuthCapableProtocol)

    def test_oauth_attributes_satisfy_protocol(self) -> None:
        """Object with required OAuth attributes satisfies the protocol."""

        class FakeOAuthBackend(_MockBackend):
            def __init__(self) -> None:
                super().__init__()
                self.token_manager = object()
                self.token_manager_db = "sqlite:///test.db"
                self.user_email = "test@example.com"
                self.provider = "google"

            @property
            def user_scoped(self) -> bool:
                return True

            @property
            def has_token_manager(self) -> bool:
                return True

        backend = FakeOAuthBackend()
        assert isinstance(backend, OAuthCapableProtocol)

    def test_partial_oauth_not_capable(self) -> None:
        """Missing some OAuth attributes should NOT satisfy the protocol."""

        class PartialOAuth(_MockBackend):
            def __init__(self) -> None:
                super().__init__()
                self.token_manager = object()
                # Missing: token_manager_db, user_email, provider

        backend = PartialOAuth()
        assert not isinstance(backend, OAuthCapableProtocol)


# ---------------------------------------------------------------------------
# Test: Registry validation (Issue #1703)
# ---------------------------------------------------------------------------


class TestRegistryProtocolValidation:
    """Verify ConnectorRegistry rejects non-compliant backends."""

    @pytest.fixture(autouse=True)
    def _clear_registry(self) -> Any:
        """Clear registry before and after each test."""
        from nexus.backends.base.registry import ConnectorRegistry

        saved = dict(ConnectorRegistry._base._items)
        ConnectorRegistry.clear()
        yield
        ConnectorRegistry._base._items = saved

    def test_compliant_backend_registers(self) -> None:
        """A compliant Backend subclass registers successfully."""
        from nexus.backends.base.registry import ConnectorRegistry

        ConnectorRegistry.register("test_ok", _MockBackend)
        assert ConnectorRegistry.is_registered("test_ok")

    def test_non_compliant_class_rejected(self) -> None:
        """A class missing ConnectorProtocol methods is rejected."""
        from nexus.backends.base.registry import ConnectorRegistry

        with pytest.raises(ValueError, match="does not satisfy ConnectorProtocol"):
            ConnectorRegistry.register("test_bad", _IncompleteClass)  # type: ignore[arg-type]

    def test_rejection_lists_missing_members(self) -> None:
        """Error message lists the specific missing members."""
        from nexus.backends.base.registry import ConnectorRegistry

        with pytest.raises(ValueError, match="write_content") as exc_info:
            ConnectorRegistry.register("test_bad2", _IncompleteClass)  # type: ignore[arg-type]

        # Should mention several missing members
        msg = str(exc_info.value)
        assert "Missing members:" in msg
        assert "read_content" in msg
        assert "mkdir" in msg


# ---------------------------------------------------------------------------
# Test: _CONNECTOR_PROTOCOL_MEMBERS stays in sync with ConnectorProtocol
# ---------------------------------------------------------------------------


class TestProtocolMembersSync:
    """Guard against drift between registry frozenset and actual protocol."""

    def test_connector_protocol_members_in_sync(self) -> None:
        """_CONNECTOR_PROTOCOL_MEMBERS matches ConnectorProtocol's actual members."""
        import inspect

        from nexus.backends.base.registry import _CONNECTOR_PROTOCOL_MEMBERS

        # Collect all public members defined in the protocol hierarchy
        protocol_members: set[str] = set()
        for name, _ in inspect.getmembers(ConnectorProtocol):
            if name.startswith("_"):
                continue
            # Exclude inherited object methods (e.g., __class__)
            if hasattr(object, name):
                continue
            protocol_members.add(name)

        assert protocol_members == _CONNECTOR_PROTOCOL_MEMBERS, (
            f"Drift detected!\n"
            f"  In protocol but not in frozenset: {protocol_members - _CONNECTOR_PROTOCOL_MEMBERS}\n"
            f"  In frozenset but not in protocol: {_CONNECTOR_PROTOCOL_MEMBERS - protocol_members}"
        )


# ---------------------------------------------------------------------------
# Test: DelegatingBackend satisfies ConnectorProtocol (Issue #2362, Decision 4B)
# ---------------------------------------------------------------------------


class TestDelegatingBackendConformance:
    """DelegatingBackend wrapping a mock inner satisfies ConnectorProtocol.

    This test documents the migration intent: DelegatingBackend is the
    base for all recursive wrappers and must satisfy the same protocol
    as the inner backend it wraps.
    """

    def test_delegating_backend_satisfies_connector_protocol(self) -> None:
        from nexus.backends.storage.delegating import DelegatingBackend

        inner = _MockBackend()
        wrapper = DelegatingBackend(inner)
        assert isinstance(wrapper, ConnectorProtocol)

    def test_delegating_backend_satisfies_content_store_protocol(self) -> None:
        from nexus.backends.storage.delegating import DelegatingBackend

        inner = _MockBackend()
        wrapper = DelegatingBackend(inner)
        assert isinstance(wrapper, ContentStoreProtocol)

    def test_delegating_backend_satisfies_directory_ops_protocol(self) -> None:
        from nexus.backends.storage.delegating import DelegatingBackend

        inner = _MockBackend()
        wrapper = DelegatingBackend(inner)
        assert isinstance(wrapper, DirectoryOpsProtocol)


# ---------------------------------------------------------------------------
# Test: SearchableConnector conformance (Issue #2367, Decision 9A)
# ---------------------------------------------------------------------------


class TestSearchableConnectorConformance:
    """SearchableConnector protocol conformance tests."""

    def test_searchable_class_satisfies_protocol(self) -> None:
        """A class with search/index/remove_from_index satisfies SearchableConnector."""
        from nexus.core.protocols.connector import SearchableConnector

        class _MockSearchable:
            def search(
                self, query: str, *, filters: Any = None, limit: int = 10, context: Any = None
            ) -> list[dict[str, Any]]:
                return []

            def index(
                self, key: str, content: str, metadata: Any = None, context: Any = None
            ) -> None:
                pass

            def remove_from_index(self, key: str, context: Any = None) -> None:
                pass

        obj = _MockSearchable()
        assert isinstance(obj, SearchableConnector)

    def test_non_searchable_class_rejected(self) -> None:
        """A plain backend without search methods fails SearchableConnector check."""
        from nexus.core.protocols.connector import SearchableConnector

        backend = _MockBackend()
        assert not isinstance(backend, SearchableConnector)

    def test_partial_searchable_rejected(self) -> None:
        """A class with only search() but missing index() fails the check."""
        from nexus.core.protocols.connector import SearchableConnector

        class _PartialSearchable:
            def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
                return []

        obj = _PartialSearchable()
        assert not isinstance(obj, SearchableConnector)


# ---------------------------------------------------------------------------
# Test: CachingConnectorContract and CacheConfigContract (Issue #2362, Decision 12A)
# ---------------------------------------------------------------------------


class TestCachingContractConformance:
    """Verify CachingConnectorContract and CacheConfigContract protocols."""

    def test_caching_backend_wrapper_satisfies_caching_connector_contract(self) -> None:
        """CachingBackendWrapper satisfies the new CachingConnectorContract."""
        from nexus.backends.wrappers.caching import CachingBackendWrapper
        from nexus.core.protocols.caching import CachingConnectorContract

        inner = _MockBackend()
        wrapper = CachingBackendWrapper(inner=inner)
        assert isinstance(wrapper, CachingConnectorContract)

    def test_plain_backend_not_caching_connector(self) -> None:
        """Plain Backend without get_cache_stats/clear_cache is not CachingConnectorContract."""
        from nexus.core.protocols.caching import CachingConnectorContract

        backend = _MockBackend()
        assert not isinstance(backend, CachingConnectorContract)

    def test_cache_config_contract_satisfied(self) -> None:
        """Object with session_factory/zone_id/l1_only satisfies CacheConfigContract."""
        from nexus.core.protocols.caching import CacheConfigContract

        class _MockCacheConfig:
            session_factory = None
            zone_id = ROOT_ZONE_ID
            l1_only = False

        obj = _MockCacheConfig()
        assert isinstance(obj, CacheConfigContract)

    def test_cache_config_contract_rejected(self) -> None:
        """Object missing cache config attributes fails CacheConfigContract."""
        from nexus.core.protocols.caching import CacheConfigContract

        backend = _MockBackend()
        assert not isinstance(backend, CacheConfigContract)
