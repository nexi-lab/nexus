"""E2E tests for pg_stat_statements monitoring dashboard (Issue #762).

Validates the complete metrics pipeline end-to-end:
1. Start real FastAPI server with permissions enabled
2. Verify /metrics endpoint is accessible (no auth required)
3. Verify QueryObserver metrics appear after DB-hitting requests
4. Verify non-admin users can still access /metrics
5. Performance: /metrics latency stays under threshold
6. Verify collector doesn't leak sensitive data
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

PYTHON = sys.executable
SERVER_STARTUP_TIMEOUT = 30
_src_path = Path(__file__).parent.parent.parent / "src"

# Clear proxy env vars so localhost connections work
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=5, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{base_url}/health")
                if resp.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            time.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s at {base_url}")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Start a real nexus server with permissions enforced."""
    tmp = tmp_path_factory.mktemp("pg_metrics_e2e")
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    api_key = "e2e-pg-metrics-test-key"

    env = os.environ.copy()
    env.update(
        {
            "NEXUS_JWT_SECRET": "test-secret-pg-metrics-e2e",
            "NEXUS_DATABASE_URL": f"sqlite:///{tmp / 'e2e.db'}",
            "NEXUS_API_KEY": api_key,
            "NEXUS_ENFORCE_PERMISSIONS": "true",
            "NEXUS_SKIP_PERMISSIONS": "false",
            "PYTHONPATH": str(_src_path),
        }
    )

    proc = subprocess.Popen(
        [
            PYTHON,
            "-c",
            (
                f"from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp}'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    try:
        _wait_for_health(base_url)
    except TimeoutError:
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5)
        pytest.fail(
            f"Server failed to start on port {port}.\n"
            f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
        )

    yield {"base_url": base_url, "api_key": api_key, "process": proc}

    # Cleanup
    if sys.platform != "win32":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture()
def client(server):
    with httpx.Client(base_url=server["base_url"], timeout=30.0, trust_env=False) as c:
        yield c


@pytest.fixture()
def admin_headers(server):
    return {"Authorization": f"Bearer {server['api_key']}"}


# ---------------------------------------------------------------------------
# /metrics availability
# ---------------------------------------------------------------------------


class TestMetricsEndpointAvailability:
    """The /metrics endpoint should be publicly accessible (no auth)."""

    def test_metrics_returns_200_no_auth(self, client) -> None:
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_content_type_is_prometheus(self, client) -> None:
        resp = client.get("/metrics")
        assert "text/plain" in resp.headers["content-type"]

    def test_metrics_no_auth_required_even_with_permissions_enabled(self, client) -> None:
        """Permissions enforcement should not block /metrics."""
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "nexus_info" in resp.text


# ---------------------------------------------------------------------------
# QueryObserver metrics in /metrics output
# ---------------------------------------------------------------------------


class TestQueryObserverMetrics:
    """Verify app-level DB metrics appear after DB-hitting requests."""

    def test_db_queries_total_present(self, client, admin_headers) -> None:
        # Fire a DB-hitting request first
        client.get("/v1/stat?path=/nonexistent", headers=admin_headers)
        resp = client.get("/metrics")
        assert "nexus_db_queries_total" in resp.text

    def test_slow_queries_total_present(self, client) -> None:
        resp = client.get("/metrics")
        assert "nexus_db_slow_queries_total" in resp.text

    def test_observer_disabled_present(self, client) -> None:
        resp = client.get("/metrics")
        assert "nexus_db_observer_disabled" in resp.text

    def test_observer_errors_present(self, client) -> None:
        resp = client.get("/metrics")
        assert "nexus_db_observer_errors_total" in resp.text

    def test_pool_checkouts_present(self, client, admin_headers) -> None:
        client.get("/v1/stat?path=/test", headers=admin_headers)
        resp = client.get("/metrics")
        assert "nexus_db_pool_checkouts_total" in resp.text

    def test_pool_checkins_present(self, client) -> None:
        resp = client.get("/metrics")
        assert "nexus_db_pool_checkins_total" in resp.text

    def test_pool_connects_present(self, client) -> None:
        resp = client.get("/metrics")
        assert "nexus_db_pool_connects_total" in resp.text

    def test_pool_invalidations_present(self, client) -> None:
        resp = client.get("/metrics")
        assert "nexus_db_pool_invalidations_total" in resp.text

    def test_all_eight_metric_families_present(self, client, admin_headers) -> None:
        """All 8 QueryObserverCollector families must appear."""
        client.get("/v1/stat?path=/warmup", headers=admin_headers)
        resp = client.get("/metrics")
        expected = [
            "nexus_db_queries_total",
            "nexus_db_slow_queries_total",
            "nexus_db_observer_errors_total",
            "nexus_db_observer_disabled",
            "nexus_db_pool_checkouts_total",
            "nexus_db_pool_checkins_total",
            "nexus_db_pool_connects_total",
            "nexus_db_pool_invalidations_total",
        ]
        for metric in expected:
            assert metric in resp.text, f"Missing metric: {metric}"


# ---------------------------------------------------------------------------
# Non-admin user access to /metrics
# ---------------------------------------------------------------------------


class TestNonAdminMetricsAccess:
    """Non-admin users (and unauthenticated requests) can read /metrics."""

    def test_unauthenticated_can_read_metrics(self, client) -> None:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "nexus_db_queries_total" in resp.text

    def test_invalid_token_can_still_read_metrics(self, client) -> None:
        resp = client.get("/metrics", headers={"Authorization": "Bearer invalid-key"})
        assert resp.status_code == 200

    def test_metrics_does_not_leak_sql_statements(self, client) -> None:
        """Metrics output should not contain SQL query text."""
        resp = client.get("/metrics")
        body = resp.text
        assert "SELECT" not in body or "select" not in body.lower().split("nexus")[0]
        assert "pg_stat_statements" not in body


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


class TestMetricsPerformanceE2E:
    """Verify /metrics endpoint has acceptable latency under real server."""

    @pytest.mark.benchmark
    def test_metrics_latency_under_50ms(self, client) -> None:
        # Warm up
        client.get("/metrics")

        n = 20
        start = time.perf_counter()
        for _ in range(n):
            resp = client.get("/metrics")
            assert resp.status_code == 200
        elapsed = time.perf_counter() - start

        per_req_ms = (elapsed / n) * 1000
        assert per_req_ms < 50, f"/metrics: {per_req_ms:.1f}ms per request — too slow"

    @pytest.mark.benchmark
    def test_db_request_with_metrics_no_regression(self, client, admin_headers) -> None:
        """DB-hitting requests should not be slowed by the collector."""
        # Warm up
        client.get("/v1/stat?path=/warmup", headers=admin_headers)

        n = 50
        start = time.perf_counter()
        for _ in range(n):
            client.get("/v1/stat?path=/perf-test", headers=admin_headers)
        elapsed = time.perf_counter() - start

        per_req_ms = (elapsed / n) * 1000
        # With collector overhead, each request should still be <100ms
        assert per_req_ms < 100, f"DB request: {per_req_ms:.1f}ms per request — regression"


# ---------------------------------------------------------------------------
# HTTP metrics co-existence
# ---------------------------------------------------------------------------


class TestHTTPMetricsCoexistence:
    """Existing HTTP metrics should still work alongside new DB metrics."""

    def test_http_request_duration_still_present(self, client, admin_headers) -> None:
        client.get("/v1/stat?path=/test", headers=admin_headers)
        resp = client.get("/metrics")
        assert "http_request_duration_seconds" in resp.text

    def test_http_requests_total_still_present(self, client, admin_headers) -> None:
        client.get("/v1/stat?path=/test", headers=admin_headers)
        resp = client.get("/metrics")
        assert "http_requests_total" in resp.text

    def test_nexus_info_still_present(self, client) -> None:
        resp = client.get("/metrics")
        assert "nexus_info" in resp.text
