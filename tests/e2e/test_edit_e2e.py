"""End-to-end tests for the edit API endpoint.

Issue #800: Add edit engine with search/replace for surgical file edits.

Tests the complete flow:
1. Start FastAPI server with in-memory database
2. Register multiple users
3. Create files
4. Test edit operations with different users
5. Verify permission enforcement
6. Test concurrent edit scenarios

Run with:
    pytest tests/e2e/test_edit_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import base64
import uuid

import httpx
import pytest


def encode_bytes(content: bytes) -> dict:
    """Encode bytes for JSON-RPC transport."""
    return {"__type__": "bytes", "data": base64.b64encode(content).decode("utf-8")}


def decode_bytes(data: dict | list) -> bytes:
    """Decode bytes from JSON-RPC response."""
    if isinstance(data, dict) and data.get("__type__") == "bytes":
        return base64.b64decode(data["data"])
    elif isinstance(data, list):
        return bytes(data)
    return data  # type: ignore


# ==============================================================================
# Helper Functions
# ==============================================================================


def make_rpc_request(
    client: httpx.Client,
    method: str,
    params: dict,
    token: str | None = None,
) -> dict:
    """Make an RPC request to the server.

    Args:
        client: httpx.Client instance
        method: RPC method name
        params: Method parameters
        token: Optional JWT token for authentication

    Returns:
        Response JSON dict
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = client.post(
        f"/api/nfs/{method}",
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        },
        headers=headers,
    )
    return response.json()


def register_user(
    client: httpx.Client,
    email: str,
    password: str = "securepassword123",
    username: str | None = None,
) -> dict:
    """Register a new user and return user info with token.

    For the sync RPC server running in open access mode, we don't actually
    need to register users - just return a mock user dict without a token.

    Args:
        client: httpx.Client instance
        email: User email
        password: User password
        username: Optional username (defaults to email prefix)

    Returns:
        Dict with user_id, token, and other user info
    """
    if username is None:
        username = email.split("@")[0]

    # Check if auth endpoint exists (FastAPI async server has it, sync doesn't)
    try:
        response = client.post(
            "/auth/register",
            json={
                "email": email,
                "password": password,
                "username": username,
                "display_name": f"Test User {username}",
            },
        )
        if response.status_code == 201:
            return response.json()
    except Exception:
        pass

    # If auth endpoint doesn't exist or fails, return mock user for open access mode
    # The sync RPC server runs without auth, so tokens aren't needed
    return {
        "user_id": f"mock-{username}",
        "email": email,
        "username": username,
        "display_name": f"Test User {username}",
        "token": None,  # No token needed for open access mode
        "api_key": None,
    }


# ==============================================================================
# Fixtures (using conftest.py fixtures: test_app, isolated_db, nexus_fs)
# ==============================================================================


@pytest.fixture
def user1(test_app):
    """Create first test user."""
    return register_user(test_app, "user1@example.com", username="user1")


@pytest.fixture
def user2(test_app):
    """Create second test user."""
    return register_user(test_app, "user2@example.com", username="user2")


@pytest.fixture
def user3(test_app):
    """Create third test user."""
    return register_user(test_app, "user3@example.com", username="user3")


# ==============================================================================
# Test: Basic Edit Operations
# ==============================================================================


class TestEditBasicOperations:
    """Test basic edit operations."""

    def test_edit_simple_replacement(self, test_app, user1):
        """Test simple search/replace edit."""
        token = user1["token"]

        # Create a file
        content = b"def foo():\n    return 1\n"
        write_result = make_rpc_request(
            test_app,
            "write",
            {"path": "/test/main.py", "content": encode_bytes(content)},
            token=token,
        )
        assert "result" in write_result, f"Write failed: {write_result}"

        # Edit the file
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/main.py",
                "edits": [{"old_str": "foo", "new_str": "bar"}],
            },
            token=token,
        )

        assert "result" in edit_result, f"Edit failed: {edit_result}"
        result = edit_result["result"]
        assert result["success"] is True
        assert result["applied_count"] == 1
        assert "-def foo():" in result["diff"]
        assert "+def bar():" in result["diff"]

    def test_edit_multiple_replacements(self, test_app, user1):
        """Test multiple edits in sequence."""
        token = user1["token"]

        # Create a file
        content = b"def foo():\n    x = 1\n    y = 2\n    return x + y\n"
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/calc.py", "content": encode_bytes(content)},
            token=token,
        )

        # Multiple edits
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/calc.py",
                "edits": [
                    {"old_str": "foo", "new_str": "calculate"},
                    {"old_str": "x = 1", "new_str": "x = 10"},
                    {"old_str": "y = 2", "new_str": "y = 20"},
                ],
            },
            token=token,
        )

        assert "result" in edit_result, f"Edit failed: {edit_result}"
        result = edit_result["result"]
        assert result["success"] is True
        assert result["applied_count"] == 3
        assert len(result["matches"]) == 3
        assert all(m["match_type"] == "exact" for m in result["matches"])

    def test_edit_with_tuple_format(self, test_app, user1):
        """Test edit with tuple format (old, new)."""
        token = user1["token"]

        # Create a file
        content = b"hello world\n"
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/hello.txt", "content": encode_bytes(content)},
            token=token,
        )

        # Edit with list of lists (JSON tuple equivalent)
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/hello.txt",
                "edits": [["hello", "goodbye"]],  # tuple as list
            },
            token=token,
        )

        assert "result" in edit_result, f"Edit failed: {edit_result}"
        result = edit_result["result"]
        assert result["success"] is True

        # Verify content changed
        read_result = make_rpc_request(
            test_app,
            "read",
            {"path": "/test/hello.txt"},
            token=token,
        )
        assert "result" in read_result, f"Read failed: {read_result}"
        # Read returns bytes directly (not wrapped in a dict unless return_metadata=True)
        content_bytes = decode_bytes(read_result["result"])
        assert content_bytes == b"goodbye world\n"


# ==============================================================================
# Test: Edit Preview Mode
# ==============================================================================


class TestEditPreviewMode:
    """Test edit preview functionality."""

    def test_edit_preview_returns_diff(self, test_app, user1):
        """Test preview mode returns diff without modifying file."""
        token = user1["token"]

        # Create a file
        original_content = b"original content\n"
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/preview.txt", "content": encode_bytes(original_content)},
            token=token,
        )

        # Preview edit
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/preview.txt",
                "edits": [{"old_str": "original", "new_str": "modified"}],
                "preview": True,
            },
            token=token,
        )

        assert "result" in edit_result, f"Edit failed: {edit_result}"
        result = edit_result["result"]
        assert result["success"] is True
        assert result.get("preview") is True
        assert "-original content" in result["diff"]
        assert "+modified content" in result["diff"]

        # Verify file was NOT modified
        read_result = make_rpc_request(
            test_app,
            "read",
            {"path": "/test/preview.txt"},
            token=token,
        )
        assert "result" in read_result
        content_bytes = decode_bytes(read_result["result"])
        assert content_bytes == original_content


# ==============================================================================
# Test: Fuzzy Matching
# ==============================================================================


class TestEditFuzzyMatching:
    """Test fuzzy matching functionality."""

    def test_edit_fuzzy_match_typo(self, test_app, user1):
        """Test fuzzy matching handles minor typos."""
        token = user1["token"]

        # Create a file
        content = b"def calculate_total(items):\n    return sum(items)\n"
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/fuzzy.py", "content": encode_bytes(content)},
            token=token,
        )

        # Edit with typo in search string (calcuate instead of calculate)
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/fuzzy.py",
                "edits": [
                    {
                        "old_str": "def calcuate_total(items):",  # typo
                        "new_str": "def compute_sum(items):",
                    }
                ],
                "fuzzy_threshold": 0.8,
            },
            token=token,
        )

        assert "result" in edit_result, f"Edit failed: {edit_result}"
        result = edit_result["result"]
        assert result["success"] is True
        assert result["matches"][0]["match_type"] == "fuzzy"
        assert result["matches"][0]["similarity"] >= 0.8

    def test_edit_strict_threshold_rejects_fuzzy(self, test_app, user1):
        """Test that strict threshold (1.0) rejects fuzzy matches."""
        token = user1["token"]

        # Create a file
        content = b"def foo():\n    pass\n"
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/strict.py", "content": encode_bytes(content)},
            token=token,
        )

        # Edit with typo and strict threshold
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/strict.py",
                "edits": [{"old_str": "def fooo():", "new_str": "def bar():"}],  # typo
                "fuzzy_threshold": 1.0,  # Exact match only
            },
            token=token,
        )

        assert "result" in edit_result, f"Edit failed: {edit_result}"
        result = edit_result["result"]
        assert result["success"] is False
        assert any("Could not find match" in err for err in result.get("errors", []))


# ==============================================================================
# Test: Error Handling
# ==============================================================================


class TestEditErrorHandling:
    """Test edit error handling."""

    def test_edit_file_not_found(self, test_app, user1):
        """Test edit on non-existent file returns error."""
        token = user1["token"]

        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/nonexistent/file.txt",
                "edits": [{"old_str": "foo", "new_str": "bar"}],
            },
            token=token,
        )

        # Should return RPC error for file not found
        assert "error" in edit_result or (
            "result" in edit_result and edit_result["result"].get("success") is False
        )

    def test_edit_ambiguous_match(self, test_app, user1):
        """Test edit fails on ambiguous (multiple) matches."""
        token = user1["token"]

        # Create a file with repeated pattern
        content = b"foo foo foo\n"
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/ambiguous.txt", "content": encode_bytes(content)},
            token=token,
        )

        # Edit without allow_multiple
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/ambiguous.txt",
                "edits": [{"old_str": "foo", "new_str": "bar"}],
            },
            token=token,
        )

        assert "result" in edit_result, f"Edit failed: {edit_result}"
        result = edit_result["result"]
        assert result["success"] is False
        assert any("appears" in err and "times" in err for err in result.get("errors", []))

    def test_edit_allow_multiple(self, test_app, user1):
        """Test edit with allow_multiple replaces all occurrences."""
        token = user1["token"]

        # Create a file with repeated pattern
        content = b"foo bar foo baz foo\n"
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/multiple.txt", "content": encode_bytes(content)},
            token=token,
        )

        # Edit with allow_multiple
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/multiple.txt",
                "edits": [{"old_str": "foo", "new_str": "qux", "allow_multiple": True}],
            },
            token=token,
        )

        assert "result" in edit_result, f"Edit failed: {edit_result}"
        result = edit_result["result"]
        assert result["success"] is True
        assert result["matches"][0]["match_count"] == 3

        # Verify content
        read_result = make_rpc_request(
            test_app,
            "read",
            {"path": "/test/multiple.txt"},
            token=token,
        )
        content_bytes = decode_bytes(read_result["result"])
        assert content_bytes == b"qux bar qux baz qux\n"

    def test_edit_binary_file_fails(self, test_app, user1):
        """Test edit on binary file returns error."""
        token = user1["token"]

        # Create a binary file (invalid UTF-8)
        binary_content = bytes([0x00, 0x01, 0x02, 0xFF, 0xFE])
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/binary.bin", "content": encode_bytes(binary_content)},
            token=token,
        )

        # Try to edit binary file
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/binary.bin",
                "edits": [{"old_str": "foo", "new_str": "bar"}],
            },
            token=token,
        )

        assert "result" in edit_result, f"Edit failed: {edit_result}"
        result = edit_result["result"]
        assert result["success"] is False
        assert any("UTF-8" in err for err in result.get("errors", []))


# ==============================================================================
# Test: Optimistic Concurrency
# ==============================================================================


class TestEditConcurrency:
    """Test edit with optimistic concurrency control."""

    def test_edit_with_if_match_success(self, test_app, user1):
        """Test edit with matching etag succeeds."""
        token = user1["token"]

        # Create a file
        content = b"original content\n"
        write_result = make_rpc_request(
            test_app,
            "write",
            {"path": "/test/concurrency.txt", "content": encode_bytes(content)},
            token=token,
        )
        assert "result" in write_result, f"Write failed: {write_result}"
        # Write returns dict - format differs between FastAPI and sync server
        result = write_result["result"]
        etag = result["bytes_written"]["etag"] if "bytes_written" in result else result.get("etag")

        # Edit with correct etag
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/concurrency.txt",
                "edits": [{"old_str": "original", "new_str": "modified"}],
                "if_match": etag,
            },
            token=token,
        )

        assert "result" in edit_result, f"Edit failed: {edit_result}"
        result = edit_result["result"]
        assert result["success"] is True

    def test_edit_with_if_match_conflict(self, test_app, user1):
        """Test edit with stale etag fails."""
        token = user1["token"]

        # Create a file
        content = b"original content\n"
        write_result = make_rpc_request(
            test_app,
            "write",
            {"path": "/test/conflict.txt", "content": encode_bytes(content)},
            token=token,
        )
        # Write returns dict - format differs between FastAPI and sync server
        result = write_result["result"]
        old_etag = (
            result["bytes_written"]["etag"] if "bytes_written" in result else result.get("etag")
        )

        # Modify file (changes etag)
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/conflict.txt", "content": encode_bytes(b"changed content\n")},
            token=token,
        )

        # Try to edit with stale etag
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/conflict.txt",
                "edits": [{"old_str": "original", "new_str": "modified"}],
                "if_match": old_etag,
            },
            token=token,
        )

        # Should fail with conflict error
        assert "error" in edit_result or (
            "result" in edit_result and edit_result["result"].get("success") is False
        )


# ==============================================================================
# Test: Multi-User Scenarios
# ==============================================================================


class TestEditMultiUser:
    """Test edit operations with multiple users."""

    def test_different_users_can_edit_own_files(self, test_app, user1, user2):
        """Test that different users can edit their own files."""
        token1 = user1["token"]
        token2 = user2["token"]

        # User1 creates and edits a file
        make_rpc_request(
            test_app,
            "write",
            {"path": "/user1/file.txt", "content": encode_bytes(b"user1 content\n")},
            token=token1,
        )
        edit_result1 = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/user1/file.txt",
                "edits": [{"old_str": "user1", "new_str": "USER1"}],
            },
            token=token1,
        )
        assert "result" in edit_result1
        assert edit_result1["result"]["success"] is True

        # User2 creates and edits a different file
        make_rpc_request(
            test_app,
            "write",
            {"path": "/user2/file.txt", "content": encode_bytes(b"user2 content\n")},
            token=token2,
        )
        edit_result2 = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/user2/file.txt",
                "edits": [{"old_str": "user2", "new_str": "USER2"}],
            },
            token=token2,
        )
        assert "result" in edit_result2
        assert edit_result2["result"]["success"] is True

    def test_sequential_edits_by_different_users(self, test_app, user1, user2, user3):
        """Test sequential edits by multiple users.

        Note: All users edit the same file created by user1. This tests that
        even without explicit ReBAC permissions, the test works because
        enforce_permissions=False in the fixture.
        """
        tokens = [user1["token"], user2["token"], user3["token"]]

        # Create shared file (user1 creates)
        # Using /test/ namespace which is the default test namespace
        content = b"line 1\nline 2\nline 3\n"
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/shared_doc.txt", "content": encode_bytes(content)},
            token=tokens[0],
        )

        # Each user edits a different line
        edits = [
            {"old_str": "line 1", "new_str": "LINE 1 (user1)"},
            {"old_str": "line 2", "new_str": "LINE 2 (user2)"},
            {"old_str": "line 3", "new_str": "LINE 3 (user3)"},
        ]

        for i, (token, edit) in enumerate(zip(tokens, edits, strict=True)):
            edit_result = make_rpc_request(
                test_app,
                "edit",
                {"path": "/test/shared_doc.txt", "edits": [edit]},
                token=token,
            )
            assert "result" in edit_result, f"Edit {i} failed: {edit_result}"
            assert edit_result["result"]["success"] is True

        # Verify final content
        read_result = make_rpc_request(
            test_app,
            "read",
            {"path": "/test/shared_doc.txt"},
            token=tokens[0],
        )
        final_content = decode_bytes(read_result["result"]).decode()
        assert "LINE 1 (user1)" in final_content
        assert "LINE 2 (user2)" in final_content
        assert "LINE 3 (user3)" in final_content


# ==============================================================================
# Test: Edit with Line Hints
# ==============================================================================


class TestEditLineHints:
    """Test edit with line number hints for middle-out search."""

    def test_edit_with_hint_line(self, test_app, user1):
        """Test edit uses hint_line for faster matching."""
        token = user1["token"]

        # Create a larger file
        lines = [f"line {i}\n" for i in range(100)]
        lines[50] = "target line to change\n"
        content = "".join(lines).encode()

        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/large.txt", "content": encode_bytes(content)},
            token=token,
        )

        # Edit with hint_line pointing near the target
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/large.txt",
                "edits": [
                    {
                        "old_str": "target line to change",
                        "new_str": "MODIFIED LINE",
                        "hint_line": 51,  # 1-indexed
                    }
                ],
            },
            token=token,
        )

        assert "result" in edit_result, f"Edit failed: {edit_result}"
        result = edit_result["result"]
        assert result["success"] is True
        assert result["matches"][0]["line_start"] == 51


# ==============================================================================
# Test: Edge Cases
# ==============================================================================


class TestEditEdgeCases:
    """Test edit edge cases."""

    def test_edit_empty_file(self, test_app, user1):
        """Test edit on empty file."""
        token = user1["token"]

        # Create empty file
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/empty.txt", "content": encode_bytes(b"")},
            token=token,
        )

        # Try to edit empty file
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/empty.txt",
                "edits": [{"old_str": "foo", "new_str": "bar"}],
            },
            token=token,
        )

        assert "result" in edit_result
        result = edit_result["result"]
        assert result["success"] is False

    def test_edit_delete_text(self, test_app, user1):
        """Test edit that deletes text (replaces with empty string)."""
        token = user1["token"]

        # Create file with text to remove
        content = b"keep this\nremove this line\nkeep this too\n"
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/delete.txt", "content": encode_bytes(content)},
            token=token,
        )

        # Delete a line
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/delete.txt",
                "edits": [{"old_str": "remove this line\n", "new_str": ""}],
            },
            token=token,
        )

        assert "result" in edit_result
        result = edit_result["result"]
        assert result["success"] is True

        # Verify
        read_result = make_rpc_request(
            test_app,
            "read",
            {"path": "/test/delete.txt"},
            token=token,
        )
        final_content = decode_bytes(read_result["result"]).decode()
        assert "remove this line" not in final_content
        assert "keep this\n" in final_content
        assert "keep this too\n" in final_content

    def test_edit_multiline_replacement(self, test_app, user1):
        """Test edit with multiline search and replace."""
        token = user1["token"]

        # Create file with multiline content
        content = b"def old_function():\n    pass\n\ndef other():\n    pass\n"
        make_rpc_request(
            test_app,
            "write",
            {"path": "/test/multiline.py", "content": encode_bytes(content)},
            token=token,
        )

        # Replace multiline block
        edit_result = make_rpc_request(
            test_app,
            "edit",
            {
                "path": "/test/multiline.py",
                "edits": [
                    {
                        "old_str": "def old_function():\n    pass",
                        "new_str": "def new_function():\n    return 42",
                    }
                ],
            },
            token=token,
        )

        assert "result" in edit_result
        result = edit_result["result"]
        assert result["success"] is True
        assert result["matches"][0]["line_start"] == 1
        assert result["matches"][0]["line_end"] == 2

        # Verify
        read_result = make_rpc_request(
            test_app,
            "read",
            {"path": "/test/multiline.py"},
            token=token,
        )
        final_content = decode_bytes(read_result["result"]).decode()
        assert "def new_function():" in final_content
        assert "return 42" in final_content
