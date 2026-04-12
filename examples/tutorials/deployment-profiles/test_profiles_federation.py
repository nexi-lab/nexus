#!/usr/bin/env python3
"""
Test nexus serve with profile=cloud (federation) across all deployment profiles.

The cloud profile enables Raft consensus for multi-zone metadata replication.

Prerequisites:
    pip install -e .

    # Build Rust extension with full features (requires protobuf 3.x)
    PROTOC=/opt/homebrew/opt/protobuf@21/bin/protoc \
      maturin develop -m rust/raft/Cargo.toml --features full

Usage:
    python3 examples/tutorials/deployment-profiles/test_profiles_federation.py
"""

import os
import shutil
import subprocess
import sys
import time

PROFILES = ["minimal", "embedded", "lite", "full", "cloud", "remote", "auto"]
BASE_PORT = 3080
BASE_DIR = "/tmp/nexus-tutorial-federation"


def main() -> int:
    shutil.rmtree(BASE_DIR, ignore_errors=True)

    # Quick check: is ZoneManager available?
    try:
        from _nexus_raft import ZoneManager  # noqa: F401
    except ImportError:
        print("ZoneManager not available.")
        print("Build the Rust extension with full features first:")
        print()
        print("  PROTOC=/opt/homebrew/opt/protobuf@21/bin/protoc \\")
        print("    maturin develop -m rust/raft/Cargo.toml --features full")
        return 1

    results: dict[str, str] = {}

    for i, profile in enumerate(PROFILES):
        port = BASE_PORT + i
        data_dir = f"{BASE_DIR}/{profile}"
        os.makedirs(data_dir, exist_ok=True)

        env = os.environ.copy()
        env["NEXUS_PROFILE"] = "cloud"
        env["NEXUS_DATA_DIR"] = data_dir
        for k in ["NEXUS_URL", "NEXUS_DATABASE_URL"]:
            env.pop(k, None)
        # Note: NEXUS_PROFILE is set to "cloud" above, don't clear it

        proc = subprocess.Popen(
            [
                "nexus",
                "serve",
                "--profile",
                profile,
                "--port",
                str(port),
                "--data-dir",
                data_dir,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        started = False
        for _attempt in range(20):
            time.sleep(1)
            if proc.poll() is not None:
                break
            try:
                r = subprocess.run(
                    ["curl", "-s", f"http://localhost:{port}/health"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if "healthy" in r.stdout:
                    started = True
                    break
            except Exception:
                pass

        if started:
            print(f"  {profile} + cloud: OK (port {port}, {_attempt + 1}s)")
            results[profile] = "OK"
        else:
            print(f"  {profile} + cloud: FAILED")
            results[profile] = "FAILED"

        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(0.5)

    # Summary
    print(f"\n{'Profile':<12} {'cloud (federation)'}")
    print("-" * 30)
    for profile in PROFILES:
        print(f"{profile:<12} {results[profile]}")

    shutil.rmtree(BASE_DIR, ignore_errors=True)

    failed = sum(1 for v in results.values() if v != "OK")
    if failed:
        print(f"\n{failed} profile(s) failed.")
        return 1
    print("\nAll profiles work with cloud (federation) profile.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
