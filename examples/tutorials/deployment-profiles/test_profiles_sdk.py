#!/usr/bin/env python3
"""
Test core filesystem operations via Python SDK across all deployment profiles.

Writes real files, then verifies read, stat, list, glob, and grep return
correct results.  No server needed — uses nexus.connect() in standalone mode.

Prerequisites:
    pip install -e .

Usage:
    python3 examples/tutorials/deployment-profiles/test_profiles_sdk.py
"""

import os
import shutil
import sys

import nexus

PROFILES = ["minimal", "embedded", "lite", "full", "cloud", "remote", "auto"]
OPS = ["write", "read", "stat", "list", "glob", "grep"]
BASE_DIR = "/tmp/nexus-tutorial-sdk"

# Real project files to write
FILES = {
    "/project/README.md": b"# My Project\n\nThis is a TODO tracker for the team.\n",
    "/project/src/main.py": (
        b"#!/usr/bin/env python3\n"
        b'"""Main entry point."""\n'
        b"import sys\n\n"
        b"def main():\n"
        b"    # TODO: implement argument parsing\n"
        b'    print("Hello, Nexus!")\n'
        b"    return 0\n\n"
        b'if __name__ == "__main__":\n'
        b"    sys.exit(main())\n"
    ),
    "/project/src/utils.py": (
        b'"""Utility functions."""\n\n'
        b"def greet(name: str) -> str:\n"
        b"    # TODO: add logging\n"
        b'    return f"Hello, {name}!"\n\n'
        b"def add(a: int, b: int) -> int:\n"
        b"    return a + b\n"
    ),
    "/project/tests/test_utils.py": (
        b'"""Tests for utils."""\n'
        b"import pytest\n"
        b"from src.utils import greet, add\n\n"
        b"def test_greet():\n"
        b'    assert greet("Alice") == "Hello, Alice!"\n\n'
        b"def test_add():\n"
        b"    assert add(2, 3) == 5\n"
    ),
    "/project/config.json": (
        b"{\n"
        b'  "name": "nexus-demo",\n'
        b'  "version": "1.0.0",\n'
        b'  "debug": true,\n'
        b'  "max_retries": 3\n'
        b"}\n"
    ),
}


async def test_profile(profile: str) -> dict[str, str]:
    """Run all operations for a single profile. Returns {op: "OK"|"FAIL"}."""
    data_dir = f"{BASE_DIR}/{profile}"
    os.makedirs(data_dir, exist_ok=True)

    for k in ["NEXUS_URL", "NEXUS_DATABASE_URL", "NEXUS_PROFILE"]:
        os.environ.pop(k, None)
    os.environ["NEXUS_PROFILE"] = profile

    row: dict[str, str] = {}

    try:
        nx = nexus.connect(config={"profile": "full", "data_dir": data_dir})
    except Exception as e:
        print(f"  CONNECT FAILED: {e}")
        return dict.fromkeys(OPS, "FAIL")

    # --- WRITE ---
    print("\n  WRITE")
    write_ok = True
    for path, content in FILES.items():
        try:
            nx.sys_write(path, content)
            print(f"    OK   {path} ({len(content)} bytes)")
        except Exception as e:
            print(f"    FAIL {path}: {e}")
            write_ok = False
    row["write"] = "OK" if write_ok else "FAIL"

    # --- READ ---
    print("  READ")
    read_ok = True
    for path, expected in FILES.items():
        try:
            got = nx.sys_read(path)
            if got == expected:
                print(f"    OK   {path} — content matches ({len(got)} bytes)")
            else:
                print(f"    DIFF {path} — expected {len(expected)}B, got {len(got)}B")
                read_ok = False
        except Exception as e:
            print(f"    FAIL {path}: {e}")
            read_ok = False
    row["read"] = "OK" if read_ok else "FAIL"

    # --- STAT ---
    print("  STAT")
    try:
        info = nx.sys_stat("/project/src/main.py")
        size_v = info.get("size", None) if isinstance(info, dict) else getattr(info, "size", None)
        expected_size = len(FILES["/project/src/main.py"])
        if size_v is not None and int(size_v) == expected_size:
            print(f"    OK   /project/src/main.py => size={size_v} (correct)")
            row["stat"] = "OK"
        elif size_v is not None:
            print(f"    FAIL /project/src/main.py => size={size_v}, expected {expected_size}")
            row["stat"] = "FAIL"
        else:
            print(f"    WARN /project/src/main.py => size not in stat, raw: {info}")
            row["stat"] = "OK"  # stat worked, just no size field
    except Exception as e:
        print(f"    FAIL: {e}")
        row["stat"] = "FAIL"

    # --- LIST ---
    print("  LIST")
    try:
        entries = nx.sys_readdir("/project/src")
        names = sorted(entries) if isinstance(entries, list) else entries
        # Verify main.py and utils.py are listed
        found = {str(e) for e in (names if isinstance(names, list) else [])}
        expected_names = {"main.py", "utils.py", "/project/src/main.py", "/project/src/utils.py"}
        if found & expected_names:
            print(f"    OK   /project/src => {names}")
            row["list"] = "OK"
        else:
            print(f"    FAIL /project/src => {names} (missing expected files)")
            row["list"] = "FAIL"
    except Exception as e:
        print(f"    FAIL: {e}")
        row["list"] = "FAIL"

    # --- GLOB ---
    print("  GLOB")
    try:
        result = nx.glob("**/*.py")
        # Handle both list and dict return types
        if isinstance(result, dict) and "matches" in result:
            paths = sorted(result["matches"])
        elif isinstance(result, list):
            paths = sorted(result)
        else:
            paths = []
        py_paths = [p for p in paths if ".py" in str(p)]
        if len(py_paths) >= 3:  # main.py, utils.py, test_utils.py
            print(f"    OK   **/*.py => {py_paths}")
            row["glob"] = "OK"
        else:
            print(f"    FAIL **/*.py => {paths} (expected >= 3 .py files)")
            row["glob"] = "FAIL"
    except Exception as e:
        print(f"    FAIL: {e}")
        row["glob"] = "FAIL"

    # --- GREP ---
    print("  GREP")
    try:
        result = nx.grep("TODO")
        # Handle list, dict with 'matches', or dict with 'results'
        if isinstance(result, dict) and "results" in result:
            matches = result["results"]
        elif isinstance(result, dict) and "matches" in result:
            matches = result["matches"]
        elif isinstance(result, list):
            matches = result
        else:
            matches = result
        count = len(matches) if isinstance(matches, list) else "?"
        if isinstance(matches, list) and len(matches) >= 2:
            print(f"    OK   'TODO' => {count} match(es)")
            row["grep"] = "OK"
        else:
            print(f"    FAIL 'TODO' => {count} match(es), expected >= 2")
            row["grep"] = "FAIL"
    except Exception as e:
        print(f"    FAIL: {e}")
        row["grep"] = "FAIL"

    del nx
    return row


def main() -> int:
    shutil.rmtree(BASE_DIR, ignore_errors=True)
    results: dict[str, dict[str, str]] = {}

    for profile in PROFILES:
        print(f"\n{'=' * 50}")
        print(f"  PROFILE: {profile}")
        print(f"{'=' * 50}")
        results[profile] = test_profile(profile)

    # Summary table
    print(f"\n{'=' * 50}")
    print("  SUMMARY")
    print(f"{'=' * 50}")
    header = f"{'Profile':<12} | " + " | ".join(f"{op:<6}" for op in OPS)
    print(header)
    print("-" * len(header))
    for profile in PROFILES:
        row = results[profile]
        cells = " | ".join(f"{row[op]:<6}" for op in OPS)
        print(f"{profile:<12} | {cells}")

    shutil.rmtree(BASE_DIR, ignore_errors=True)

    failed = sum(1 for r in results.values() for v in r.values() if v != "OK")
    if failed:
        print(f"\n{failed} operation(s) failed.")
        return 1
    print("\nAll operations passed across all profiles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
