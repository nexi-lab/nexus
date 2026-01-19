"""End-to-end tests for CDC Chunked Storage via FastAPI HTTP endpoints (Issue #1074).

Tests chunked storage through actual HTTP API calls to verify the full stack:
FastAPI -> NexusFS -> LocalBackend (chunked storage)

Run with:
    pytest tests/e2e/test_chunked_storage_api_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import base64
import os

import pytest

from nexus.backends.chunked_storage import CDC_THRESHOLD_BYTES


def encode_bytes_param(data: bytes) -> dict:
    """Encode bytes for RPC transmission.

    The RPC protocol expects bytes as {"__type__": "bytes", "data": "<base64>"}
    """
    return {"__type__": "bytes", "data": base64.b64encode(data).decode("utf-8")}


def rpc_call(test_app, method: str, params: dict, timeout: float = 30.0):
    """Make an RPC call to the NexusFS API."""
    response = test_app.post(
        f"/api/nfs/{method}",
        json={"jsonrpc": "2.0", "method": method, "params": params},
        timeout=timeout,
    )
    return response


def decode_bytes_result(result):
    """Decode bytes from RPC response.

    The RPC response encodes bytes as {"__type__": "bytes", "data": "<base64>"}
    """
    if isinstance(result, dict) and result.get("__type__") == "bytes":
        return base64.b64decode(result["data"])
    elif isinstance(result, str):
        return base64.b64decode(result)
    elif isinstance(result, bytes):
        return result
    else:
        raise ValueError(f"Unexpected result type: {type(result)}")


class TestChunkedStorageHTTPAPI:
    """End-to-end tests for chunked storage through FastAPI HTTP endpoints."""

    @pytest.mark.asyncio
    async def test_small_file_write_read_api(self, test_app):
        """Test small file write/read through HTTP API."""
        content = b"This is a small test file content via HTTP API."
        path = "/api_test_small.txt"

        # Write via HTTP API - use proper bytes encoding
        response = rpc_call(test_app, "write", {
            "path": path,
            "content": encode_bytes_param(content),
        })
        assert response.status_code == 200, f"Write failed: {response.text}"
        result = response.json()
        assert result.get("error") is None, f"Write error: {result.get('error')}"

        # Read back via HTTP API
        response = rpc_call(test_app, "read", {"path": path})
        assert response.status_code == 200, f"Read failed: {response.text}"
        result = response.json()
        assert result.get("error") is None, f"Read error: {result.get('error')}"
        read_content = decode_bytes_result(result["result"])
        assert read_content == content

        # Clean up
        rpc_call(test_app, "delete", {"path": path})

    @pytest.mark.asyncio
    async def test_large_file_chunked_write_read_api(self, test_app):
        """Test large file (chunked) write/read through HTTP API."""
        # Create content larger than CDC threshold (~17MB)
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)
        path = "/api_test_large.bin"

        # Write large file via HTTP API
        response = rpc_call(test_app, "write", {
            "path": path,
            "content": encode_bytes_param(large_content),
        }, timeout=120.0)
        assert response.status_code == 200, f"Write failed: {response.text}"
        result = response.json()
        assert result.get("error") is None, f"Write error: {result.get('error')}"

        # Read back via HTTP API
        response = rpc_call(test_app, "read", {"path": path}, timeout=120.0)
        assert response.status_code == 200, f"Read failed: {response.text}"
        result = response.json()
        assert result.get("error") is None, f"Read error: {result.get('error')}"
        read_content = decode_bytes_result(result["result"])
        assert read_content == large_content, "Content mismatch after chunked read via API"

        # Clean up
        rpc_call(test_app, "delete", {"path": path}, timeout=60.0)

    @pytest.mark.asyncio
    async def test_file_metadata_size_correct_api(self, test_app):
        """Test that file metadata returns correct size for chunked files."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 500_000)
        original_size = len(large_content)
        path = "/api_test_size.bin"

        # Write
        response = rpc_call(test_app, "write", {
            "path": path,
            "content": encode_bytes_param(large_content),
        }, timeout=120.0)
        assert response.status_code == 200
        assert response.json().get("error") is None

        # Get metadata
        response = rpc_call(test_app, "get_metadata", {"path": path})
        assert response.status_code == 200
        result = response.json()
        assert result.get("error") is None, f"Metadata error: {result.get('error')}"
        metadata = result.get("result")
        assert metadata is not None
        # get_metadata returns a dict with metadata inside
        if isinstance(metadata, dict) and "metadata" in metadata:
            metadata = metadata["metadata"]
        if metadata is not None:
            assert metadata["size"] == original_size, "Metadata size should match original content size"

        # Clean up
        rpc_call(test_app, "delete", {"path": path}, timeout=60.0)

    @pytest.mark.asyncio
    async def test_chunked_deduplication_api(self, test_app):
        """Test that identical large files are deduplicated."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)
        path_a = "/api_dedup_a.bin"
        path_b = "/api_dedup_b.bin"
        encoded_content = encode_bytes_param(large_content)

        # Write same content twice
        response = rpc_call(test_app, "write", {"path": path_a, "content": encoded_content}, timeout=120.0)
        assert response.status_code == 200
        assert response.json().get("error") is None

        response = rpc_call(test_app, "write", {"path": path_b, "content": encoded_content}, timeout=120.0)
        assert response.status_code == 200
        assert response.json().get("error") is None

        # Get ETags - same content should have same ETag
        response = rpc_call(test_app, "get_etag", {"path": path_a})
        assert response.status_code == 200
        etag_a = response.json().get("result")

        response = rpc_call(test_app, "get_etag", {"path": path_b})
        assert response.status_code == 200
        etag_b = response.json().get("result")

        assert etag_a == etag_b, "Same content should have same ETag (deduplicated)"

        # Read both back
        response = rpc_call(test_app, "read", {"path": path_a}, timeout=120.0)
        assert decode_bytes_result(response.json()["result"]) == large_content

        response = rpc_call(test_app, "read", {"path": path_b}, timeout=120.0)
        assert decode_bytes_result(response.json()["result"]) == large_content

        # Delete one, other should still work
        rpc_call(test_app, "delete", {"path": path_a}, timeout=60.0)

        response = rpc_call(test_app, "read", {"path": path_b}, timeout=120.0)
        assert response.status_code == 200
        assert decode_bytes_result(response.json()["result"]) == large_content

        # Clean up
        rpc_call(test_app, "delete", {"path": path_b}, timeout=60.0)

    @pytest.mark.asyncio
    async def test_mixed_file_sizes_api(self, test_app):
        """Test that small and large files work together via API."""
        small_content = b"Small file content via API"
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 100_000)

        small_path = "/api_mixed_small.txt"
        large_path = "/api_mixed_large.bin"

        # Write both
        response = rpc_call(test_app, "write", {
            "path": small_path,
            "content": encode_bytes_param(small_content),
        })
        assert response.status_code == 200
        assert response.json().get("error") is None

        response = rpc_call(test_app, "write", {
            "path": large_path,
            "content": encode_bytes_param(large_content),
        }, timeout=120.0)
        assert response.status_code == 200
        assert response.json().get("error") is None

        # Read both back
        response = rpc_call(test_app, "read", {"path": small_path})
        assert decode_bytes_result(response.json()["result"]) == small_content

        response = rpc_call(test_app, "read", {"path": large_path}, timeout=120.0)
        assert decode_bytes_result(response.json()["result"]) == large_content

        # List directory
        response = rpc_call(test_app, "list", {"path": "/"})
        assert response.status_code == 200
        result = response.json().get("result", {})
        # list returns {"files": [...], "has_more": ..., "next_cursor": ...}
        files = result.get("files", []) if isinstance(result, dict) else result
        file_paths = [f if isinstance(f, str) else f.get("path", "") for f in files]
        assert any("api_mixed_small.txt" in p for p in file_paths)
        assert any("api_mixed_large.bin" in p for p in file_paths)

        # Clean up
        rpc_call(test_app, "delete", {"path": small_path})
        rpc_call(test_app, "delete", {"path": large_path}, timeout=60.0)

    @pytest.mark.asyncio
    async def test_overwrite_small_to_large_api(self, test_app):
        """Test overwriting a small file with a large chunked file via API."""
        small_content = b"Initial small content"
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 500_000)
        path = "/api_overwrite.bin"

        # Write small
        response = rpc_call(test_app, "write", {
            "path": path,
            "content": encode_bytes_param(small_content),
        })
        assert response.status_code == 200
        assert response.json().get("error") is None

        # Verify small content
        response = rpc_call(test_app, "read", {"path": path})
        assert decode_bytes_result(response.json()["result"]) == small_content

        # Overwrite with large
        response = rpc_call(test_app, "write", {
            "path": path,
            "content": encode_bytes_param(large_content),
        }, timeout=120.0)
        assert response.status_code == 200
        assert response.json().get("error") is None

        # Verify large content
        response = rpc_call(test_app, "read", {"path": path}, timeout=120.0)
        assert decode_bytes_result(response.json()["result"]) == large_content

        # Clean up
        rpc_call(test_app, "delete", {"path": path}, timeout=60.0)

    @pytest.mark.asyncio
    async def test_delete_chunked_file_api(self, test_app):
        """Test deleting a chunked file via API."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 100_000)
        path = "/api_delete_test.bin"

        # Write
        response = rpc_call(test_app, "write", {
            "path": path,
            "content": encode_bytes_param(large_content),
        }, timeout=120.0)
        assert response.status_code == 200
        assert response.json().get("error") is None

        # Verify exists via read
        response = rpc_call(test_app, "read", {"path": path}, timeout=120.0)
        assert response.status_code == 200
        assert response.json().get("error") is None

        # Delete
        response = rpc_call(test_app, "delete", {"path": path}, timeout=60.0)
        assert response.status_code == 200
        assert response.json().get("error") is None

        # Verify deleted - read should fail
        response = rpc_call(test_app, "read", {"path": path})
        # Either returns an error or returns None
        result = response.json()
        # Should have an error (file not found)
        assert result.get("error") is not None or result.get("result") is None
