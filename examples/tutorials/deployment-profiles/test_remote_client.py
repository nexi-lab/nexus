#!/usr/bin/env python3
"""
Test remote client mode: gRPC proxy from client SDK to a running server.

Starts a minimal server with gRPC enabled, then connects a remote client
and verifies write/read/list/stat/glob/grep/delete operations work through
the proxy.

Prerequisites:
    pip install -e .

Usage:
    python3 examples/tutorials/deployment-profiles/test_remote_client.py
"""

import os
import shutil
import subprocess
import sys
import time

BASE_DIR = "/tmp/nexus-tutorial-remote"
HTTP_PORT = 3090
GRPC_PORT = 3091


def wait_for_server(port: int, timeout: int = 20) -> bool:
    """Wait until the server health endpoint responds."""
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


async def main() -> int:
    shutil.rmtree(BASE_DIR, ignore_errors=True)
    os.makedirs(f"{BASE_DIR}/server", exist_ok=True)

    # --- Start server ---
    print("Starting minimal server with gRPC...")
    env = os.environ.copy()
    env["NEXUS_GRPC_PORT"] = str(GRPC_PORT)
    env["NEXUS_DATA_DIR"] = f"{BASE_DIR}/server"
    for k in ["NEXUS_URL", "NEXUS_DATABASE_URL", "NEXUS_PROFILE"]:
        env.pop(k, None)

    server = subprocess.Popen(
        [
            "nexus",
            "serve",
            "--profile",
            "minimal",
            "--port",
            str(HTTP_PORT),
            "--data-dir",
            f"{BASE_DIR}/server",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if not wait_for_server(HTTP_PORT):
        print("Server failed to start.")
        server.kill()
        return 1

    # Check gRPC port
    time.sleep(1)
    r = subprocess.run(
        ["lsof", "-ti", f":{GRPC_PORT}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    if not r.stdout.strip():
        print(f"gRPC port {GRPC_PORT} not listening.")
        server.terminate()
        return 1

    print(f"  Server ready (HTTP :{HTTP_PORT}, gRPC :{GRPC_PORT})")

    # --- Connect remote client ---
    print("\nConnecting remote client via gRPC...")
    os.environ["NEXUS_GRPC_PORT"] = str(GRPC_PORT)

    import nexus

    nx = nexus.connect(
        config={
            "profile": "remote",
            "url": f"http://localhost:{HTTP_PORT}",
        }
    )
    print(f"  Connected: {type(nx).__name__}")

    ok = True

    # --- WRITE ---
    print("\nWRITE")
    files = {
        "/hello.txt": b"Hello from remote client!",
        "/project/main.py": b'# TODO: implement argument parsing\nprint("Hello, Nexus!")\n',
        "/project/utils.py": b'# TODO: add logging\ndef greet(name):\n    return f"Hello, {name}!"\n',
        "/project/config.json": b'{"version": "1.0"}\n',
    }
    for path, content in files.items():
        try:
            nx.sys_write(path, content)
            print(f"  OK   {path} ({len(content)} bytes)")
        except Exception as e:
            print(f"  FAIL {path}: {e}")
            ok = False

    # --- READ ---
    print("READ")
    for path, expected in files.items():
        try:
            got = nx.sys_read(path)
            if got == expected:
                print(f"  OK   {path} — matches ({len(got)} bytes)")
            else:
                print(f"  DIFF {path} — expected {len(expected)}B, got {len(got)}B")
                ok = False
        except Exception as e:
            print(f"  FAIL {path}: {e}")
            ok = False

    # --- LIST ---
    print("LIST")
    try:
        entries = nx.sys_readdir("/")
        names = sorted(entries) if isinstance(entries, list) else entries
        print(f"  raw  / => {names}")
        # Verify expected paths are present
        expected_paths = {
            "/hello.txt",
            "/project/main.py",
            "/project/utils.py",
            "/project/config.json",
        }
        found = {str(e) for e in (names if isinstance(names, list) else [])}
        missing = expected_paths - found
        if missing:
            print(f"  FAIL missing entries: {missing}")
            ok = False
        else:
            print(f"  OK   all {len(expected_paths)} expected entries found")
    except Exception as e:
        print(f"  FAIL: {e}")
        ok = False

    # --- STAT ---
    print("STAT")
    try:
        info = nx.sys_stat("/hello.txt")
        print(f"  raw  /hello.txt => {info}")
        # Verify size matches what we wrote (25 bytes)
        size_v = info.get("size", None) if isinstance(info, dict) else getattr(info, "size", None)
        if size_v is not None and int(size_v) == len(files["/hello.txt"]):
            print(f"  OK   /hello.txt => size={size_v} (correct)")
        elif size_v is not None:
            print(f"  FAIL /hello.txt => size={size_v}, expected {len(files['/hello.txt'])}")
            ok = False
        else:
            print("  WARN /hello.txt => size not in stat result")
    except Exception as e:
        print(f"  FAIL: {e}")
        ok = False

    # --- GLOB ---
    print("GLOB")
    try:
        result = nx.glob("**/*.py")
        print(f"  raw  **/*.py => {result}")
        # Handle both list and dict return types
        if isinstance(result, dict) and "matches" in result:
            paths = sorted(result["matches"])
        elif isinstance(result, list):
            paths = sorted(result)
        else:
            paths = []
        expected_py = {"/project/main.py", "/project/utils.py"}
        found_py = set(paths)
        if expected_py <= found_py:
            print(f"  OK   found {len(paths)} .py file(s): {paths}")
        else:
            print(f"  FAIL expected {expected_py}, got {found_py}")
            ok = False
    except Exception as e:
        print(f"  FAIL: {e}")
        ok = False

    # --- GREP ---
    print("GREP")
    try:
        result = nx.grep("TODO")
        print(f"  raw  'TODO' => {result}")
        # Handle list, dict with 'matches', or dict with 'results'
        if isinstance(result, dict) and "results" in result:
            matches = result["results"]
        elif isinstance(result, dict) and "matches" in result:
            matches = result["matches"]
        elif isinstance(result, list):
            matches = result
        else:
            matches = result
        # We expect at least 2 matches (main.py and utils.py both have TODO)
        count = len(matches) if isinstance(matches, list) else "?"
        if isinstance(matches, list) and len(matches) >= 2:
            print(f"  OK   {count} match(es)")
        else:
            print(f"  FAIL expected >= 2 matches, got {count}")
            ok = False
    except Exception as e:
        print(f"  FAIL: {e}")
        ok = False

    # --- DELETE ---
    print("DELETE")
    try:
        nx.sys_unlink("/hello.txt")
        exists = await nx.access("/hello.txt")
        if not exists:
            print("  OK   /hello.txt deleted, verified gone")
        else:
            print("  FAIL /hello.txt still exists after delete")
            ok = False
    except Exception as e:
        print(f"  FAIL: {e}")
        ok = False

    # Verify remaining files still readable
    try:
        got = nx.sys_read("/project/main.py")
        assert got == files["/project/main.py"]
        print("  OK   /project/main.py still readable after sibling delete")
    except Exception as e:
        print(f"  FAIL: {e}")
        ok = False

    # Cleanup
    del nx
    server.terminate()
    try:
        server.wait(timeout=3)
    except subprocess.TimeoutExpired:
        server.kill()
    shutil.rmtree(BASE_DIR, ignore_errors=True)

    if ok:
        print("\nAll remote client operations passed.")
        return 0
    else:
        print("\nSome operations failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
