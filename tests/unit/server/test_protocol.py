"""Unit tests for RPC protocol."""

from datetime import datetime

import pytest

from nexus.server.protocol import (
    RPCEncoder,
    RPCErrorCode,
    RPCRequest,
    RPCResponse,
    decode_rpc_message,
    encode_rpc_message,
    parse_method_params,
)


class TestRPCRequest:
    """Tests for RPCRequest class."""

    def test_from_dict(self):
        """Test creating RPCRequest from dict."""
        data = {
            "jsonrpc": "2.0",
            "id": "test-123",
            "method": "read",
            "params": {"path": "/test.txt"},
        }
        request = RPCRequest.from_dict(data)
        assert request.jsonrpc == "2.0"
        assert request.id == "test-123"
        assert request.method == "read"
        assert request.params == {"path": "/test.txt"}

    def test_to_dict(self):
        """Test converting RPCRequest to dict."""
        request = RPCRequest(
            jsonrpc="2.0", id="test-456", method="write", params={"path": "/file.txt"}
        )
        result = request.to_dict()
        assert result["jsonrpc"] == "2.0"
        assert result["id"] == "test-456"
        assert result["method"] == "write"
        assert result["params"] == {"path": "/file.txt"}


class TestRPCResponse:
    """Tests for RPCResponse class."""

    def test_success_response(self):
        """Test creating success response."""
        response = RPCResponse.success("req-1", {"result": "ok"})
        assert response.id == "req-1"
        assert response.result == {"result": "ok"}
        assert response.error is None

    def test_error_response(self):
        """Test creating error response."""
        response = RPCResponse.create_error(
            "req-2", RPCErrorCode.FILE_NOT_FOUND, "File not found", data={"path": "/missing.txt"}
        )
        assert response.id == "req-2"
        assert response.result is None
        assert response.error is not None
        assert response.error["code"] == -32000
        assert response.error["message"] == "File not found"
        assert response.error["data"] == {"path": "/missing.txt"}

    def test_to_dict_success(self):
        """Test converting success response to dict."""
        response = RPCResponse.success("req-3", {"files": ["/a.txt", "/b.txt"]})
        result = response.to_dict()
        assert result["jsonrpc"] == "2.0"
        assert result["id"] == "req-3"
        assert result["result"] == {"files": ["/a.txt", "/b.txt"]}
        assert "error" not in result

    def test_to_dict_error(self):
        """Test converting error response to dict."""
        response = RPCResponse.create_error("req-4", RPCErrorCode.INVALID_PATH, "Invalid path")
        result = response.to_dict()
        assert result["jsonrpc"] == "2.0"
        assert result["id"] == "req-4"
        assert "result" not in result
        assert result["error"]["code"] == -32002
        assert result["error"]["message"] == "Invalid path"


class TestRPCEncoder:
    """Tests for custom JSON encoder."""

    def test_encode_bytes(self):
        """Test encoding bytes."""
        import json

        data = {"content": b"Hello, World!"}
        encoded = json.dumps(data, cls=RPCEncoder)
        assert "__type__" in encoded
        assert "bytes" in encoded

    def test_encode_datetime(self):
        """Test encoding datetime."""
        import json

        dt = datetime(2024, 1, 15, 10, 30, 45)
        data = {"timestamp": dt}
        encoded = json.dumps(data, cls=RPCEncoder)
        assert "__type__" in encoded
        assert "datetime" in encoded
        assert "2024-01-15" in encoded

    def test_encode_object_with_dict(self):
        """Test encoding objects with __dict__."""
        import json

        class TestObject:
            def __init__(self):
                self.value = 42
                self.name = "test"

            def some_method(self):
                pass

        obj = TestObject()
        data = {"obj": obj}
        encoded = json.dumps(data, cls=RPCEncoder)
        decoded = json.loads(encoded)
        assert decoded["obj"]["value"] == 42
        assert decoded["obj"]["name"] == "test"
        assert "some_method" not in decoded["obj"]  # Methods should be filtered


class TestEncodeDecodeRPCMessage:
    """Tests for encoding/decoding RPC messages."""

    def test_encode_decode_simple(self):
        """Test encoding and decoding simple message."""
        data = {"jsonrpc": "2.0", "id": "1", "result": {"value": 123}}
        encoded = encode_rpc_message(data)
        decoded = decode_rpc_message(encoded)
        assert decoded == data

    def test_encode_decode_with_bytes(self):
        """Test encoding and decoding message with bytes."""
        data = {"jsonrpc": "2.0", "id": "2", "result": {"content": b"Test data"}}
        encoded = encode_rpc_message(data)
        decoded = decode_rpc_message(encoded)
        assert decoded["result"]["content"] == b"Test data"

    def test_encode_decode_with_datetime(self):
        """Test encoding and decoding message with datetime."""
        dt = datetime(2024, 10, 19, 12, 0, 0)
        data = {"jsonrpc": "2.0", "id": "3", "result": {"timestamp": dt}}
        encoded = encode_rpc_message(data)
        decoded = decode_rpc_message(encoded)
        # Note: microseconds might differ slightly
        assert decoded["result"]["timestamp"].year == 2024
        assert decoded["result"]["timestamp"].month == 10
        assert decoded["result"]["timestamp"].day == 19


class TestParseMethodParams:
    """Tests for parse_method_params function."""

    def test_parse_read_params(self):
        """Test parsing read method parameters."""
        params = parse_method_params("read", {"path": "/test.txt"})
        assert params.path == "/test.txt"

    def test_parse_write_params(self):
        """Test parsing write method parameters."""
        params = parse_method_params("write", {"path": "/file.txt", "content": b"data"})
        assert params.path == "/file.txt"
        assert params.content == b"data"

    def test_parse_list_params(self):
        """Test parsing list method parameters."""
        params = parse_method_params(
            "list", {"path": "/workspace", "recursive": True, "details": False}
        )
        assert params.path == "/workspace"
        assert params.recursive is True
        assert params.details is False

    def test_parse_list_params_defaults(self):
        """Test parsing list with default parameters."""
        params = parse_method_params("list", {})
        assert params.path == "/"
        assert params.recursive is True
        assert params.details is False

    def test_parse_unknown_method(self):
        """Test parsing unknown method raises error."""
        with pytest.raises(ValueError, match="Unknown method"):
            parse_method_params("unknown_method", {})

    def test_parse_invalid_params(self):
        """Test parsing with invalid parameters raises error."""
        with pytest.raises(ValueError, match="Invalid parameters"):
            parse_method_params("read", {"invalid_param": "value"})


class TestRPCErrorCode:
    """Tests for RPCErrorCode enum."""

    def test_error_codes(self):
        """Test error code values."""
        assert RPCErrorCode.FILE_NOT_FOUND.value == -32000
        assert RPCErrorCode.INVALID_PATH.value == -32002
        assert RPCErrorCode.INTERNAL_ERROR.value == -32603
        assert RPCErrorCode.PARSE_ERROR.value == -32700
