#!/usr/bin/env python3
"""Standalone test for Python-Rust IPC (no nexus dependencies)."""

import base64
import json
import socket
import subprocess
import time
from pathlib import Path

RUST_BINARY = Path(__file__).parent / "target/debug/nexus-fuse"
NEXUS_URL = "http://localhost:2026"
API_KEY = "sk-test-key-123"


def test_ipc():
    """Test basic IPC communication with Rust daemon."""
    print("🧪 Testing Python → Rust IPC (standalone)\n")

    # 1. Start Rust daemon
    print("1. Starting Rust daemon...")
    proc = subprocess.Popen(
        [str(RUST_BINARY), "daemon", "--url", NEXUS_URL, "--api-key", API_KEY],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Read socket path from stdout
    socket_path = proc.stdout.readline().strip()
    print(f"   ✓ Daemon started: {socket_path}\n")

    # Wait for socket to be ready
    for _ in range(50):
        if Path(socket_path).exists():
            break
        time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError(f"Socket not created: {socket_path}")

    try:
        # 2. Connect to socket
        print("2. Connecting to socket...")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)
        print("   ✓ Connected\n")

        # 3. Test write operation
        print("3. Testing write...")
        test_content = b"Hello from Python!"
        encoded = base64.b64encode(test_content).decode()
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "write",
            "params": {
                "path": "/ipc-test.txt",
                "content": {"__type__": "bytes", "data": encoded},
            },
        }
        print(f"   DEBUG: Sending: {json.dumps(request)[:100]}...")
        sock.sendall((json.dumps(request) + "\n").encode())
        response = sock.recv(4096).decode()
        print(f"   DEBUG: Response: {response!r}")
        result = json.loads(response)
        if "error" in result:
            print(f"   ✗ Write failed: {result['error']}")
            return False
        print("   ✓ Write successful\n")

        # 4. Test read operation
        print("4. Testing read...")
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "read",
            "params": {"path": "/ipc-test.txt"},
        }
        sock.sendall((json.dumps(request) + "\n").encode())
        response = sock.recv(4096).decode()
        result = json.loads(response)
        if "error" in result:
            print(f"   ✗ Read failed: {result['error']}")
            return False
        content = base64.b64decode(result["result"]["data"])
        assert content == test_content, f"Content mismatch: {content!r} != {test_content!r}"
        print(f"   ✓ Read successful: {content.decode()!r}\n")

        # 5. Test list operation
        print("5. Testing list...")
        request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "list",
            "params": {"path": "/"},
        }
        sock.sendall((json.dumps(request) + "\n").encode())
        response = sock.recv(4096).decode()
        result = json.loads(response)
        if "error" in result:
            print(f"   ✗ List failed: {result['error']}")
            return False
        files = result["result"]["files"]
        print(f"   ✓ List successful: {len(files)} files\n")

        # 6. Test delete operation
        print("6. Testing delete...")
        request = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "delete",
            "params": {"path": "/ipc-test.txt"},
        }
        sock.sendall((json.dumps(request) + "\n").encode())
        response = sock.recv(4096).decode()
        result = json.loads(response)
        if "error" in result:
            print(f"   ✗ Delete failed: {result['error']}")
            return False
        print("   ✓ Delete successful\n")

        # 7. Test error handling (404)
        print("7. Testing error handling (404)...")
        request = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "read",
            "params": {"path": "/nonexistent.txt"},
        }
        sock.sendall((json.dumps(request) + "\n").encode())
        response = sock.recv(4096).decode()
        result = json.loads(response)
        if "error" not in result:
            print("   ✗ Should have returned error for nonexistent file")
            return False
        error = result["error"]
        errno = error.get("data", {}).get("errno", 0)
        assert errno == 2, f"Expected ENOENT (2), got {errno}"
        print(f"   ✓ Error handling correct: errno={errno}, msg={error['message']!r}\n")

        sock.close()
        print("🎉 All IPC tests passed!\n")
        return True

    finally:
        # Cleanup
        proc.terminate()
        proc.wait(timeout=5)
        if Path(socket_path).exists():
            Path(socket_path).unlink()


if __name__ == "__main__":
    success = test_ipc()
    exit(0 if success else 1)
