"""
E2E test for Issue #1147: Race Condition in List API After File Creation.

This test verifies that newly created files are immediately visible in the list API,
testing the predicate pushdown optimization with Tiger Cache.

The race condition occurs when:
1. File is created and added to Tiger Cache bitmap
2. List API is called immediately after
3. The list API uses cached bitmap that doesn't include the new file
4. Result: File is filtered out by database JOIN before permission checking

Best practices from research:
- Read-Your-Writes Consistency: Ensure writes are visible to subsequent reads
- Write-Through Caching: Update cache synchronously with database writes
- Fallback Pattern: Query without cache filter as safety net
- Lease Pattern: Prevent stale cache writes with tokens

Run with:
    pytest tests/e2e/test_list_race_condition_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import base64
import uuid

import httpx
import pytest


def make_rpc_request(
    client: httpx.Client,
    method: str,
    params: dict,
    token: str | None = None,
) -> dict:
    """Make an RPC request to the server."""
    headers = {"X-Nexus-Zone-ID": "system"}
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


def encode_bytes(content: str | bytes) -> dict:
    """Encode content for JSON-RPC transport."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return {"__type__": "bytes", "data": base64.b64encode(content).decode("utf-8")}


def parse_list_result(list_response: dict) -> list[dict]:
    """Parse list API response to extract file entries.

    The list API returns a paginated response:
    {"result": {"files": [...], "has_more": bool, "next_cursor": str|None}}
    Or in non-paginated mode:
    {"result": [...]}
    """
    result = list_response.get("result", {})
    if isinstance(result, dict) and "files" in result:
        return result["files"]
    elif isinstance(result, list):
        return result
    return []


def get_file_names(files: list) -> list[str]:
    """Extract file names from list result entries."""
    names = []
    for f in files:
        if isinstance(f, dict):
            # Try path first, then name
            name = f.get("path", f.get("name", ""))
            if "/" in name:
                name = name.split("/")[-1]
            names.append(name)
        else:
            names.append(str(f))
    return names


class TestListRaceConditionE2E:
    """E2E tests for Issue #1147: List API race condition with newly created files."""

    def test_newly_created_file_visible_in_list(self, test_app: httpx.Client):
        """Test that a newly created file is immediately visible in list API.

        This is the basic case - create a file and immediately list it.
        """
        # Create a unique test directory
        unique_id = str(uuid.uuid4())[:8]
        test_dir = f"/zone:system/test_race_{unique_id}"
        test_file = f"{test_dir}/test_file.txt"

        # Step 1: Create the file
        write_result = make_rpc_request(
            test_app,
            "write",
            {
                "path": test_file,
                "content": encode_bytes("test content"),
            },
        )
        assert "error" not in write_result, f"Write failed: {write_result}"

        # Step 2: Immediately list the directory (no delay)
        list_result = make_rpc_request(
            test_app,
            "list",
            {
                "path": test_dir,
                "recursive": False,
            },
        )
        assert "error" not in list_result, f"List failed: {list_result}"

        # Step 3: Verify the file is visible
        files = parse_list_result(list_result)
        file_names = get_file_names(files)

        assert any("test_file.txt" in name for name in file_names), (
            f"Newly created file not visible in list! "
            f"Expected to find 'test_file.txt' in {file_names}"
        )

    def test_provision_user_agents_visible_in_list(self, test_app: httpx.Client):
        """Test that provisioned user's agent config.yaml files are immediately visible.

        This reproduces the exact issue from #1147:
        - provision_user creates agents with config.yaml
        - Immediately listing the agent directory should show config.yaml
        """
        unique_id = str(uuid.uuid4())[:8]
        test_user_id = f"testuser_{unique_id}"

        # Step 1: Provision user (creates agents)
        provision_result = make_rpc_request(
            test_app,
            "provision_user",
            {
                "user_id": test_user_id,
                "email": f"{test_user_id}@test.com",
                "display_name": f"Test User {unique_id}",
                "create_agents": True,
                "import_skills": False,  # Skip skills to speed up test
            },
        )
        # provision_user might not be exposed as RPC - skip if not available
        if (
            "error" in provision_result
            and "not found" in str(provision_result.get("error", "")).lower()
        ):
            pytest.skip("provision_user not available as RPC method")

        assert "error" not in provision_result, f"Provision failed: {provision_result}"

        # Step 2: Immediately list agent directories (no delay)
        agent_names = ["ImpersonatedUser", "UntrustedAgent", "SkillBuilder"]

        for agent_name in agent_names:
            agent_dir = f"/zone:system/user:{test_user_id}/agent/{agent_name}"

            list_result = make_rpc_request(
                test_app,
                "list",
                {
                    "path": agent_dir,
                    "recursive": False,
                },
            )

            if "error" in list_result:
                # Directory might not exist if agent creation is optional
                continue

            files = parse_list_result(list_result)
            file_names = get_file_names(files)

            # The config.yaml should be visible
            has_config = any("config.yaml" in name for name in file_names)

            # Also try to read the file directly to verify it exists
            config_path = f"{agent_dir}/config.yaml"
            read_result = make_rpc_request(
                test_app,
                "read",
                {
                    "path": config_path,
                },
            )

            file_exists = "error" not in read_result

            # This is the race condition: file exists (can be read) but not in list
            if file_exists and not has_config:
                pytest.fail(
                    f"RACE CONDITION DETECTED for {agent_name}!\n"
                    f"  - READ succeeded: config.yaml exists and is readable\n"
                    f"  - LIST returned {len(files)} items: {file_names}\n"
                    f"  - config.yaml is NOT visible in list!\n"
                    f"  This confirms Issue #1147 - predicate pushdown cache staleness"
                )

    def test_rapid_create_list_sequence(self, test_app: httpx.Client):
        """Test rapid file creation followed by list operations.

        Creates multiple files rapidly and verifies all are visible in list.
        This stress-tests the cache update mechanism.
        """
        unique_id = str(uuid.uuid4())[:8]
        test_dir = f"/zone:system/rapid_test_{unique_id}"
        num_files = 5

        created_files = []

        # Create files rapidly
        for i in range(num_files):
            file_path = f"{test_dir}/file_{i}.txt"
            write_result = make_rpc_request(
                test_app,
                "write",
                {
                    "path": file_path,
                    "content": encode_bytes(f"content {i}"),
                },
            )
            assert "error" not in write_result, f"Write failed: {write_result}"
            created_files.append(f"file_{i}.txt")

        # Immediately list (no delay)
        list_result = make_rpc_request(
            test_app,
            "list",
            {
                "path": test_dir,
                "recursive": False,
            },
        )
        assert "error" not in list_result, f"List failed: {list_result}"

        files = parse_list_result(list_result)
        listed_names = set(get_file_names(files))

        # All files should be visible
        missing = set(created_files) - {name.split("/")[-1] for name in listed_names}
        if missing:
            pytest.fail(
                f"RACE CONDITION: {len(missing)} files not visible immediately after creation!\n"
                f"  Created: {created_files}\n"
                f"  Listed: {list(listed_names)}\n"
                f"  Missing: {list(missing)}"
            )

    def test_cross_request_cache_staleness(self, nexus_server):
        """Test cache staleness across separate HTTP clients.

        Simulates the multi-process scenario where different requests
        might have different cache states.
        """
        unique_id = str(uuid.uuid4())[:8]
        test_dir = f"/zone:system/cross_client_{unique_id}"
        test_file = f"{test_dir}/new_file.txt"

        base_url = nexus_server["base_url"]

        # Client A: List directory first to populate cache
        with httpx.Client(base_url=base_url, timeout=30.0, trust_env=False) as client_a:
            # Prime the cache by listing
            make_rpc_request(
                client_a,
                "list",
                {
                    "path": test_dir,
                    "recursive": False,
                },
            )
            # Directory might not exist yet, that's OK

        # Client B: Create file
        with httpx.Client(base_url=base_url, timeout=30.0, trust_env=False) as client_b:
            write_result = make_rpc_request(
                client_b,
                "write",
                {
                    "path": test_file,
                    "content": encode_bytes("new content"),
                },
            )
            assert "error" not in write_result, f"Write failed: {write_result}"

        # Client A again: List should show new file
        with httpx.Client(base_url=base_url, timeout=30.0, trust_env=False) as client_a:
            list_result = make_rpc_request(
                client_a,
                "list",
                {
                    "path": test_dir,
                    "recursive": False,
                },
            )
            assert "error" not in list_result, f"List failed: {list_result}"

            files = parse_list_result(list_result)
            file_names = get_file_names(files)

            assert any("new_file.txt" in name for name in file_names), (
                f"Cross-client race condition: File created by client B "
                f"not visible to client A. Files: {file_names}"
            )


class TestPushdownFallbackBehavior:
    """Tests for the predicate pushdown fallback mechanism."""

    def test_list_works_without_cache(self, test_app: httpx.Client):
        """Test that list works correctly even when cache is empty/cold.

        Verifies the fallback to full scan when no cached bitmap exists.
        """
        unique_id = str(uuid.uuid4())[:8]
        test_dir = f"/zone:system/no_cache_{unique_id}"
        test_file = f"{test_dir}/file.txt"

        # Create file
        write_result = make_rpc_request(
            test_app,
            "write",
            {
                "path": test_file,
                "content": encode_bytes("test"),
            },
        )
        assert "error" not in write_result, f"Write failed: {write_result}"

        # List should work via fallback
        list_result = make_rpc_request(
            test_app,
            "list",
            {
                "path": test_dir,
                "recursive": False,
            },
        )
        assert "error" not in list_result, f"List failed: {list_result}"

        files = parse_list_result(list_result)
        assert len(files) >= 1, "List fallback should return the file"

    def test_list_consistency_multiple_calls(self, test_app: httpx.Client):
        """Test that multiple list calls return consistent results.

        If there's a cache staleness issue, results might vary between calls.
        """
        unique_id = str(uuid.uuid4())[:8]
        test_dir = f"/zone:system/consistency_{unique_id}"

        # Create files
        for i in range(3):
            write_result = make_rpc_request(
                test_app,
                "write",
                {
                    "path": f"{test_dir}/file_{i}.txt",
                    "content": encode_bytes(f"content {i}"),
                },
            )
            assert "error" not in write_result

        # List multiple times and verify consistency
        results = []
        for _ in range(5):
            list_result = make_rpc_request(
                test_app,
                "list",
                {
                    "path": test_dir,
                    "recursive": False,
                },
            )
            assert "error" not in list_result, f"List failed: {list_result}"
            files = parse_list_result(list_result)
            results.append(len(files))

        # All results should be the same
        assert len(set(results)) == 1, (
            f"Inconsistent list results across calls: {results}. "
            f"This suggests cache staleness issues."
        )


class TestReadYourWritesConsistency:
    """Tests specifically for read-your-writes consistency pattern."""

    def test_write_then_read_same_request(self, test_app: httpx.Client):
        """Test that a write is immediately readable in the same logical request."""
        unique_id = str(uuid.uuid4())[:8]
        test_file = f"/zone:system/ryw_{unique_id}/doc.txt"
        content = f"content_{unique_id}"

        # Write
        write_result = make_rpc_request(
            test_app,
            "write",
            {
                "path": test_file,
                "content": encode_bytes(content),
            },
        )
        assert "error" not in write_result

        # Immediate read
        read_result = make_rpc_request(
            test_app,
            "read",
            {
                "path": test_file,
            },
        )
        assert "error" not in read_result, f"Read failed: {read_result}"

    def test_write_then_list_then_read(self, test_app: httpx.Client):
        """Test the full cycle: write -> list (should show file) -> read."""
        unique_id = str(uuid.uuid4())[:8]
        test_dir = f"/zone:system/full_cycle_{unique_id}"
        test_file = f"{test_dir}/document.txt"

        # Write
        write_result = make_rpc_request(
            test_app,
            "write",
            {
                "path": test_file,
                "content": encode_bytes("full cycle test"),
            },
        )
        assert "error" not in write_result

        # List
        list_result = make_rpc_request(
            test_app,
            "list",
            {
                "path": test_dir,
                "recursive": False,
            },
        )
        assert "error" not in list_result

        files = parse_list_result(list_result)
        file_names = get_file_names(files)
        assert any("document.txt" in name for name in file_names), f"File not in list: {file_names}"

        # Read
        read_result = make_rpc_request(
            test_app,
            "read",
            {
                "path": test_file,
            },
        )
        assert "error" not in read_result
