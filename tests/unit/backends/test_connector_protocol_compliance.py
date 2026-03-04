"""Protocol compliance tests for LocalBackend against connector sub-protocols.

Verifies that ``LocalBackend`` satisfies all 8 sub-protocols defined in
``nexus.core.protocols.connector`` using the reusable ``assert_protocol_compliance``
helper from ``tests.unit.services.test_protocol_compliance``.

Protocols tested:
    1. ContentStoreProtocol   -- Minimal CAS interface
    2. DirectoryOpsProtocol   -- Directory operations
    3. ConnectorProtocol      -- Full connector interface (CAS + dirs + lifecycle)
    4. PassthroughProtocol    -- Same-box locking / physical paths
    5. OAuthCapableProtocol   -- OAuth token management
    6. StreamingProtocol      -- Streaming reads / writes
    7. BatchContentProtocol   -- Bulk content read
    8. DirectoryListingProtocol -- Directory listing + file metadata

References:
    - Issue #1601: ConnectorProtocol + Storage Brick Extraction
    - Issue #1703: Make backends implement ConnectorProtocol
    - src/nexus/core/protocols/connector.py
"""

import pytest

from nexus.backends.storage.local import LocalBackend
from nexus.core.protocols.connector import (
    BatchContentProtocol,
    ConnectorProtocol,
    ContentStoreProtocol,
    DirectoryListingProtocol,
    DirectoryOpsProtocol,
    OAuthCapableProtocol,
    PassthroughProtocol,
    StreamingProtocol,
)
from tests.unit.services.test_protocol_compliance import assert_protocol_compliance

# ---------------------------------------------------------------------------
# Protocols that LocalBackend is expected to satisfy
# ---------------------------------------------------------------------------

_EXPECTED_PROTOCOLS: list[type] = [
    ContentStoreProtocol,
    DirectoryOpsProtocol,
    ConnectorProtocol,
    StreamingProtocol,
    BatchContentProtocol,
    DirectoryListingProtocol,
]

# Protocols that LocalBackend is NOT expected to satisfy (have callable methods)
_UNSUPPORTED_METHOD_PROTOCOLS: list[type] = [
    PassthroughProtocol,
]

# Protocols that LocalBackend is NOT expected to satisfy (attribute-only)
# OAuthCapableProtocol defines only attributes (token_manager, provider, etc.),
# not callable methods, so assert_protocol_compliance cannot detect non-compliance.
# We use isinstance() checks for these instead.
_UNSUPPORTED_ATTR_PROTOCOLS: list[type] = [
    OAuthCapableProtocol,
]

_ALL_UNSUPPORTED_PROTOCOLS: list[type] = _UNSUPPORTED_METHOD_PROTOCOLS + _UNSUPPORTED_ATTR_PROTOCOLS

# ---------------------------------------------------------------------------
# Parametrized: LocalBackend vs all 8 connector sub-protocols
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "protocol",
    _EXPECTED_PROTOCOLS,
    ids=[p.__name__ for p in _EXPECTED_PROTOCOLS],
)
def test_local_backend_protocol_compliance(protocol: type) -> None:
    """LocalBackend satisfies the expected connector sub-protocols."""
    assert_protocol_compliance(LocalBackend, protocol, strict_params=False)


@pytest.mark.parametrize(
    "protocol",
    _UNSUPPORTED_METHOD_PROTOCOLS,
    ids=[p.__name__ for p in _UNSUPPORTED_METHOD_PROTOCOLS],
)
def test_local_backend_fails_compliance_for_method_protocols(protocol: type) -> None:
    """LocalBackend does NOT satisfy PassthroughProtocol (missing methods)."""
    with pytest.raises(AssertionError):
        assert_protocol_compliance(LocalBackend, protocol, strict_params=False)


def test_local_backend_not_oauth_capable(tmp_path: object) -> None:
    """LocalBackend does NOT satisfy OAuthCapableProtocol (missing attributes).

    OAuthCapableProtocol defines only attributes (token_manager, provider, etc.),
    so we verify via isinstance() on a real instance rather than the method-based
    assert_protocol_compliance helper.
    """
    backend = LocalBackend(root_path=str(tmp_path))
    assert not isinstance(backend, OAuthCapableProtocol)


# ---------------------------------------------------------------------------
# Per-protocol detailed tests
# ---------------------------------------------------------------------------


class TestContentStoreProtocolMethods:
    """Verify LocalBackend has all ContentStoreProtocol methods with correct names."""

    _REQUIRED_METHODS = [
        "write_content",
        "read_content",
        "delete_content",
        "content_exists",
        "get_content_size",
        "get_ref_count",
    ]

    @pytest.mark.parametrize("method_name", _REQUIRED_METHODS)
    def test_method_exists(self, method_name: str) -> None:
        attr = getattr(LocalBackend, method_name, None)
        assert attr is not None, f"LocalBackend missing ContentStoreProtocol method: {method_name}"
        assert callable(attr), f"LocalBackend.{method_name} is not callable"

    def test_name_property_exists(self) -> None:
        """ContentStoreProtocol requires a 'name' property."""
        assert hasattr(LocalBackend, "name"), "LocalBackend missing 'name' property"


class TestDirectoryOpsProtocolMethods:
    """Verify LocalBackend has all DirectoryOpsProtocol methods."""

    _REQUIRED_METHODS = ["mkdir", "rmdir", "is_directory"]

    @pytest.mark.parametrize("method_name", _REQUIRED_METHODS)
    def test_method_exists(self, method_name: str) -> None:
        attr = getattr(LocalBackend, method_name, None)
        assert attr is not None, f"LocalBackend missing DirectoryOpsProtocol method: {method_name}"
        assert callable(attr), f"LocalBackend.{method_name} is not callable"


class TestConnectorProtocolMethods:
    """Verify LocalBackend has ConnectorProtocol lifecycle and capability methods."""

    _LIFECYCLE_METHODS = ["connect", "disconnect", "check_connection"]
    _CAPABILITY_PROPERTIES = [
        "user_scoped",
        "is_connected",
        "is_passthrough",
        "has_root_path",
        "has_token_manager",
    ]

    @pytest.mark.parametrize("method_name", _LIFECYCLE_METHODS)
    def test_lifecycle_method_exists(self, method_name: str) -> None:
        attr = getattr(LocalBackend, method_name, None)
        assert attr is not None, (
            f"LocalBackend missing ConnectorProtocol lifecycle method: {method_name}"
        )

    @pytest.mark.parametrize("prop_name", _CAPABILITY_PROPERTIES)
    def test_capability_property_exists(self, prop_name: str) -> None:
        assert hasattr(LocalBackend, prop_name), (
            f"LocalBackend missing ConnectorProtocol capability: {prop_name}"
        )


class TestStreamingProtocolMethods:
    """Verify LocalBackend has all StreamingProtocol methods."""

    _REQUIRED_METHODS = ["stream_content", "stream_range", "write_stream"]

    @pytest.mark.parametrize("method_name", _REQUIRED_METHODS)
    def test_method_exists(self, method_name: str) -> None:
        attr = getattr(LocalBackend, method_name, None)
        assert attr is not None, f"LocalBackend missing StreamingProtocol method: {method_name}"
        assert callable(attr), f"LocalBackend.{method_name} is not callable"


class TestBatchContentProtocolMethods:
    """Verify LocalBackend has batch_read_content."""

    def test_batch_read_content_exists(self) -> None:
        attr = getattr(LocalBackend, "batch_read_content", None)
        assert attr is not None, "LocalBackend missing batch_read_content"
        assert callable(attr), "LocalBackend.batch_read_content is not callable"


class TestDirectoryListingProtocolMethods:
    """Verify LocalBackend has list_dir and get_file_info."""

    _REQUIRED_METHODS = ["list_dir", "get_file_info"]

    @pytest.mark.parametrize("method_name", _REQUIRED_METHODS)
    def test_method_exists(self, method_name: str) -> None:
        attr = getattr(LocalBackend, method_name, None)
        assert attr is not None, (
            f"LocalBackend missing DirectoryListingProtocol method: {method_name}"
        )
        assert callable(attr), f"LocalBackend.{method_name} is not callable"


# ---------------------------------------------------------------------------
# isinstance runtime check with an actual LocalBackend instance
# ---------------------------------------------------------------------------


class TestRuntimeCheckable:
    """Verify runtime isinstance() checks against a real LocalBackend instance."""

    @pytest.fixture()
    def backend(self, tmp_path: object) -> LocalBackend:
        return LocalBackend(root_path=str(tmp_path))

    @pytest.mark.parametrize(
        "protocol",
        _EXPECTED_PROTOCOLS,
        ids=[p.__name__ for p in _EXPECTED_PROTOCOLS],
    )
    def test_isinstance_passes(self, backend: LocalBackend, protocol: type) -> None:
        """LocalBackend instances pass isinstance() for expected protocols."""
        assert isinstance(backend, protocol)

    @pytest.mark.parametrize(
        "protocol",
        _ALL_UNSUPPORTED_PROTOCOLS,
        ids=[p.__name__ for p in _ALL_UNSUPPORTED_PROTOCOLS],
    )
    def test_isinstance_fails_for_unsupported(self, backend: LocalBackend, protocol: type) -> None:
        """LocalBackend instances do NOT pass isinstance() for unsupported protocols."""
        assert not isinstance(backend, protocol)
