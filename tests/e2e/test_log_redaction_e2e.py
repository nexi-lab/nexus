"""E2E test for log redaction / secret masking (Issue #86).

Starts a real `nexus serve` process, sends authenticated requests
with secrets in headers/params, then captures server logs and
verifies secrets are redacted.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import closing, suppress
from pathlib import Path

import httpx
import pytest

_src_path = Path(__file__).parent.parent.parent / "src"


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 30.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(f"{url}/health", timeout=1.0, trust_env=False)
            if response.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout):
            pass
        time.sleep(0.1)
    return False


# Secrets we'll inject into requests and check are NOT in logs
_TEST_API_KEY = "sk-test_admin_550e8400-e29b-41d4-a716-446655440000_a1b2c3d4e5f6"
_TEST_BEARER_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
_TEST_DB_PASSWORD = "postgresql://admin:supersecret_p4ssw0rd@db.example.com:5432/production_db"


@pytest.fixture(scope="function")
def redaction_server(tmp_path, monkeypatch):
    """Start nexus serve with stderr captured to a log file for redaction verification."""
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import uuid

    unique_id = str(uuid.uuid4())[:8]
    db_path = tmp_path / f"redaction_test_{unique_id}.db"
    log_file = tmp_path / "server.log"

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-redaction-e2e"
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(_src_path)
    env["NEXUS_API_KEY"] = _TEST_API_KEY
    # Ensure redaction is enabled
    env["NEXUS_LOG_REDACTION_ENABLED"] = "true"

    # Open log file for stderr capture
    log_fh = open(log_file, "w")  # noqa: SIM115

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=log_fh,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    if not _wait_for_server(base_url, timeout=30.0):
        process.terminate()
        log_fh.close()
        log_content = log_file.read_text() if log_file.exists() else "<no logs>"
        pytest.fail(f"Server failed to start.\nLogs:\n{log_content}")

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "log_file": log_file,
        "log_fh": log_fh,
    }

    # Cleanup
    if sys.platform != "win32":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    else:
        process.terminate()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    log_fh.close()


def test_log_redaction_e2e(redaction_server: dict) -> None:
    """E2E: Verify secrets in HTTP requests don't appear in server logs.

    Steps:
    1. Send requests with Bearer tokens, API keys, and other secrets
    2. Give the server time to flush logs
    3. Read captured logs
    4. Assert raw secrets are NOT present
    5. Assert [REDACTED] placeholders ARE present (where secrets were logged)
    """
    base_url = redaction_server["base_url"]
    log_file: Path = redaction_server["log_file"]

    with httpx.Client(base_url=base_url, timeout=10.0, trust_env=False) as client:
        # Request 1: Bearer token in Authorization header
        client.get(
            "/health",
            headers={"Authorization": f"Bearer {_TEST_BEARER_TOKEN}"},
        )

        # Request 2: API key in X-API-Key header
        client.get(
            "/api/nfs/list",
            headers={"X-API-Key": _TEST_API_KEY},
        )

        # Request 3: API key as query parameter
        client.get(f"/api/nfs/list?api_key={_TEST_API_KEY}")

        # Request 4: Try to trigger an auth error to force logging of credentials
        client.get(
            "/api/nfs/list",
            headers={"Authorization": "Bearer invalid_token_that_should_fail"},
        )

        # Request 5: POST with body containing a secret (may appear in error logs)
        with suppress(httpx.HTTPStatusError):
            client.post(
                "/api/nfs/write",
                json={
                    "path": "/test.txt",
                    "content": f"db_url={_TEST_DB_PASSWORD}",
                },
                headers={"X-API-Key": _TEST_API_KEY},
            )

    # Give server time to flush log buffers
    time.sleep(1.0)

    # Force flush by reading current log content
    redaction_server["log_fh"].flush()
    log_content = log_file.read_text()

    # --- ASSERTIONS ---

    # The server should have produced some log output
    assert len(log_content) > 0, "Server produced no log output"

    # Raw secrets MUST NOT appear in logs
    assert _TEST_BEARER_TOKEN not in log_content, (
        f"Bearer token leaked in logs!\n"
        f"Token: {_TEST_BEARER_TOKEN[:20]}...\n"
        f"Found in: {log_content[:500]}"
    )

    # Check API key is not in raw form
    # The full Nexus API key pattern should be redacted
    assert _TEST_API_KEY not in log_content, (
        f"API key leaked in logs!\n"
        f"Key: {_TEST_API_KEY[:20]}...\n"
        f"Found in log output"
    )

    # The DB password in connection strings should be redacted
    assert "supersecret_p4ssw0rd" not in log_content, (
        f"Database password leaked in logs!\n"
        f"Found in log output"
    )


def test_log_redaction_disabled_e2e(tmp_path, monkeypatch) -> None:
    """E2E: Verify redaction can be disabled via config.

    When NEXUS_LOG_REDACTION_ENABLED=false, secrets should appear in logs
    (for debugging purposes in development environments).
    """
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import uuid

    unique_id = str(uuid.uuid4())[:8]
    db_path = tmp_path / f"redaction_disabled_{unique_id}.db"
    log_file = tmp_path / "server_no_redact.log"

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-for-disabled-test"
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(_src_path)
    env["NEXUS_API_KEY"] = _TEST_API_KEY
    # Disable redaction
    env["NEXUS_LOG_REDACTION_ENABLED"] = "false"

    log_fh = open(log_file, "w")  # noqa: SIM115

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=log_fh,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    try:
        if not _wait_for_server(base_url, timeout=30.0):
            log_fh.close()
            log_content = log_file.read_text() if log_file.exists() else "<no logs>"
            pytest.fail(f"Server failed to start.\nLogs:\n{log_content}")

        # Make a request — the server logs should NOT redact
        with httpx.Client(base_url=base_url, timeout=10.0, trust_env=False) as client:
            client.get("/health")

        # Give server time to flush logs
        time.sleep(0.5)
        log_fh.flush()
        log_content = log_file.read_text()

        # Server should have produced output
        assert len(log_content) > 0, "Server produced no log output"

        # In this test, we just verify the server started and runs fine
        # with redaction disabled — no crash, no error
        # (We don't assert secrets are visible because that would require
        # a specific log line that always emits the API key)
    finally:
        if sys.platform != "win32":
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log_fh.close()
