#!/usr/bin/env python3
"""
Test CLI commands (write/cat/info/ls/glob/grep) via gRPC across all profiles.

Starts a server per profile with gRPC enabled, then runs CLI commands as
a remote client (NEXUS_URL + NEXUS_GRPC_PORT).  This exercises the real
client-server path that production deployments use.

Prerequisites:
    pip install -e .

Usage:
    python3 examples/tutorials/deployment-profiles/test_profiles_cli.py
"""

import os
import shutil
import subprocess
import sys
import time

PROFILES = ["minimal", "embedded", "lite", "full", "cloud", "remote", "auto"]
OPS = ["write", "read", "stat", "list", "glob", "grep"]
BASE_PORT = 3100
BASE_DIR = "/tmp/nexus-tutorial-cli"

# Files to write — include TODO comments so grep has matches
FILES = {
    "/project/src/main.py": '# TODO: implement argument parsing\nprint("Hello, Nexus!")\n',
    "/project/src/utils.py": '# TODO: add logging\ndef greet(name):\n    return f"Hello, {name}!"\n',
    "/project/tests/test_utils.py": "import pytest\n\ndef test_greet():\n    assert True\n",
    "/project/config.json": '{"name": "nexus-demo", "version": "1.0.0"}\n',
}

# Build a clean env (remove vars that would interfere)
_CLEAN_ENV = {
    k: v
    for k, v in os.environ.items()
    if k not in ("NEXUS_URL", "NEXUS_DATABASE_URL", "NEXUS_MODE", "NEXUS_PROFILE")
}


def _run_cli(args: list[str], url: str, grpc_port: int) -> tuple[int, str]:
    """Run a nexus CLI command as a remote client."""
    env = _CLEAN_ENV.copy()
    env["NEXUS_URL"] = url
    env["NEXUS_GRPC_PORT"] = str(grpc_port)
    r = subprocess.run(
        ["nexus"] + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return r.returncode, r.stdout + r.stderr


def _wait_for_server(port: int, timeout: int = 20) -> bool:
    for _ in range(timeout):
        time.sleep(1)
        try:
            r = subprocess.run(
                ["curl", "-s", f"http://localhost:{port}/health"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if "healthy" in r.stdout:
                return True
        except Exception:
            pass
    return False


def test_profile(profile: str, http_port: int, grpc_port: int) -> dict[str, str]:
    """Start a server, run all CLI ops against it, return results."""
    data_dir = f"{BASE_DIR}/{profile}"
    os.makedirs(data_dir, exist_ok=True)

    url = f"http://localhost:{http_port}"

    # Start server with gRPC
    env = _CLEAN_ENV.copy()
    env["NEXUS_GRPC_PORT"] = str(grpc_port)
    env["NEXUS_DATA_DIR"] = data_dir
    proc = subprocess.Popen(
        ["nexus", "serve", "--profile", profile, "--port", str(http_port), "--data-dir", data_dir],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if not _wait_for_server(http_port):
        print("  SERVER FAILED TO START")
        proc.kill()
        return dict.fromkeys(OPS, "FAIL")

    row: dict[str, str] = {}

    # --- WRITE ---
    print("  WRITE")
    write_ok = True
    for path, content in FILES.items():
        rc, out = _run_cli(["write", path, content], url, grpc_port)
        if rc == 0 and "Wrote" in out:
            print(f"    OK   {path}")
        else:
            print(f"    FAIL {path}: {out.strip()[:100]}")
            write_ok = False
    row["write"] = "OK" if write_ok else "FAIL"

    # --- READ (cat) ---
    print("  READ")
    read_ok = True
    for path, content in FILES.items():
        rc, out = _run_cli(["cat", path], url, grpc_port)
        key_substr = content.split("\n")[0][:30]
        if rc == 0 and key_substr in out:
            print(f"    OK   {path} — content verified")
        else:
            print(f"    FAIL {path}: {out.strip()[:100]}")
            read_ok = False
    row["read"] = "OK" if read_ok else "FAIL"

    # --- STAT (info) ---
    print("  STAT")
    rc, out = _run_cli(["info", "/project/src/main.py"], url, grpc_port)
    if rc == 0 and "/project/src/main.py" in out and "bytes" in out.lower():
        print("    OK   /project/src/main.py — path and size present")
        row["stat"] = "OK"
    else:
        print(f"    FAIL: {out.strip()[:150]}")
        row["stat"] = "FAIL"

    # --- LIST (ls) ---
    print("  LIST")
    rc, out = _run_cli(["ls", "/project/src"], url, grpc_port)
    if rc == 0 and "main.py" in out and "utils.py" in out:
        print("    OK   /project/src — both files listed")
        row["list"] = "OK"
    else:
        print(f"    FAIL: {out.strip()[:150]}")
        row["list"] = "FAIL"

    # --- GLOB ---
    print("  GLOB")
    rc, out = _run_cli(["glob", "**/*.py"], url, grpc_port)
    if rc == 0 and "main.py" in out and "utils.py" in out and "test_utils.py" in out:
        print("    OK   **/*.py — all 3 .py files found")
        row["glob"] = "OK"
    else:
        print(f"    FAIL: {out.strip()[:150]}")
        row["glob"] = "FAIL"

    # --- GREP ---
    print("  GREP")
    rc, out = _run_cli(["grep", "TODO"], url, grpc_port)
    todo_lines = [line for line in out.split("\n") if "TODO" in line and "Found" not in line]
    if rc == 0 and len(todo_lines) >= 2:
        print(f"    OK   'TODO' — {len(todo_lines)} match(es)")
        row["grep"] = "OK"
    else:
        print(f"    FAIL: {out.strip()[:150]}")
        row["grep"] = "FAIL"

    # Stop server
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(0.5)

    return row


def main() -> int:
    shutil.rmtree(BASE_DIR, ignore_errors=True)
    results: dict[str, dict[str, str]] = {}

    for i, profile in enumerate(PROFILES):
        http_port = BASE_PORT + i
        grpc_port = BASE_PORT + 100 + i
        print(f"\n{'=' * 50}")
        print(f"  PROFILE: {profile} (HTTP :{http_port}, gRPC :{grpc_port})")
        print(f"{'=' * 50}")
        results[profile] = test_profile(profile, http_port, grpc_port)

    # Summary
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
    print("\nAll CLI operations passed across all profiles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
