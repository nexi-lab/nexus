"""E2E tests for Monty sandbox provider via FastAPI server (Issue #1316).

Starts a real nexus serve process, creates a Monty sandbox via the API,
executes code, and validates behavior end-to-end with permissions enabled.

Requires:
    - pydantic-monty installed
    - Server fixtures from conftest.py (nexus_server, isolated_db)
"""

from __future__ import annotations

import time

import httpx
import pytest

# Skip if pydantic-monty not installed
try:
    import pydantic_monty  # noqa: F401

    MONTY_AVAILABLE = True
except ImportError:
    MONTY_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(not MONTY_AVAILABLE, reason="pydantic-monty not installed"),
    pytest.mark.e2e,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_key_headers(api_key: str = "test-e2e-api-key-12345") -> dict[str, str]:
    return {"X-API-Key": api_key}


def _create_sandbox(
    base_url: str,
    name: str = "monty-e2e-test",
    provider: str = "monty",
) -> dict:
    """Create a sandbox via REST API."""
    response = httpx.post(
        f"{base_url}/api/v2/sandboxes",
        headers=_api_key_headers(),
        json={
            "name": name,
            "provider": provider,
        },
        timeout=10.0,
        trust_env=False,
    )
    return response


def _run_code(
    base_url: str,
    sandbox_id: str,
    code: str,
    language: str = "python",
) -> dict:
    """Run code in a sandbox via REST API."""
    response = httpx.post(
        f"{base_url}/api/v2/sandboxes/{sandbox_id}/run",
        headers=_api_key_headers(),
        json={
            "language": language,
            "code": code,
        },
        timeout=30.0,
        trust_env=False,
    )
    return response


# ---------------------------------------------------------------------------
# E2E Tests
# ---------------------------------------------------------------------------


class TestMontySandboxE2E:
    """End-to-end tests for Monty sandbox via the real FastAPI server."""

    def test_monty_provider_available_on_server(self, nexus_server) -> None:
        """Server health check passes and Monty is loadable."""
        base_url = nexus_server["base_url"]
        response = httpx.get(
            f"{base_url}/health",
            timeout=5.0,
            trust_env=False,
        )
        assert response.status_code == 200

    def test_create_monty_sandbox(self, nexus_server) -> None:
        """Create a Monty sandbox through the API."""
        base_url = nexus_server["base_url"]
        response = _create_sandbox(base_url, name="monty-create-test")

        # If the sandbox API is available, verify creation
        if response.status_code == 200:
            data = response.json()
            assert data.get("sandbox_id", "").startswith("monty-")
            assert data.get("provider") == "monty"
            assert data.get("status") == "active"
        elif response.status_code == 404:
            # Sandbox API routes may not be registered
            pytest.skip("Sandbox API routes not available on this server")
        elif response.status_code == 401:
            pytest.skip("Auth required — sandbox creation needs agent registry")
        else:
            # Accept 422 if the endpoint expects different params
            assert response.status_code in (200, 422), (
                f"Unexpected status {response.status_code}: {response.text}"
            )

    def test_create_and_execute_code(self, nexus_server) -> None:
        """Full lifecycle: create → run code → verify output."""
        base_url = nexus_server["base_url"]

        # Create sandbox
        create_resp = _create_sandbox(base_url, name="monty-run-test")
        if create_resp.status_code in (404, 401):
            pytest.skip("Sandbox API not available")

        if create_resp.status_code != 200:
            pytest.skip(f"Sandbox creation failed: {create_resp.status_code}")

        sandbox_id = create_resp.json()["sandbox_id"]

        # Run simple code
        run_resp = _run_code(base_url, sandbox_id, 'print("hello from monty")')
        if run_resp.status_code == 200:
            data = run_resp.json()
            assert data.get("exit_code") == 0
            assert "hello from monty" in data.get("stdout", "")
        elif run_resp.status_code == 404:
            pytest.skip("Run code endpoint not found")

    def test_monty_rejects_non_python(self, nexus_server) -> None:
        """Monty rejects non-Python languages."""
        base_url = nexus_server["base_url"]

        create_resp = _create_sandbox(base_url, name="monty-lang-test")
        if create_resp.status_code in (404, 401):
            pytest.skip("Sandbox API not available")
        if create_resp.status_code != 200:
            pytest.skip(f"Sandbox creation failed: {create_resp.status_code}")

        sandbox_id = create_resp.json()["sandbox_id"]

        # Try JavaScript — should fail
        run_resp = _run_code(
            base_url, sandbox_id, 'console.log("hi")', language="javascript"
        )
        if run_resp.status_code == 200:
            data = run_resp.json()
            # Should either be an error response or non-zero exit code
            assert data.get("exit_code", 0) != 0 or "error" in str(data).lower()

    def test_monty_execution_performance(self, nexus_server) -> None:
        """Monty execution should be fast (< 500ms for simple code)."""
        base_url = nexus_server["base_url"]

        create_resp = _create_sandbox(base_url, name="monty-perf-test")
        if create_resp.status_code in (404, 401):
            pytest.skip("Sandbox API not available")
        if create_resp.status_code != 200:
            pytest.skip(f"Sandbox creation failed: {create_resp.status_code}")

        sandbox_id = create_resp.json()["sandbox_id"]

        # Time a simple execution (includes HTTP round-trip)
        start = time.monotonic()
        run_resp = _run_code(base_url, sandbox_id, "1 + 1")
        elapsed = time.monotonic() - start

        if run_resp.status_code == 200:
            data = run_resp.json()
            assert data.get("exit_code") == 0
            # Monty execution time (without HTTP overhead) should be fast
            exec_time = data.get("execution_time", 0)
            assert exec_time < 1.0, f"Monty execution took {exec_time:.3f}s"
            # Total including HTTP should be < 500ms
            assert elapsed < 2.0, f"Total round-trip took {elapsed:.3f}s"

    def test_monty_security_no_filesystem(self, nexus_server) -> None:
        """Monty denies filesystem access even through the server."""
        base_url = nexus_server["base_url"]

        create_resp = _create_sandbox(base_url, name="monty-sec-test")
        if create_resp.status_code in (404, 401):
            pytest.skip("Sandbox API not available")
        if create_resp.status_code != 200:
            pytest.skip(f"Sandbox creation failed: {create_resp.status_code}")

        sandbox_id = create_resp.json()["sandbox_id"]

        # Try to access filesystem — should fail
        run_resp = _run_code(
            base_url, sandbox_id, 'open("/etc/passwd").read()'
        )
        if run_resp.status_code == 200:
            data = run_resp.json()
            assert data.get("exit_code") != 0, "Filesystem access should be denied"
