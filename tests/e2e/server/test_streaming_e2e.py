"""E2E tests for streaming write/read with permissions enabled (#1625).

Validates that the streaming code paths (write_stream, stream_content,
stream_range) work correctly end-to-end through a real nexus server with
permissions enabled. Specifically tests:

1. Large file upload via /api/v2/files/write -> readback via /api/v2/files/stream
2. Range requests (partial downloads) via /api/v2/files/stream with Range header
3. Hash integrity: content round-trips without corruption
4. Permission enforcement on streaming endpoints
5. Memory behavior: server doesn't OOM on moderately-sized files
"""

import base64
import hashlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import httpx
import pytest

# === Config ===

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30

ADMIN_API_KEY = "sk-stream-admin-key"
ALICE_API_KEY = "sk-stream-alice-key"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=10, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{base_url}/health")
                if resp.status_code == 200:
                    return
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
                pass
            time.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s at {base_url}")


def _build_startup_script(port: int, data_dir: str) -> str:
    return textwrap.dedent(f"""\
        import os, sys, logging
        logging.basicConfig(level=logging.INFO)
        sys.path.insert(0, os.getenv("PYTHONPATH", ""))

        from nexus.bricks.auth.providers.static_key import StaticAPIKeyAuth
        from nexus.daemon.main import main as cli_main

        auth_config = {{
            "api_keys": {{
                "{ADMIN_API_KEY}": {{
                    "subject_type": "user",
                    "subject_id": "admin",
                    "zone_id": "test",
                    "is_admin": True,
                }},
                "{ALICE_API_KEY}": {{
                    "subject_type": "user",
                    "subject_id": "alice",
                    "zone_id": "test",
                    "is_admin": False,
                }},
            }}
        }}

        import nexus.server.auth.factory as factory
        _orig = factory.create_auth_provider
        def _patched(auth_type, auth_config_arg=None, **kwargs):
            if auth_type == "static":
                return StaticAPIKeyAuth.from_config(auth_config)
            return _orig(auth_type, auth_config_arg, **kwargs)
        factory.create_auth_provider = _patched

        import nexus.bricks.rebac.namespace_manager as ns_mod
        _OrigNS = ns_mod.NamespaceManager
        class _NoCacheNS(_OrigNS):
            def __init__(self, **kwargs):
                kwargs["cache_ttl"] = 0
                super().__init__(**kwargs)
        ns_mod.NamespaceManager = _NoCacheNS

        cli_main([
            '--host', '127.0.0.1', '--port', '{port}',
            '--data-dir', '{data_dir}',
            '--auth-type', 'static', '--api-key', '{ADMIN_API_KEY}',
        ])
    """)


# === Fixtures ===


@pytest.fixture(scope="module")
def server():
    """Start nexus server with permissions + zone isolation enabled."""
    port = _find_free_port()
    data_dir = tempfile.mkdtemp(prefix="nexus_stream_e2e_")
    os.makedirs(os.path.join(data_dir, "backend"), exist_ok=True)
    base_url = f"http://127.0.0.1:{port}"
    db_path = os.path.join(data_dir, "nexus_stream_e2e.db")

    env = {
        **os.environ,
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "*",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "NEXUS_DATABASE_URL": f"sqlite:///{db_path}",
        "NEXUS_BACKEND_ROOT": os.path.join(data_dir, "backend"),
        "NEXUS_TENANT_ID": "stream-e2e",
        "NEXUS_ENFORCE_PERMISSIONS": "true",
        "NEXUS_ENFORCE_ZONE_ISOLATION": "true",
        "NEXUS_SEARCH_DAEMON": "false",
        "NEXUS_RATE_LIMIT_ENABLED": "false",
        "NEXUS_UPLOAD_MIN_CHUNK_SIZE": "1",
    }

    startup_script = _build_startup_script(port, data_dir)
    proc = subprocess.Popen(
        [PYTHON, "-c", startup_script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    try:
        _wait_for_health(base_url)
        yield {
            "base_url": base_url,
            "port": port,
            "data_dir": data_dir,
            "process": proc,
        }
    except Exception:
        if sys.platform != "win32":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        stdout = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"Server failed to start. Output:\n{stdout}")
    finally:
        if proc.poll() is None:
            if sys.platform != "win32":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def client(server: dict) -> httpx.Client:
    with httpx.Client(timeout=30, trust_env=False) as c:
        yield c


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_API_KEY}"}


@pytest.fixture()
def alice_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ALICE_API_KEY}"}


def _grant(
    client: httpx.Client,
    base_url: str,
    admin_headers: dict,
    *,
    subject_id: str,
    relation: str,
    object_id: str,
) -> None:
    resp = client.post(
        f"{base_url}/api/nfs/rebac_create",
        json={
            "method": "rebac_create",
            "params": {
                "subject": ["user", subject_id],
                "relation": relation,
                "object": ["file", object_id],
                "zone_id": "test",
            },
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Grant failed: {resp.text}"


# === Tests ===


def test_health(server: dict, client: httpx.Client) -> None:
    """Server is healthy with streaming-capable config."""
    resp = client.get(f"{server['base_url']}/health")
    assert resp.status_code == 200


def test_write_then_stream_roundtrip(
    server: dict,
    client: httpx.Client,
    admin_headers: dict,
) -> None:
    """Write file via API, then stream it back — content must match exactly.

    This exercises:
    - AsyncLocalBackend.write_stream() (or write_content via the API)
    - AsyncLocalBackend.stream_content() via /api/v2/files/stream
    """
    base = server["base_url"]
    path = "/workspace/stream-e2e/roundtrip.bin"

    # Create 256KB of random-looking but deterministic binary data
    content = bytes(range(256)) * 1024  # 256KB
    content_b64 = base64.b64encode(content).decode()
    expected_sha256 = hashlib.sha256(content).hexdigest()

    # Write via API (admin)
    resp = client.post(
        f"{base}/api/v2/files/write",
        json={"path": path, "content": content_b64, "encoding": "base64"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Write failed: {resp.text}"

    # Stream back via /api/v2/files/stream
    resp = client.get(
        f"{base}/api/v2/files/stream",
        params={"path": path},
        headers=admin_headers,
    )
    assert resp.status_code == 200, f"Stream failed: {resp.text}"
    assert len(resp.content) == len(content)
    actual_sha256 = hashlib.sha256(resp.content).hexdigest()
    assert actual_sha256 == expected_sha256, "Content corrupted during stream roundtrip"


def test_stream_with_range_header(
    server: dict,
    client: httpx.Client,
    admin_headers: dict,
) -> None:
    """Stream file with Range header — exercises stream_range() code path.

    This exercises:
    - AsyncLocalBackend.stream_range() with aiofiles
    - HTTP 206 Partial Content response
    """
    base = server["base_url"]
    path = "/workspace/stream-e2e/range-test.bin"

    # Create 100KB file
    content = os.urandom(102400)
    content_b64 = base64.b64encode(content).decode()

    resp = client.post(
        f"{base}/api/v2/files/write",
        json={"path": path, "content": content_b64, "encoding": "base64"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Request first 10KB
    resp = client.get(
        f"{base}/api/v2/files/stream",
        params={"path": path},
        headers={**admin_headers, "Range": "bytes=0-10239"},
    )
    assert resp.status_code == 206
    assert len(resp.content) == 10240
    assert resp.content == content[:10240]

    # Request middle 5KB
    resp = client.get(
        f"{base}/api/v2/files/stream",
        params={"path": path},
        headers={**admin_headers, "Range": "bytes=50000-54999"},
    )
    assert resp.status_code == 206
    assert len(resp.content) == 5000
    assert resp.content == content[50000:55000]

    # Request last 1KB (suffix range)
    resp = client.get(
        f"{base}/api/v2/files/stream",
        params={"path": path},
        headers={**admin_headers, "Range": "bytes=-1024"},
    )
    assert resp.status_code == 206
    assert len(resp.content) == 1024
    assert resp.content == content[-1024:]


def test_stream_permission_denied_without_grant(
    server: dict,
    client: httpx.Client,
    admin_headers: dict,
    alice_headers: dict,
) -> None:
    """Non-admin user without grant cannot stream file (403 or 404).

    Exercises permission enforcement on the streaming endpoint.
    """
    base = server["base_url"]
    path = "/workspace/stream-e2e/secret.bin"

    # Admin writes file
    resp = client.post(
        f"{base}/api/v2/files/write",
        json={"path": path, "content": "secret data"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Alice cannot stream without grant
    resp = client.get(
        f"{base}/api/v2/files/stream",
        params={"path": path},
        headers=alice_headers,
    )
    assert resp.status_code in (403, 404), f"Expected 403/404, got {resp.status_code}"


def test_stream_with_permission_grant(
    server: dict,
    client: httpx.Client,
    admin_headers: dict,
    alice_headers: dict,
) -> None:
    """Non-admin user WITH viewer grant can stream file.

    Exercises permission check + streaming read for normal user.
    """
    base = server["base_url"]
    path = "/workspace/stream-e2e/granted.bin"
    content = b"alice can read this via streaming"

    # Admin writes file
    resp = client.post(
        f"{base}/api/v2/files/write",
        json={
            "path": path,
            "content": base64.b64encode(content).decode(),
            "encoding": "base64",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Grant alice viewer
    _grant(
        client,
        base,
        admin_headers,
        subject_id="alice",
        relation="direct_viewer",
        object_id=path,
    )

    # Alice can now stream
    resp = client.get(
        f"{base}/api/v2/files/stream",
        params={"path": path},
        headers=alice_headers,
    )
    assert resp.status_code == 200, f"Stream failed for alice: {resp.text}"
    assert resp.content == content


def test_large_file_streaming_integrity(
    server: dict,
    client: httpx.Client,
    admin_headers: dict,
) -> None:
    """Write and stream back a 2MB file — validates no corruption at scale.

    This is the key test for the #1625 fix: ensures that the streaming
    code path handles larger files correctly with the new incremental
    hashing and aiofiles-based streaming.
    """
    base = server["base_url"]
    path = "/workspace/stream-e2e/large-file.bin"

    # 2MB of random data
    content = os.urandom(2 * 1024 * 1024)
    content_b64 = base64.b64encode(content).decode()
    expected_sha256 = hashlib.sha256(content).hexdigest()

    # Write
    resp = client.post(
        f"{base}/api/v2/files/write",
        json={"path": path, "content": content_b64, "encoding": "base64"},
        headers=admin_headers,
        timeout=60,
    )
    assert resp.status_code == 200, f"Large file write failed: {resp.text}"

    # Full stream
    resp = client.get(
        f"{base}/api/v2/files/stream",
        params={"path": path},
        headers=admin_headers,
        timeout=60,
    )
    assert resp.status_code == 200
    assert len(resp.content) == len(content)
    actual_sha256 = hashlib.sha256(resp.content).hexdigest()
    assert actual_sha256 == expected_sha256, "Large file corrupted during stream"

    # Range read of last 64KB
    resp = client.get(
        f"{base}/api/v2/files/stream",
        params={"path": path},
        headers={**admin_headers, "Range": "bytes=-65536"},
        timeout=60,
    )
    assert resp.status_code == 206
    assert resp.content == content[-65536:]


def test_multiple_files_streaming(
    server: dict,
    client: httpx.Client,
    admin_headers: dict,
) -> None:
    """Write and stream multiple files — validates no cross-contamination.

    Ensures CAS store_streaming with concurrent-ish writes doesn't
    mix up content between files.
    """
    base = server["base_url"]
    files = {}
    for i in range(5):
        path = f"/workspace/stream-e2e/multi/file-{i}.bin"
        content = os.urandom(32768)  # 32KB each
        files[path] = content

        resp = client.post(
            f"{base}/api/v2/files/write",
            json={
                "path": path,
                "content": base64.b64encode(content).decode(),
                "encoding": "base64",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200

    # Stream each back and verify
    for path, expected in files.items():
        resp = client.get(
            f"{base}/api/v2/files/stream",
            params={"path": path},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.content == expected, f"Content mismatch for {path}"
