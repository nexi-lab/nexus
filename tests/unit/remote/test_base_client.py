"""Unit tests for BaseRemoteNexusFS shared logic.

Tests cover:
- Negative cache (Bloom filter): init, check, add, invalidate, bulk invalidate
- Zone/agent identity properties
- RPC error handling: error code to exception mapping
- Response parsing: standard bytes, legacy format, raw bytes, delta_read
- _decode_bytes_field: bytes dict, base64 string, raw bytes
- _parse_auth_info: authenticated vs. unauthenticated, user vs. agent
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from nexus.core.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.remote.base_client import BaseRemoteNexusFS
from nexus.server.protocol import RPCErrorCode

# ---------------------------------------------------------------------------
# Concrete subclass for testing (BaseRemoteNexusFS is abstract-like)
# ---------------------------------------------------------------------------


class ConcreteRemoteFS(BaseRemoteNexusFS):
    """Minimal concrete subclass to test BaseRemoteNexusFS helpers."""

    def __init__(
        self,
        zone_id: str | None = None,
        agent_id: str | None = None,
        negative_cache_capacity: int = 1000,
        negative_cache_fp_rate: float = 0.01,
    ):
        self._zone_id = zone_id
        self._agent_id = agent_id
        self._negative_cache_capacity = negative_cache_capacity
        self._negative_cache_fp_rate = negative_cache_fp_rate
        self._negative_bloom = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> ConcreteRemoteFS:
    """Create a test client instance."""
    return ConcreteRemoteFS(zone_id="test-zone", agent_id=None)


@pytest.fixture
def client_with_bloom() -> ConcreteRemoteFS:
    """Create a client and attempt to init bloom filter (may be None if nexus_fast unavailable)."""
    c = ConcreteRemoteFS(zone_id="test-zone")
    c._init_negative_cache()
    return c


# ===========================================================================
# Negative Cache
# ===========================================================================


class TestNegativeCacheWithoutBloom:
    """Tests for negative cache when Bloom filter is unavailable (None)."""

    def test_check_returns_false_when_bloom_is_none(self, client):
        """Without bloom filter, check should always return False (allow RPC)."""
        assert client._negative_bloom is None
        assert client._negative_cache_check("/test.txt") is False

    def test_add_does_not_raise_when_bloom_is_none(self, client):
        """Adding to negative cache should be a no-op when bloom is None."""
        client._negative_cache_add("/test.txt")  # Should not raise

    def test_invalidate_does_not_raise_when_bloom_is_none(self, client):
        """Invalidate should be a no-op when bloom is None."""
        client._negative_cache_invalidate("/test.txt")  # Should not raise

    def test_bulk_invalidate_does_not_raise_when_bloom_is_none(self, client):
        """Bulk invalidate should be a no-op when bloom is None."""
        client._negative_cache_invalidate_bulk(["/a.txt", "/b.txt"])

    def test_bulk_invalidate_empty_list(self, client):
        """Empty paths list should be a no-op even with bloom present."""
        client._negative_bloom = MagicMock()
        client._negative_cache_invalidate_bulk([])
        client._negative_bloom.clear.assert_not_called()


class TestNegativeCacheWithMockBloom:
    """Tests for negative cache with a mocked Bloom filter."""

    def _client_with_mock_bloom(self) -> ConcreteRemoteFS:
        c = ConcreteRemoteFS(zone_id="z1")
        bloom = MagicMock()
        bloom.might_exist.return_value = False
        c._negative_bloom = bloom
        return c

    def test_cache_key_includes_zone(self):
        c = ConcreteRemoteFS(zone_id="zone-42")
        assert c._negative_cache_key("/file.txt") == "zone-42:/file.txt"

    def test_cache_key_default_zone(self):
        c = ConcreteRemoteFS(zone_id=None)
        assert c._negative_cache_key("/file.txt") == "default:/file.txt"

    def test_check_calls_might_exist(self):
        c = self._client_with_mock_bloom()
        c._negative_bloom.might_exist.return_value = True
        assert c._negative_cache_check("/gone.txt") is True
        c._negative_bloom.might_exist.assert_called_once_with("z1:/gone.txt")

    def test_add_calls_bloom_add(self):
        c = self._client_with_mock_bloom()
        c._negative_cache_add("/missing.txt")
        c._negative_bloom.add.assert_called_once_with("z1:/missing.txt")

    def test_invalidate_clears_bloom(self):
        c = self._client_with_mock_bloom()
        c._negative_cache_invalidate("/updated.txt")
        c._negative_bloom.clear.assert_called_once()

    def test_bulk_invalidate_clears_bloom(self):
        c = self._client_with_mock_bloom()
        c._negative_cache_invalidate_bulk(["/a.txt", "/b.txt"])
        c._negative_bloom.clear.assert_called_once()


class TestNegativeCacheInit:
    """Tests for _init_negative_cache."""

    def test_init_with_import_error(self):
        """When nexus_fast is unavailable, bloom should be None."""
        c = ConcreteRemoteFS()
        with patch.dict("sys.modules", {"nexus_fast": None}), patch(
            "builtins.__import__", side_effect=ImportError("no nexus_fast")
        ):
            c._init_negative_cache()
        assert c._negative_bloom is None

    def test_init_with_generic_exception(self):
        """Generic exception during init should set bloom to None."""
        c = ConcreteRemoteFS()
        mock_module = MagicMock()
        mock_module.BloomFilter.side_effect = RuntimeError("init failed")
        with patch.dict("sys.modules", {"nexus_fast": mock_module}):
            c._init_negative_cache()
        assert c._negative_bloom is None


# ===========================================================================
# Zone / Agent Properties
# ===========================================================================


class TestIdentityProperties:
    """Tests for zone_id and agent_id properties."""

    def test_zone_id_getter(self, client):
        assert client.zone_id == "test-zone"

    def test_zone_id_setter(self, client):
        client.zone_id = "new-zone"
        assert client.zone_id == "new-zone"

    def test_agent_id_getter(self, client):
        assert client.agent_id is None

    def test_agent_id_setter(self, client):
        client.agent_id = "agent-007"
        assert client.agent_id == "agent-007"


# ===========================================================================
# RPC Error Handling
# ===========================================================================


class TestHandleRpcError:
    """Tests for _handle_rpc_error exception mapping."""

    def test_file_not_found(self, client):
        error = {
            "code": RPCErrorCode.FILE_NOT_FOUND.value,
            "message": "Not found",
            "data": {"path": "/missing.txt"},
        }
        with pytest.raises(NexusFileNotFoundError):
            client._handle_rpc_error(error)

    def test_file_not_found_without_path_data(self, client):
        error = {
            "code": RPCErrorCode.FILE_NOT_FOUND.value,
            "message": "Not found",
            "data": None,
        }
        with pytest.raises(NexusFileNotFoundError, match="Not found"):
            client._handle_rpc_error(error)

    def test_file_exists(self, client):
        error = {
            "code": RPCErrorCode.FILE_EXISTS.value,
            "message": "Already exists",
        }
        with pytest.raises(FileExistsError, match="Already exists"):
            client._handle_rpc_error(error)

    def test_invalid_path(self, client):
        error = {
            "code": RPCErrorCode.INVALID_PATH.value,
            "message": "Bad path",
        }
        with pytest.raises(InvalidPathError):
            client._handle_rpc_error(error)

    def test_access_denied(self, client):
        error = {
            "code": RPCErrorCode.ACCESS_DENIED.value,
            "message": "Denied",
        }
        with pytest.raises(NexusPermissionError):
            client._handle_rpc_error(error)

    def test_permission_error(self, client):
        error = {
            "code": RPCErrorCode.PERMISSION_ERROR.value,
            "message": "Forbidden",
        }
        with pytest.raises(NexusPermissionError):
            client._handle_rpc_error(error)

    def test_validation_error(self, client):
        error = {
            "code": RPCErrorCode.VALIDATION_ERROR.value,
            "message": "Invalid input",
        }
        with pytest.raises(ValidationError):
            client._handle_rpc_error(error)

    def test_conflict_error(self, client):
        error = {
            "code": RPCErrorCode.CONFLICT.value,
            "message": "Conflict",
            "data": {
                "path": "/file.txt",
                "expected_etag": "aaa",
                "current_etag": "bbb",
            },
        }
        with pytest.raises(ConflictError) as exc_info:
            client._handle_rpc_error(error)
        assert exc_info.value.expected_etag == "aaa"
        assert exc_info.value.current_etag == "bbb"

    def test_conflict_error_without_data(self, client):
        error = {
            "code": RPCErrorCode.CONFLICT.value,
            "message": "Conflict",
            "data": None,
        }
        with pytest.raises(ConflictError) as exc_info:
            client._handle_rpc_error(error)
        assert exc_info.value.expected_etag == "(unknown)"

    def test_unknown_error_code(self, client):
        error = {
            "code": -99999,
            "message": "Unknown thing",
        }
        with pytest.raises(NexusError, match="RPC error.*-99999.*Unknown thing"):
            client._handle_rpc_error(error)

    def test_missing_code_defaults_to_internal(self, client):
        error = {"message": "Something broke"}
        with pytest.raises(NexusError, match="RPC error.*-32603"):
            client._handle_rpc_error(error)

    def test_missing_message_defaults(self, client):
        error = {"code": -32603}
        with pytest.raises(NexusError, match="Unknown error"):
            client._handle_rpc_error(error)


# ===========================================================================
# Response Parsing
# ===========================================================================


class TestParseReadResponse:
    """Tests for _parse_read_response."""

    def test_standard_bytes_format(self, client):
        """Standard {__type__: 'bytes', data: '<base64>'} should decode."""
        raw_content = b"hello world"
        result = {
            "__type__": "bytes",
            "data": base64.b64encode(raw_content).decode(),
        }
        decoded = client._parse_read_response(result)
        assert decoded == raw_content

    def test_standard_bytes_with_metadata(self, client):
        """With return_metadata=True, should return dict with decoded content."""
        raw = b"data"
        result = {"__type__": "bytes", "data": base64.b64encode(raw).decode()}
        decoded = client._parse_read_response(result, return_metadata=True)
        assert isinstance(decoded, dict)
        assert decoded["content"] == raw

    def test_legacy_base64_format(self, client):
        """Legacy {content: '<base64>', encoding: 'base64'} should decode."""
        raw = b"legacy content"
        result = {
            "content": base64.b64encode(raw).decode(),
            "encoding": "base64",
        }
        decoded = client._parse_read_response(result)
        assert decoded == raw

    def test_legacy_format_with_metadata(self, client):
        """Legacy format with return_metadata should preserve other fields."""
        raw = b"data"
        result = {
            "content": base64.b64encode(raw).decode(),
            "encoding": "base64",
            "etag": "abc123",
        }
        decoded = client._parse_read_response(result, return_metadata=True)
        assert isinstance(decoded, dict)
        assert decoded["content"] == raw
        assert decoded["etag"] == "abc123"

    def test_legacy_bytes_content(self, client):
        """Legacy format with bytes content should pass through."""
        raw = b"raw bytes"
        result = {"content": raw, "encoding": "raw"}
        decoded = client._parse_read_response(result)
        assert decoded == raw

    def test_legacy_str_content_non_base64(self, client):
        """Legacy format with non-base64 string should encode to bytes."""
        result = {"content": "plain text", "encoding": "utf-8"}
        decoded = client._parse_read_response(result)
        assert decoded == b"plain text"

    def test_raw_bytes_passthrough(self, client):
        """Raw bytes result should pass through unchanged."""
        raw = b"direct bytes"
        assert client._parse_read_response(raw) == raw

    def test_fallback_returns_raw(self, client):
        """Unrecognized format should return as-is."""
        result = "just a string"
        assert client._parse_read_response(result) == "just a string"


class TestDecodeBytesField:
    """Tests for _decode_bytes_field."""

    def test_bytes_dict_format(self, client):
        raw = b"field data"
        value = {"__type__": "bytes", "data": base64.b64encode(raw).decode()}
        assert client._decode_bytes_field(value) == raw

    def test_base64_string(self, client):
        raw = b"b64 data"
        assert client._decode_bytes_field(base64.b64encode(raw).decode()) == raw

    def test_raw_bytes_passthrough(self, client):
        raw = b"already bytes"
        assert client._decode_bytes_field(raw) == raw


class TestDecodeDeltaReadResponse:
    """Tests for _decode_delta_read_response."""

    def test_decodes_delta_field(self, client):
        delta_bytes = b"delta content"
        result = {
            "delta": {"__type__": "bytes", "data": base64.b64encode(delta_bytes).decode()},
            "version": 3,
        }
        decoded = client._decode_delta_read_response(result)
        assert decoded["delta"] == delta_bytes
        assert decoded["version"] == 3

    def test_decodes_content_field(self, client):
        content_bytes = b"full content"
        result = {
            "content": {"__type__": "bytes", "data": base64.b64encode(content_bytes).decode()},
        }
        decoded = client._decode_delta_read_response(result)
        assert decoded["content"] == content_bytes

    def test_non_bytes_fields_unchanged(self, client):
        result = {"delta": "plain string", "version": 1}
        decoded = client._decode_delta_read_response(result)
        assert decoded["delta"] == "plain string"

    def test_non_dict_result_wrapped(self, client):
        decoded = client._decode_delta_read_response("not a dict")
        assert decoded == {"result": "not a dict"}

    def test_original_dict_not_mutated(self, client):
        """The input dict should not be mutated (immutability)."""
        delta_bytes = b"data"
        original = {
            "delta": {"__type__": "bytes", "data": base64.b64encode(delta_bytes).decode()},
        }
        original_copy = dict(original)
        client._decode_delta_read_response(original)
        # The original's "delta" key should still be the dict, not decoded bytes
        assert original["delta"] == original_copy["delta"]


# ===========================================================================
# _parse_auth_info
# ===========================================================================


class TestParseAuthInfo:
    """Tests for _parse_auth_info."""

    def test_authenticated_user(self, client):
        client._parse_auth_info({
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "alice",
            "zone_id": "z1",
        })
        assert client.zone_id == "z1"
        assert client.agent_id is None  # User, not agent

    def test_authenticated_agent(self, client):
        client._parse_auth_info({
            "authenticated": True,
            "subject_type": "agent",
            "subject_id": "agent-42",
            "zone_id": "z2",
        })
        assert client.zone_id == "z2"
        assert client.agent_id == "agent-42"

    def test_unauthenticated(self, client):
        original_zone = client.zone_id
        client._parse_auth_info({"authenticated": False})
        # Zone and agent should remain unchanged (no assignment)
        assert client.zone_id == original_zone

    def test_authenticated_with_no_zone(self, client):
        client._parse_auth_info({
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "bob",
            "zone_id": None,
        })
        assert client.zone_id is None
