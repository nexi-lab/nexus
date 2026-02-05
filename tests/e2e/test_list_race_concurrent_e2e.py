"""
E2E test for Issue #1147: CONCURRENT Race Condition in List API.

This test attempts to trigger the TOCTOU (time-of-check-time-of-use) race:
1. Request A starts list, calls get_accessible_int_ids() -> gets bitmap {1,2,3}
2. Request B writes file -> updates bitmap to {1,2,3,4}
3. Request A continues with stale bitmap -> misses file 4

Run with:
    pytest tests/e2e/test_list_race_concurrent_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import base64
import concurrent.futures
import uuid
from typing import Any

import httpx
import pytest


def make_rpc_request(
    client: httpx.Client,
    method: str,
    params: dict,
) -> dict:
    """Make an RPC request to the server."""
    headers = {"X-Nexus-Zone-ID": "system"}
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
    if isinstance(content, str):
        content = content.encode("utf-8")
    return {"__type__": "bytes", "data": base64.b64encode(content).decode("utf-8")}


def parse_list_result(list_response: dict) -> list[dict]:
    result = list_response.get("result", {})
    if isinstance(result, dict) and "files" in result:
        return result["files"]
    elif isinstance(result, list):
        return result
    return []


def get_file_names(files: list) -> list[str]:
    names = []
    for f in files:
        if isinstance(f, dict):
            name = f.get("path", f.get("name", ""))
            if "/" in name:
                name = name.split("/")[-1]
            names.append(name)
        else:
            names.append(str(f))
    return names


class TestConcurrentRaceCondition:
    """Tests that attempt to trigger race condition with concurrent requests."""

    def test_concurrent_write_and_list(self, nexus_server):
        """Stress test: concurrent writes and lists to trigger TOCTOU race.

        Creates multiple files while simultaneously listing the directory.
        If race condition exists, some lists will miss recently created files.
        """
        base_url = nexus_server["base_url"]
        unique_id = str(uuid.uuid4())[:8]
        test_dir = f"/zone:system/race_stress_{unique_id}"

        num_files = 8  # Reduced to avoid overwhelming test server
        num_list_ops = 15  # Reduced to avoid overwhelming test server

        def write_file(file_num: int) -> dict[str, Any]:
            """Write a file and return its name."""
            with httpx.Client(base_url=base_url, timeout=90.0, trust_env=False) as client:
                file_path = f"{test_dir}/file_{file_num:03d}.txt"
                result = make_rpc_request(
                    client,
                    "write",
                    {"path": file_path, "content": encode_bytes(f"content {file_num}")},
                )
                return {"file_num": file_num, "success": "error" not in result}

        def list_directory() -> dict[str, Any]:
            """List directory and return file count."""
            with httpx.Client(base_url=base_url, timeout=90.0, trust_env=False) as client:
                result = make_rpc_request(
                    client,
                    "list",
                    {"path": test_dir, "recursive": False},
                )
                if "error" not in result:
                    files = parse_list_result(result)
                    return {"count": len(files), "names": get_file_names(files)}
                return {"count": 0, "names": [], "error": result.get("error")}

        # First create some baseline files
        baseline_count = 5
        for i in range(baseline_count):
            write_file(i)

        # Now do concurrent writes and lists (reduced concurrency for stability)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            # Submit interleaved writes and lists
            futures = []
            for i in range(5, num_files):
                futures.append(executor.submit(write_file, i))
                # Submit multiple list operations between writes
                for _ in range(num_list_ops // (num_files - 5)):
                    futures.append(executor.submit(list_directory))

            # Collect results
            write_results = []
            list_results = []
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if "file_num" in result:
                    write_results.append(result)
                else:
                    list_results.append(result)

        # After all operations complete, verify final state
        with httpx.Client(base_url=base_url, timeout=90.0, trust_env=False) as client:
            final_result = make_rpc_request(
                client,
                "list",
                {"path": test_dir, "recursive": False},
            )

        final_files = parse_list_result(final_result)
        final_count = len(final_files)
        successful_writes = sum(1 for r in write_results if r["success"])

        # Check if any list returned fewer files than expected at that point
        # Sort list results by count to see if there were any "low" counts
        list_counts = sorted([r["count"] for r in list_results])
        min_count = min(list_counts) if list_counts else 0
        max_count = max(list_counts) if list_counts else 0

        # Total expected = baseline + successful concurrent writes
        expected_total = baseline_count + successful_writes

        print("\nResults:")
        print(f"  Baseline files: {baseline_count}")
        print(f"  Concurrent writes: {successful_writes}/{num_files - baseline_count}")
        print(f"  Expected total: {expected_total}")
        print(f"  Final file count: {final_count}")
        print(f"  List count range: {min_count} - {max_count}")
        print(f"  List operations: {len(list_results)}")

        # The race condition would manifest as:
        # - A list operation sees fewer files than were written at that moment
        # This is hard to detect precisely, but if final_count != expected_total,
        # or if there's high variance in list counts, something might be wrong

        assert final_count == expected_total, (
            f"Final count mismatch! Expected {expected_total} files (baseline={baseline_count}, "
            f"concurrent={successful_writes}), got {final_count}. "
            f"This could indicate a race condition."
        )

    def test_write_then_immediate_list_burst(self, nexus_server):
        """Write a file then immediately burst many list requests.

        If there's cache staleness, some list requests might miss the file.
        """
        base_url = nexus_server["base_url"]
        unique_id = str(uuid.uuid4())[:8]
        test_dir = f"/zone:system/burst_{unique_id}"
        test_file = f"{test_dir}/target.txt"

        # Write the file
        with httpx.Client(base_url=base_url, timeout=90.0, trust_env=False) as client:
            write_result = make_rpc_request(
                client,
                "write",
                {"path": test_file, "content": encode_bytes("target content")},
            )
            assert "error" not in write_result, f"Write failed: {write_result}"

        # Immediately burst many list requests
        num_lists = 10  # Reduced from 20

        def do_list() -> bool:
            with httpx.Client(base_url=base_url, timeout=90.0, trust_env=False) as client:
                result = make_rpc_request(
                    client,
                    "list",
                    {"path": test_dir, "recursive": False},
                )
                if "error" in result:
                    return False
                files = parse_list_result(result)
                names = get_file_names(files)
                return any("target.txt" in name for name in names)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(do_list) for _ in range(num_lists)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        found_count = sum(results)
        missed_count = num_lists - found_count

        print(f"\nBurst list results: {found_count}/{num_lists} found the file")

        if missed_count > 0:
            pytest.fail(
                f"RACE CONDITION DETECTED: {missed_count}/{num_lists} list requests "
                f"missed the newly created file! This confirms Issue #1147."
            )

    def test_provision_with_concurrent_lists(self, nexus_server):
        """Simulate the exact issue: provision user while listing agent directories."""
        base_url = nexus_server["base_url"]
        unique_id = str(uuid.uuid4())[:8]
        test_user = f"raceuser_{unique_id}"

        agent_dirs = [
            f"/zone:system/user:{test_user}/agent/ImpersonatedUser",
            f"/zone:system/user:{test_user}/agent/UntrustedAgent",
            f"/zone:system/user:{test_user}/agent/SkillBuilder",
        ]

        race_detected = []

        def provision_user() -> bool:
            """Provision the user."""
            with httpx.Client(base_url=base_url, timeout=180.0, trust_env=False) as client:
                result = make_rpc_request(
                    client,
                    "provision_user",
                    {
                        "user_id": test_user,
                        "email": f"{test_user}@test.com",
                        "display_name": "Race Test User",
                        "create_agents": True,
                        "import_skills": False,
                    },
                )
                return "error" not in result

        def check_agent_config(agent_dir: str) -> dict:
            """Check if config.yaml is visible in list vs read."""
            with httpx.Client(base_url=base_url, timeout=90.0, trust_env=False) as client:
                # Try to list
                list_result = make_rpc_request(
                    client,
                    "list",
                    {"path": agent_dir, "recursive": False},
                )

                # Try to read directly
                config_path = f"{agent_dir}/config.yaml"
                read_result = make_rpc_request(
                    client,
                    "read",
                    {"path": config_path},
                )

                list_has_config = False
                if "error" not in list_result:
                    files = parse_list_result(list_result)
                    names = get_file_names(files)
                    list_has_config = any("config.yaml" in name for name in names)

                read_succeeded = "error" not in read_result

                return {
                    "agent_dir": agent_dir,
                    "list_has_config": list_has_config,
                    "read_succeeded": read_succeeded,
                    "race_detected": read_succeeded and not list_has_config,
                }

        # Start provision in background
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            provision_future = executor.submit(provision_user)

            # While provisioning, repeatedly check agent directories
            check_futures = []
            for _ in range(5):  # Reduced from 10 rounds to avoid overwhelming test server
                for agent_dir in agent_dirs:
                    check_futures.append(executor.submit(check_agent_config, agent_dir))

            # Wait for provision to complete
            provision_success = provision_future.result()

            # Collect check results
            for future in concurrent.futures.as_completed(check_futures):
                result = future.result()
                if result["race_detected"]:
                    race_detected.append(result)

        if race_detected:
            pytest.fail(
                f"RACE CONDITION DETECTED during provision!\n"
                f"  {len(race_detected)} instances where READ succeeded but LIST missed config.yaml:\n"
                + "\n".join(f"    - {r['agent_dir']}" for r in race_detected[:5])
            )

        # Even if no race during provision, check final state
        if provision_success:
            for agent_dir in agent_dirs:
                result = check_agent_config(agent_dir)
                if result["race_detected"]:
                    pytest.fail(
                        f"Race condition in final state for {agent_dir}: "
                        f"READ works but LIST doesn't show config.yaml"
                    )
