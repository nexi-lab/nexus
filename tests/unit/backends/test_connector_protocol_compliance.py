"""Protocol compliance tests for CASLocalBackend against connector sub-protocols.

Verifies that ``CASLocalBackend`` satisfies all 7 sub-protocols defined in
``nexus.core.protocols.connector`` using the reusable ``assert_protocol_compliance``
helper from ``tests.unit.services.test_protocol_compliance``.

Protocols tested:
    1. ContentStoreProtocol   -- Minimal CAS interface
    2. DirectoryOpsProtocol   -- Directory operations
    3. ConnectorProtocol      -- Full connector interface (CAS + dirs + lifecycle)
    4. OAuthCapableProtocol   -- OAuth token management
    5. StreamingProtocol      -- Streaming reads / writes
    6. BatchContentProtocol   -- Bulk content read
    7. DirectoryListingProtocol -- Directory listing + file metadata

References:
    - Issue #1601: ConnectorProtocol + Storage Brick Extraction
    - Issue #1703: Make backends implement ConnectorProtocol
    - src/nexus/core/protocols/connector.py
"""

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.core.protocols.connector import (
    BatchContentProtocol,
    ConnectorProtocol,
    ContentStoreProtocol,
    DirectoryListingProtocol,
    DirectoryOpsProtocol,
    OAuthCapableProtocol,
    StreamingProtocol,
)
from tests.unit.services.test_protocol_compliance import assert_protocol_compliance

# ---------------------------------------------------------------------------
# Protocols that CASLocalBackend is expected to satisfy
# ---------------------------------------------------------------------------

_EXPECTED_PROTOCOLS: list[type] = [
    ContentStoreProtocol,
    DirectoryOpsProtocol,
    ConnectorProtocol,
    StreamingProtocol,
    BatchContentProtocol,
    DirectoryListingProtocol,
]

# Protocols that CASLocalBackend is NOT expected to satisfy (attribute-only)
# OAuthCapableProtocol defines only attributes (token_manager, provider, etc.),
# not callable methods, so assert_protocol_compliance cannot detect non-compliance.
# We use isinstance() checks for these instead.
_UNSUPPORTED_ATTR_PROTOCOLS: list[type] = [
    OAuthCapableProtocol,
]

_ALL_UNSUPPORTED_PROTOCOLS: list[type] = _UNSUPPORTED_ATTR_PROTOCOLS

# ---------------------------------------------------------------------------
# Parametrized: CASLocalBackend vs all 7 connector sub-protocols
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "protocol",
    _EXPECTED_PROTOCOLS,
    ids=[p.__name__ for p in _EXPECTED_PROTOCOLS],
)
def test_local_backend_protocol_compliance(protocol: type) -> None:
    """CASLocalBackend satisfies the expected connector sub-protocols."""
    assert_protocol_compliance(CASLocalBackend, protocol, strict_params=False)


def test_local_backend_not_oauth_capable(tmp_path: object) -> None:
    """CASLocalBackend does NOT satisfy OAuthCapableProtocol (missing attributes).

    OAuthCapableProtocol defines only attributes (token_manager, provider, etc.),
    so we verify via isinstance() on a real instance rather than the method-based
    assert_protocol_compliance helper.
    """
    backend = CASLocalBackend(root_path=str(tmp_path))
    assert not isinstance(backend, OAuthCapableProtocol)


# ---------------------------------------------------------------------------
# Per-protocol detailed tests
# ---------------------------------------------------------------------------


class TestContentStoreProtocolMethods:
    """Verify CASLocalBackend has all ContentStoreProtocol methods with correct names."""

    _REQUIRED_METHODS = [
        "write_content",
        "read_content",
        "delete_content",
        "content_exists",
        "get_content_size",
    ]

    @pytest.mark.parametrize("method_name", _REQUIRED_METHODS)
    def test_method_exists(self, method_name: str) -> None:
        attr = getattr(CASLocalBackend, method_name, None)
        assert attr is not None, (
            f"CASLocalBackend missing ContentStoreProtocol method: {method_name}"
        )
        assert callable(attr), f"CASLocalBackend.{method_name} is not callable"

    def test_name_property_exists(self) -> None:
        """ContentStoreProtocol requires a 'name' property."""
        assert hasattr(CASLocalBackend, "name"), "CASLocalBackend missing 'name' property"


class TestDirectoryOpsProtocolMethods:
    """Verify CASLocalBackend has all DirectoryOpsProtocol methods."""

    _REQUIRED_METHODS = ["mkdir", "rmdir", "is_directory"]

    @pytest.mark.parametrize("method_name", _REQUIRED_METHODS)
    def test_method_exists(self, method_name: str) -> None:
        attr = getattr(CASLocalBackend, method_name, None)
        assert attr is not None, (
            f"CASLocalBackend missing DirectoryOpsProtocol method: {method_name}"
        )
        assert callable(attr), f"CASLocalBackend.{method_name} is not callable"


class TestConnectorProtocolMethods:
    """Verify CASLocalBackend has ConnectorProtocol lifecycle and capability methods."""

    _LIFECYCLE_METHODS = ["check_connection"]
    _CAPABILITY_PROPERTIES = [
        "is_connected",
        "has_root_path",
    ]

    @pytest.mark.parametrize("method_name", _LIFECYCLE_METHODS)
    def test_lifecycle_method_exists(self, method_name: str) -> None:
        attr = getattr(CASLocalBackend, method_name, None)
        assert attr is not None, (
            f"CASLocalBackend missing ConnectorProtocol lifecycle method: {method_name}"
        )

    @pytest.mark.parametrize("prop_name", _CAPABILITY_PROPERTIES)
    def test_capability_property_exists(self, prop_name: str) -> None:
        assert hasattr(CASLocalBackend, prop_name), (
            f"CASLocalBackend missing ConnectorProtocol capability: {prop_name}"
        )


class TestStreamingProtocolMethods:
    """Verify CASLocalBackend has all StreamingProtocol methods."""

    _REQUIRED_METHODS = ["stream_content", "stream_range", "write_stream"]

    @pytest.mark.parametrize("method_name", _REQUIRED_METHODS)
    def test_method_exists(self, method_name: str) -> None:
        attr = getattr(CASLocalBackend, method_name, None)
        assert attr is not None, f"CASLocalBackend missing StreamingProtocol method: {method_name}"
        assert callable(attr), f"CASLocalBackend.{method_name} is not callable"


class TestBatchContentProtocolMethods:
    """Verify CASLocalBackend has batch_read_content."""

    def test_batch_read_content_exists(self) -> None:
        attr = getattr(CASLocalBackend, "batch_read_content", None)
        assert attr is not None, "CASLocalBackend missing batch_read_content"
        assert callable(attr), "CASLocalBackend.batch_read_content is not callable"


class TestDirectoryListingProtocolMethods:
    """Verify CASLocalBackend has list_dir and get_file_info."""

    _REQUIRED_METHODS = ["list_dir", "get_file_info"]

    @pytest.mark.parametrize("method_name", _REQUIRED_METHODS)
    def test_method_exists(self, method_name: str) -> None:
        attr = getattr(CASLocalBackend, method_name, None)
        assert attr is not None, (
            f"CASLocalBackend missing DirectoryListingProtocol method: {method_name}"
        )
        assert callable(attr), f"CASLocalBackend.{method_name} is not callable"


# ---------------------------------------------------------------------------
# isinstance runtime check with an actual CASLocalBackend instance
# ---------------------------------------------------------------------------


class TestRuntimeCheckable:
    """Verify runtime isinstance() checks against a real CASLocalBackend instance."""

    @pytest.fixture()
    def backend(self, tmp_path: object) -> CASLocalBackend:
        return CASLocalBackend(root_path=str(tmp_path))

    @pytest.mark.parametrize(
        "protocol",
        _EXPECTED_PROTOCOLS,
        ids=[p.__name__ for p in _EXPECTED_PROTOCOLS],
    )
    def test_isinstance_passes(self, backend: CASLocalBackend, protocol: type) -> None:
        """CASLocalBackend instances pass isinstance() for expected protocols."""
        assert isinstance(backend, protocol)

    @pytest.mark.parametrize(
        "protocol",
        _ALL_UNSUPPORTED_PROTOCOLS,
        ids=[p.__name__ for p in _ALL_UNSUPPORTED_PROTOCOLS],
    )
    def test_isinstance_fails_for_unsupported(
        self, backend: CASLocalBackend, protocol: type
    ) -> None:
        """CASLocalBackend instances do NOT pass isinstance() for unsupported protocols."""
        assert not isinstance(backend, protocol)
