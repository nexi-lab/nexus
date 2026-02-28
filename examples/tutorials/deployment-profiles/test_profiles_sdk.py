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


def test_profile(profile: str) -> dict[str, str]:
    """Run all operations for a single profile. Returns {op: "OK"|"FAIL"}."""
    data_dir = f"{BASE_DIR}/{profile}"
    os.makedirs(data_dir, exist_ok=True)

    for k in ["NEXUS_URL", "NEXUS_DATABASE_URL", "NEXUS_MODE"]:
        os.environ.pop(k, None)
    os.environ["NEXUS_PROFILE"] = profile

    row: dict[str, str] = {}

    try:
        nx = nexus.connect(config={"mode": "standalone", "data_dir": data_dir})
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
        path_v = info.get("path", "?") if isinstance(info, dict) else getattr(info, "path", "?")
        size_v = info.get("size", "?") if isinstance(info, dict) else getattr(info, "size", "?")
        print(f"    OK   /project/src/main.py => path={path_v}, size={size_v}")
        row["stat"] = "OK"
    except Exception as e:
        print(f"    FAIL: {e}")
        row["stat"] = "FAIL"

    # --- LIST ---
    print("  LIST")
    try:
        entries = nx.sys_readdir("/project/src")
        names = sorted(entries) if isinstance(entries, list) else entries
        print(f"    OK   /project/src => {names}")
        row["list"] = "OK"
    except Exception as e:
        print(f"    FAIL: {e}")
        row["list"] = "FAIL"

    # --- GLOB ---
    print("  GLOB")
    try:
        matches = nx.glob("**/*.py")
        paths = sorted(matches) if isinstance(matches, list) else matches
        print(f"    OK   **/*.py => {paths}")
        row["glob"] = "OK"
    except Exception as e:
        print(f"    FAIL: {e}")
        row["glob"] = "FAIL"

    # --- GREP ---
    print("  GREP")
    try:
        matches = nx.grep("TODO")
        count = len(matches) if isinstance(matches, list) else "?"
        print(f"    OK   'TODO' => {count} match(es)")
        row["grep"] = "OK"
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
