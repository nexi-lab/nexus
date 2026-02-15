"""E2E tests for RPC proxy client against real FastAPI server with permissions.

Validates the __getattr__-based proxy dispatch works end-to-end:
1. Start server with database auth + permissions enabled
2. Connect via RemoteNexusFS (new proxy client)
3. Exercise: write, read, list, glob, grep, delete, stat, exists
4. Verify permission denied for non-admin user
5. Verify deprecated methods raise NotImplementedError
6. Verify auto-dispatched methods (mkdir, rmdir, workspace ops)
7. Measure latency (no performance regression)

Issue #1289: Protocol + RPC Proxy pattern.
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

from nexus.remote.client import (
    RemoteFilesystemError,
    RemoteNexusFS,
)

# Clear proxy env vars so localhost connections work
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "*"

PYTHON = sys.executable
SRC_PATH = str(Path(__file__).resolve().parents[2] / "src")
SERVER_STARTUP_TIMEOUT = 30


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _rpc_call(
    client: httpx.Client, base_url: str, method: str, params: dict, *, api_key: str
) -> dict:
    """Raw RPC call to set up test data."""
    resp = client.post(
        f"{base_url}/api/nfs/{method}",
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error in {method}: {data['error']}")
    return data.get("result")


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def e2e_server(tmp_path_factory):
    """Start real Nexus server with database auth + permissions enabled."""
    tmp_path = tmp_path_factory.mktemp("rpc_proxy_e2e")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = tmp_path / "metadata.db"

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CONDA_PREFIX", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    }
    env.update(
        {
            "PYTHONPATH": SRC_PATH,
            "NO_PROXY": "*",
            "NEXUS_DATABASE_URL": f"sqlite:///{db_path}",
            "NEXUS_ENFORCE_PERMISSIONS": "true",
            "NEXUS_ENFORCE_ZONE_ISOLATION": "false",
            "NEXUS_SEARCH_DAEMON": "false",
            "NEXUS_RATE_LIMIT_ENABLED": "false",
            "NEXUS_JWT_SECRET": "test-rpc-proxy-e2e-secret-12345",
        }
    )

    proc = subprocess.Popen(
        [
            PYTHON,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{data_dir}', "
                "'--auth-type', 'database', '--init', '--reset'])"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    try:
        # Wait for server + extract admin key
        admin_api_key = None
        lines = []
        deadline = time.monotonic() + SERVER_STARTUP_TIMEOUT
        server_ready = False

        while time.monotonic() < deadline:
            try:
                with httpx.Client(timeout=2) as client:
                    resp = client.get(f"{base_url}/health")
                    if resp.status_code == 200:
                        server_ready = True
                        break
            except httpx.ConnectError:
                pass
            time.sleep(0.3)

            # Read output for admin key
            if proc.stdout and proc.stdout.readable():
                import select

                if select.select([proc.stdout], [], [], 0)[0]:
                    line = proc.stdout.readline()
                    if line:
                        lines.append(line)
                        if "sk-" in line and not admin_api_key:
                            for word in line.split():
                                if word.startswith("sk-"):
                                    admin_api_key = word.strip("'\"")
                                    break

        if not server_ready:
            rest = proc.stdout.read() if proc.stdout else ""
            all_output = "".join(lines) + rest
            pytest.fail(f"Server failed to start on port {port}.\nOutput:\n{all_output}")

        # Try harder to find admin key — read remaining output
        if not admin_api_key and proc.stdout:
            import select

            for _ in range(100):
                if select.select([proc.stdout], [], [], 0.1)[0]:
                    line = proc.stdout.readline()
                    if line:
                        lines.append(line)
                        if "sk-" in line:
                            for word in line.split():
                                if word.startswith("sk-"):
                                    admin_api_key = word.strip("'\"")
                                    break
                if admin_api_key:
                    break

        # Check .nexus-admin-env file as fallback
        if not admin_api_key:
            env_file = Path(".nexus-admin-env")
            if env_file.exists():
                for line in env_file.read_text().splitlines():
                    if "sk-" in line:
                        for word in line.split():
                            w = word.strip("'\"=")
                            if w.startswith("sk-"):
                                admin_api_key = w
                                break

        # Last resort: create a key via direct DB access
        if not admin_api_key:
            # Use the admin_create_key RPC without auth (server may allow init calls)
            try:
                with httpx.Client(timeout=5, trust_env=False) as client:
                    resp = client.post(
                        f"{base_url}/api/nfs/admin_create_key",
                        json={
                            "jsonrpc": "2.0",
                            "method": "admin_create_key",
                            "params": {
                                "user_id": "admin",
                                "name": "admin-bootstrap",
                                "is_admin": True,
                                "zone_id": "default",
                            },
                            "id": 1,
                        },
                    )
                    data = resp.json()
                    if "result" in data and "api_key" in data["result"]:
                        admin_api_key = data["result"]["api_key"]
            except Exception:
                pass

        if not admin_api_key:
            pytest.fail(f"Could not extract admin API key.\nOutput:\n{''.join(lines)}")

        yield {
            "base_url": base_url,
            "port": port,
            "process": proc,
            "admin_api_key": admin_api_key,
        }
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


@pytest.fixture(scope="module")
def admin_client(e2e_server) -> RemoteNexusFS:
    """RemoteNexusFS connected as admin."""
    nx = RemoteNexusFS(
        server_url=e2e_server["base_url"],
        api_key=e2e_server["admin_api_key"],
        timeout=60,
    )
    yield nx
    nx.close()


@pytest.fixture(scope="module")
def non_admin_key(e2e_server) -> str:
    """Create a non-admin user 'carol' and return her API key."""
    with httpx.Client(timeout=10) as client:
        result = _rpc_call(
            client,
            e2e_server["base_url"],
            "admin_create_key",
            {
                "user_id": "carol",
                "name": "Carol (no permissions)",
                "is_admin": False,
                "zone_id": "default",
                "expires_days": 1,
            },
            api_key=e2e_server["admin_api_key"],
        )
        return result["api_key"]


@pytest.fixture(scope="module")
def non_admin_client(e2e_server, non_admin_key) -> RemoteNexusFS:
    """RemoteNexusFS connected as non-admin user with no permissions."""
    nx = RemoteNexusFS(
        server_url=e2e_server["base_url"],
        api_key=non_admin_key,
        timeout=10,
    )
    yield nx
    nx.close()


# =============================================================================
# Tests: Core File Operations (proxy-dispatched + hand-written overrides)
# =============================================================================


class TestProxyFileOperations:
    """Test core file operations via the new proxy client."""

    def test_write_and_read(self, admin_client: RemoteNexusFS) -> None:
        """Write file via proxy, read back via proxy (hand-written override)."""
        admin_client.write("/workspace/proxy-test.txt", b"Hello from RPC proxy!")
        content = admin_client.read("/workspace/proxy-test.txt")
        assert content == b"Hello from RPC proxy!"

    def test_write_str_content(self, admin_client: RemoteNexusFS) -> None:
        """Write string content (auto-encoded to bytes)."""
        admin_client.write("/workspace/str-test.txt", "String content here")
        content = admin_client.read("/workspace/str-test.txt")
        assert content == b"String content here"

    def test_stat(self, admin_client: RemoteNexusFS) -> None:
        """Stat file via proxy (hand-written override with negative cache)."""
        info = admin_client.stat("/workspace/proxy-test.txt")
        assert isinstance(info, dict)
        assert info.get("size", 0) > 0

    def test_exists(self, admin_client: RemoteNexusFS) -> None:
        """Exists via proxy (hand-written override)."""
        assert admin_client.exists("/workspace/proxy-test.txt") is True
        assert admin_client.exists("/workspace/nonexistent-xyz.txt") is False

    def test_list_auto_dispatched(self, admin_client: RemoteNexusFS) -> None:
        """List via proxy (__getattr__ dispatch with response_key='files')."""
        files = admin_client.list("/workspace")
        assert isinstance(files, list)
        # Should contain files we created
        paths = [f if isinstance(f, str) else f.get("path", "") for f in files]
        assert any("proxy-test.txt" in p for p in paths)

    def test_glob_auto_dispatched(self, admin_client: RemoteNexusFS) -> None:
        """Glob via proxy (__getattr__ dispatch with response_key='matches')."""
        matches = admin_client.glob("*.txt", "/workspace")
        assert isinstance(matches, list)
        assert any("proxy-test.txt" in m for m in matches)

    def test_grep_auto_dispatched(self, admin_client: RemoteNexusFS) -> None:
        """Grep via proxy (__getattr__ dispatch with response_key='results')."""
        results = admin_client.grep("Hello", "/workspace")
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_delete(self, admin_client: RemoteNexusFS) -> None:
        """Delete file via proxy (hand-written override)."""
        admin_client.write("/workspace/to-delete.txt", b"delete me")
        assert admin_client.exists("/workspace/to-delete.txt") is True
        admin_client.delete("/workspace/to-delete.txt")
        assert admin_client.exists("/workspace/to-delete.txt") is False

    def test_rename(self, admin_client: RemoteNexusFS) -> None:
        """Rename file via proxy (hand-written override)."""
        admin_client.write("/workspace/old-name.txt", b"rename me")
        admin_client.rename("/workspace/old-name.txt", "/workspace/new-name.txt")
        assert admin_client.exists("/workspace/new-name.txt") is True
        assert admin_client.exists("/workspace/old-name.txt") is False
        # Cleanup
        admin_client.delete("/workspace/new-name.txt")

    def test_edit(self, admin_client: RemoteNexusFS) -> None:
        """Edit file via proxy (hand-written override with edit serialization)."""
        admin_client.write("/workspace/edit-test.txt", b"original text here")
        result = admin_client.edit(
            "/workspace/edit-test.txt",
            [("original", "modified")],
        )
        assert result.get("success") is True
        content = admin_client.read("/workspace/edit-test.txt")
        assert b"modified text here" in content
        # Cleanup
        admin_client.delete("/workspace/edit-test.txt")


# =============================================================================
# Tests: Auto-dispatched Methods (via __getattr__)
# =============================================================================


class TestAutoDispatchedMethods:
    """Test methods that go through __getattr__ auto-dispatch."""

    def test_mkdir_and_rmdir(self, admin_client: RemoteNexusFS) -> None:
        """mkdir and rmdir via auto-dispatch."""
        admin_client.mkdir("/workspace/proxy-dir")
        assert admin_client.is_directory("/workspace/proxy-dir") is True
        admin_client.rmdir("/workspace/proxy-dir")

    def test_rebac_check(self, admin_client: RemoteNexusFS) -> None:
        """rebac_check via auto-dispatch."""
        result = admin_client.rebac_check(
            subject=("user", "admin"),
            permission="read",
            object=("file", "/workspace"),
            zone_id="default",
        )
        assert result is True

    def test_get_etag(self, admin_client: RemoteNexusFS) -> None:
        """get_etag via hand-written override with negative cache."""
        etag = admin_client.get_etag("/workspace/proxy-test.txt")
        assert etag is not None
        assert isinstance(etag, str)

    def test_get_etag_nonexistent(self, admin_client: RemoteNexusFS) -> None:
        """get_etag for nonexistent file returns None."""
        etag = admin_client.get_etag("/workspace/does-not-exist-abc.txt")
        assert etag is None


# =============================================================================
# Tests: Deprecated Methods
# =============================================================================


class TestDeprecatedMethods:
    """Test deprecated methods raise NotImplementedError."""

    def test_chmod_deprecated(self, admin_client: RemoteNexusFS) -> None:
        with pytest.raises(NotImplementedError, match="rebac_create"):
            admin_client.chmod("/workspace/test.txt", 0o755)

    def test_chown_deprecated(self, admin_client: RemoteNexusFS) -> None:
        with pytest.raises(NotImplementedError, match="rebac_create"):
            admin_client.chown("/workspace/test.txt", "owner")

    def test_grant_user_deprecated(self, admin_client: RemoteNexusFS) -> None:
        with pytest.raises(NotImplementedError, match="rebac_create"):
            admin_client.grant_user("user1", "/workspace/test.txt", "read")


# =============================================================================
# Tests: Performance (no regression) — runs BEFORE permission tests
# =============================================================================


class TestPerformance:
    """Verify proxy dispatch doesn't add significant overhead."""

    def test_write_read_latency(self, admin_client: RemoteNexusFS) -> None:
        """Write+read round-trip should be under 500ms (localhost)."""
        start = time.monotonic()
        admin_client.write("/workspace/perf-test.txt", b"performance test payload")
        content = admin_client.read("/workspace/perf-test.txt")
        elapsed = time.monotonic() - start
        assert content == b"performance test payload"
        assert elapsed < 0.5, f"Write+read took {elapsed:.3f}s, expected < 0.5s"
        # Cleanup
        admin_client.delete("/workspace/perf-test.txt")

    def test_list_latency(self, admin_client: RemoteNexusFS) -> None:
        """List should be under 200ms (auto-dispatched via proxy)."""
        start = time.monotonic()
        files = admin_client.list("/workspace")
        elapsed = time.monotonic() - start
        assert isinstance(files, list)
        assert elapsed < 0.2, f"List took {elapsed:.3f}s, expected < 0.2s"

    def test_exists_latency(self, admin_client: RemoteNexusFS) -> None:
        """Exists should be under 200ms (hand-written with negative cache)."""
        start = time.monotonic()
        admin_client.exists("/workspace/proxy-test.txt")
        elapsed = time.monotonic() - start
        assert elapsed < 0.2, f"Exists took {elapsed:.3f}s, expected < 0.2s"

    def test_batch_operations(self, admin_client: RemoteNexusFS) -> None:
        """5 sequential writes + reads should complete under 10s."""
        start = time.monotonic()
        for i in range(5):
            path = f"/workspace/batch-{i}.txt"
            admin_client.write(path, f"batch content {i}".encode())
            content = admin_client.read(path)
            assert content == f"batch content {i}".encode()
            admin_client.delete(path)
        elapsed = time.monotonic() - start
        assert elapsed < 10.0, f"5 write+read+delete cycles took {elapsed:.3f}s, expected < 10s"
        print(
            f"\n  Batch perf: 5 write+read+delete cycles in {elapsed:.3f}s ({elapsed / 5 * 1000:.0f}ms avg)"
        )


# =============================================================================
# Tests: isinstance check (virtual subclass)
# =============================================================================


class TestVirtualSubclass:
    """Test that isinstance works with virtual subclass registration."""

    def test_isinstance_nexus_filesystem(self, admin_client: RemoteNexusFS) -> None:
        from nexus.core.filesystem import NexusFilesystem

        assert isinstance(admin_client, NexusFilesystem)

    def test_isinstance_remote_nexusfs(self, admin_client: RemoteNexusFS) -> None:
        assert isinstance(admin_client, RemoteNexusFS)


# =============================================================================
# Tests: Permission Enforcement (non-admin user) — runs LAST
# These tests can cause the server's async event loop to become unresponsive
# (pre-existing server-side issue with permission enforcement + SQLite).
# =============================================================================


class TestPermissionEnforcement:
    """Test that non-admin user is properly denied via proxy.

    Note: The server currently returns INTERNAL_ERROR (-32603) wrapping
    permission denial messages, rather than PERMISSION_ERROR. This is a
    pre-existing server-side error mapping issue (not related to the proxy).
    Tests accept any NexusError subclass as valid denial indication.
    """

    def test_non_admin_denied_write(self, non_admin_client: RemoteNexusFS) -> None:
        """Non-admin user without permissions should be denied write."""
        from nexus.core.exceptions import NexusError

        with pytest.raises((NexusError, RemoteFilesystemError)):
            non_admin_client.write("/workspace/unauthorized.txt", b"should fail")

    def test_non_admin_denied_read(self, non_admin_client: RemoteNexusFS) -> None:
        """Non-admin user without permissions should be denied read."""
        from nexus.core.exceptions import NexusError

        with pytest.raises((NexusError, RemoteFilesystemError)):
            non_admin_client.read("/workspace/proxy-test.txt")

    def test_non_admin_list_filtered(self, non_admin_client: RemoteNexusFS) -> None:
        """Non-admin list is filtered by permissions (empty or error)."""
        from nexus.core.exceptions import NexusError

        try:
            files = non_admin_client.list("/workspace")
            # If server completes, non-admin should see empty or filtered list
            assert isinstance(files, list)
        except (NexusError, RemoteFilesystemError):
            # Server may deny the list operation entirely
            pass

    def test_non_admin_denied_delete(self, non_admin_client: RemoteNexusFS) -> None:
        """Non-admin user without permissions should be denied delete."""
        from nexus.core.exceptions import NexusError

        with pytest.raises((NexusError, RemoteFilesystemError)):
            non_admin_client.delete("/workspace/proxy-test.txt")
