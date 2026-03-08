#!/usr/bin/env python3
"""
Test nexus serve startup across all deployment profiles.

Starts each profile on a unique port, verifies the /health endpoint
responds, and reports results.

Prerequisites:
    pip install -e .

Usage:
    python3 examples/tutorials/deployment-profiles/test_profiles_serve.py
"""

import json
import os
import shutil
import subprocess
import sys
import time

PROFILES = ["minimal", "embedded", "lite", "full", "cloud", "remote", "auto"]
BASE_PORT = 3070
BASE_DIR = "/tmp/nexus-tutorial-serve"


def main() -> int:
    # Clean previous run
    shutil.rmtree(BASE_DIR, ignore_errors=True)

    results: dict[str, dict] = {}
    pids: list[int] = []

    for i, profile in enumerate(PROFILES):
        port = BASE_PORT + i
        data_dir = f"{BASE_DIR}/{profile}"
        os.makedirs(data_dir, exist_ok=True)

        env = os.environ.copy()
        env["NEXUS_DATA_DIR"] = data_dir
        for k in ["NEXUS_URL", "NEXUS_DATABASE_URL", "NEXUS_PROFILE"]:
            env.pop(k, None)

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
        pids.append(proc.pid)

        # Wait for server to start
        started = False
        for _attempt in range(20):
            time.sleep(1)
            if proc.poll() is not None:
                print(f"  {profile}: FAILED (process exited)")
                break
            try:
                r = subprocess.run(
                    ["curl", "-s", f"http://localhost:{port}/health"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if "healthy" in r.stdout:
                    health = json.loads(r.stdout)
                    started = True
                    break
            except Exception:
                pass

        if started:
            print(f"  {profile}: OK (port {port}, {_attempt + 1}s)")
            results[profile] = {"status": "OK", "port": port, "health": health}
        else:
            print(f"  {profile}: FAILED (timeout)")
            results[profile] = {"status": "FAILED"}

        # Stop server
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(0.5)

    # Summary
    print(f"\n{'Profile':<12} {'Status':<8} {'Port'}")
    print("-" * 35)
    for profile in PROFILES:
        r = results[profile]
        port_str = str(r.get("port", "-"))
        print(f"{profile:<12} {r['status']:<8} {port_str}")

    shutil.rmtree(BASE_DIR, ignore_errors=True)

    failed = sum(1 for r in results.values() if r["status"] != "OK")
    if failed:
        print(f"\n{failed} profile(s) failed.")
        return 1
    print("\nAll profiles started successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
