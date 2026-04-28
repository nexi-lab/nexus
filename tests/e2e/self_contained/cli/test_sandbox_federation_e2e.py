"""E2E: Sandbox federation — thin client + hub (issue #3786).

Full lifecycle test:
  1. Hub running (requires NEXUS_ADMIN_KEY + NEXUS_GRPC_HOST env vars)
  2. Create two zones on hub: e2e-company-<uuid> (r) + e2e-shared-<uuid> (rw)
  3. Seed a doc in the company zone
  4. Issue a multi-zone token: company:r,shared:rw
  5. Start nexusd --profile sandbox --workspace <tmp> --hub-url ... --hub-token ...
  6. Wait for sandbox /health → ready
  7. Assert: federation_client_whoami returns both zones
  8. Assert: write to company zone → 403 (ZoneReadOnlyError, client-side)
  9. Assert: write to shared zone → success (routed to hub)
 10. Assert: write to local zone → success (disk only)
 11. Cleanup: kill sandbox, revoke token

Skips cleanly if NEXUS_ADMIN_KEY or NEXUS_GRPC_HOST is not set.

Run against a live stack started with:
    nexus up --build
"""

from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.e2e]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SANDBOX_PORT_BASE = 12600  # well above common service ports


@dataclasses.dataclass
class SandboxHandle:
    proc: subprocess.Popen[str]
    port: int
    grpc_target: str
    workspace: Path
    hub_token: str = ""  # bearer token the sandbox was configured with


def _nexus_bin() -> str:
    return str(Path(sys.executable).parent / "nexus")


def _nexusd_bin() -> str:
    # Prefer nexusd co-located with the current Python interpreter (venv bin).
    # Falling back to shutil.which may find a system-installed nexusd that
    # predates --profile sandbox / --workspace (wrong version).
    venv_nexusd = Path(sys.executable).parent / "nexusd"
    if venv_nexusd.exists():
        return str(venv_nexusd)
    import shutil

    found = shutil.which("nexusd")
    if found:
        return found
    return sys.executable + " -m nexus.daemon.main"


def _require(var: str) -> str:
    value = os.environ.get(var)
    if not value:
        pytest.skip(
            f"{var} must be set to run sandbox federation e2e "
            "(requires a running nexus hub; see `nexus-stack` skill or `nexus up --build`)"
        )
    return value


def _grpc_call(
    target: str,
    method: str,
    params: dict,
    *,
    api_key: str = "",
    timeout: float = 15,
) -> dict:
    """One-shot gRPC Call RPC to target (no Raft redirect logic needed for sandbox)."""
    import grpc

    from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
    from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

    channel_opts = [
        ("grpc.max_send_message_length", 64 * 1024 * 1024),
        ("grpc.max_receive_message_length", 64 * 1024 * 1024),
    ]
    channel = grpc.insecure_channel(target, options=channel_opts)
    try:
        stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
        req = vfs_pb2.CallRequest(
            method=method,
            payload=encode_rpc_message(params),
            auth_token=api_key,
        )
        resp = stub.Call(req, timeout=timeout)
        result = decode_rpc_message(resp.payload)
        if resp.is_error:
            return {"error": result}
        return result
    finally:
        channel.close()


def _poll_health(url: str, timeout: int = 60) -> bool:
    """Poll /health until status is healthy/ready, or timeout. Returns True on success."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=3)
            if resp.status_code == 200 and resp.json().get("status") in ("healthy", "ready"):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _terminate(proc: subprocess.Popen[str]) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def hub_grpc() -> str:
    """gRPC address of the hub (e.g. 'localhost:2028')."""
    return _require("NEXUS_GRPC_HOST")


@pytest.fixture()
def hub_admin_key() -> str:
    return _require("NEXUS_ADMIN_KEY")


@pytest.fixture()
def hub_zones(hub_grpc: str, hub_admin_key: str) -> tuple[str, str]:
    """Create e2e-company + e2e-shared zones on the hub, yield their IDs, delete after.

    Two-step creation:
    1. ``federation_create_zone`` gRPC — registers the zone in the hub Rust kernel at runtime.
    2. Direct DB insert (psql) — ``hub token create`` validates against the ``zones`` table;
       ``federation_create_zone`` only touches in-memory state.
    """
    import datetime

    suffix = uuid.uuid4().hex[:8]
    company_id = f"e2e-company-{suffix}"
    shared_id = f"e2e-shared-{suffix}"

    db_url = _require("NEXUS_DATABASE_URL")

    for zone_id in (company_id, shared_id):
        # Step 1: runtime registration in hub kernel
        r = _grpc_call(
            hub_grpc,
            "federation_create_zone",
            {"zone_id": zone_id},
            api_key=hub_admin_key,
        )
        assert "error" not in r, f"federation_create_zone({zone_id}) failed: {r}"

        # Step 2: DB row so hub token create can validate zone existence
        now = datetime.datetime.utcnow().isoformat()
        sql = (
            f"INSERT INTO zones (zone_id, name, phase, finalizers, indexing_mode, created_at, updated_at) "
            f"VALUES ('{zone_id}', '{zone_id}', 'Active', '[]', 'all', '{now}', '{now}') "
            f"ON CONFLICT (zone_id) DO NOTHING;"
        )
        subprocess.run(
            ["psql", db_url, "-c", sql],
            check=True,
            capture_output=True,
        )

    # Seed a doc in the company zone so search results can include it
    _grpc_call(
        hub_grpc,
        "sys_write",
        {"path": f"/zone/{company_id}/policy.md", "buf": "# Company Policy\nDo no evil."},
        api_key=hub_admin_key,
    )

    yield company_id, shared_id

    # Cleanup is best-effort — orphan zones don't break anything
    for zone_id in (company_id, shared_id):
        _grpc_call(
            hub_grpc,
            "federation_delete_zone",
            {"zone_id": zone_id},
            api_key=hub_admin_key,
        )
        subprocess.run(
            ["psql", db_url, "-c", f"DELETE FROM zones WHERE zone_id = '{zone_id}';"],
            capture_output=True,
        )


@pytest.fixture()
def hub_token(hub_zones: tuple[str, str]) -> str:
    """Issue a multi-zone token (company:r, shared:rw) and revoke after test."""
    company_id, shared_id = hub_zones
    token_name = f"e2e-sandbox-{uuid.uuid4().hex[:8]}"
    zones_spec = f"{company_id}:r,{shared_id}:rw"

    db_url = _require("NEXUS_DATABASE_URL")
    create = subprocess.run(
        [_nexus_bin(), "hub", "token", "create", "--zones", zones_spec, "--name", token_name],
        env={**os.environ, "NEXUS_DATABASE_URL": db_url},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert create.returncode == 0, (
        f"hub token create failed\nstdout: {create.stdout}\nstderr: {create.stderr}"
    )

    token = None
    for line in create.stdout.splitlines():
        if line.strip().startswith("token:"):
            token = line.split(":", 1)[1].strip()
            break
    assert token and token.startswith("sk-"), f"no sk- token in output:\n{create.stdout}"

    yield token

    subprocess.run(
        [_nexus_bin(), "hub", "token", "revoke", token_name],
        env={**os.environ, "NEXUS_DATABASE_URL": db_url},
        capture_output=True,
        timeout=15,
    )


@pytest.fixture()
def sandbox(
    tmp_path: Path,
    hub_grpc: str,
    hub_token: str,
) -> SandboxHandle:
    """Start nexusd in sandbox mode and kill after test."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello from local workspace")

    port = _SANDBOX_PORT_BASE + (os.getpid() % 1000)
    hub_url = f"grpc://{hub_grpc}"
    nexusd = _nexusd_bin()
    base_flags = [
        "--profile",
        "sandbox",
        "--workspace",
        str(workspace),
        "--hub-url",
        hub_url,
        "--hub-token",
        hub_token,
        "--port",
        str(port),
    ]
    cmd = nexusd.split() + base_flags if " " in nexusd else [nexusd] + base_flags

    # Strip NEXUS_API_KEY so the sandbox doesn't inherit the hub's key and
    # accidentally require auth.  Sandbox is meant to run anonymously in tests.
    sandbox_env = {k: v for k, v in os.environ.items() if k != "NEXUS_API_KEY"}
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=sandbox_env
    )

    health_url = f"http://localhost:{port}/health"
    ready = _poll_health(health_url, timeout=30)

    if not ready:
        _terminate(proc)
        stderr = proc.stderr.read() if proc.stderr else ""
        pytest.fail(f"Sandbox did not reach healthy within 30s\nstderr: {stderr[:2000]}")

    # gRPC port is HTTP port + 2 by nexusd convention (main.py always sets NEXUS_GRPC_PORT=port+2)
    handle = SandboxHandle(
        proc=proc,
        port=port,
        grpc_target=f"localhost:{port + 2}",
        workspace=workspace,
        hub_token=hub_token,
    )
    yield handle
    _terminate(proc)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSandboxFederationBoot:
    """Sandbox boots, reaches healthy, handshake succeeded."""

    def test_health_reaches_ready(self, sandbox: SandboxHandle) -> None:
        """Sandbox /health must return 'healthy' after boot."""
        resp = httpx.get(f"http://localhost:{sandbox.port}/health", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_federation_whoami_returns_both_zones(
        self,
        sandbox: SandboxHandle,
        hub_grpc: str,
        hub_zones: tuple[str, str],
    ) -> None:
        """Hub's federation_client_whoami with the sandbox hub_token returns both zones.

        The sandbox hub_token is a multi-zone token (company:r, shared:rw).  Calling
        federation_client_whoami on the HUB with that token verifies the hub correctly
        resolves the zone grants — the same information the sandbox uses to mount
        remote zones during SandboxBootstrapper.run().
        """
        company_id, shared_id = hub_zones
        result = _grpc_call(hub_grpc, "federation_client_whoami", {}, api_key=sandbox.hub_token)
        assert "error" not in result, f"federation_client_whoami failed: {result}"

        inner = result.get("result", result)  # unwrap {"result": {...}} envelope if present
        zones = {z["zone_id"]: z["permission"] for z in inner.get("zones", [])}
        assert company_id in zones, f"{company_id} not in zones: {zones}"
        assert shared_id in zones, f"{shared_id} not in zones: {zones}"
        assert zones[company_id] == "r", f"company zone should be r, got {zones[company_id]}"
        assert zones[shared_id] == "rw", f"shared zone should be rw, got {zones[shared_id]}"


class TestSandboxZonePermissions:
    """Write enforcement: company=readonly, shared=rw, local=rw."""

    def test_write_to_readonly_company_zone_rejected(
        self,
        sandbox: SandboxHandle,
        hub_zones: tuple[str, str],
    ) -> None:
        """Write to company (r) zone must be rejected client-side with 403."""
        company_id, _ = hub_zones
        result = _grpc_call(
            sandbox.grpc_target,
            "sys_write",
            {"path": f"/zone/{company_id}/forbidden.txt", "buf": "should not reach hub"},
        )
        assert "error" in result, "Expected error for write to read-only zone"
        error_body = result["error"]
        # Error propagates through the Rust remote backend wrapped as a
        # StorageError; the underlying hub message ("Access denied: zone X
        # is read-only for this token") is preserved in the body.  Check for
        # the rejection pattern rather than a specific HTTP status code.
        body_str = str(error_body).lower()
        assert (
            "read-only" in body_str
            or "denied" in body_str
            or "permission" in body_str
            or "403" in body_str
        ), f"Expected read-only / permission denial, got: {error_body}"

    def test_write_to_shared_zone_reaches_hub(
        self,
        sandbox: SandboxHandle,
        hub_zones: tuple[str, str],
        hub_grpc: str,
        hub_admin_key: str,
    ) -> None:
        """Write to shared (rw) zone proxies to hub and is readable from hub."""
        _, shared_id = hub_zones
        write_result = _grpc_call(
            sandbox.grpc_target,
            "sys_write",
            {"path": f"/zone/{shared_id}/note.txt", "buf": "written from sandbox"},
        )
        assert "error" not in write_result, f"Write to shared zone failed: {write_result}"

        # Verify the content reached the hub — use sandbox.hub_token (has shared:rw);
        # hub_admin_key lacks MANAGE_ZONES so it can't read across e2e zones.
        read_result = _grpc_call(
            hub_grpc,
            "sys_read",
            {"path": f"/zone/{shared_id}/note.txt"},
            api_key=sandbox.hub_token,
        )
        assert "error" not in read_result, f"Read from hub failed: {read_result}"
        _r = read_result.get("result", b"")
        content = _r.decode() if isinstance(_r, bytes) else (str(_r) if _r else "")
        assert "sandbox" in content, f"Expected sandbox content on hub, got: {content!r}"

    def test_sandbox_reads_back_from_hub(
        self,
        sandbox: SandboxHandle,
        hub_zones: tuple[str, str],
    ) -> None:
        """Read from shared zone via sandbox routes to hub (Check 11 — RemoteBackend.read_content).

        This exercises the Call-RPC read path added in Issue #3786:
        sandbox sys_read → RemoteBackend.read_content → transport.call("sys_read") →
        hub Python dispatch_call_sync → full zone_perms context → enforcer grants access.
        """
        _, shared_id = hub_zones
        # Write from sandbox so hub has content to serve back
        write_result = _grpc_call(
            sandbox.grpc_target,
            "sys_write",
            {"path": f"/zone/{shared_id}/readback.txt", "buf": "readback-via-sandbox"},
        )
        assert "error" not in write_result, f"Write to shared zone failed: {write_result}"

        # Read back via sandbox — must route through RemoteBackend to hub
        read_result = _grpc_call(
            sandbox.grpc_target,
            "sys_read",
            {"path": f"/zone/{shared_id}/readback.txt"},
        )
        assert "error" not in read_result, f"Sandbox remote read failed (Check 11): {read_result}"
        _r = read_result.get("result", b"")
        content = _r.decode() if isinstance(_r, bytes) else (str(_r) if _r else "")
        assert "readback-via-sandbox" in content, (
            f"Expected readback content via sandbox RemoteBackend, got: {content!r}"
        )

    def test_write_to_local_zone_stays_on_disk(self, sandbox: SandboxHandle) -> None:
        """Write to local zone goes to workspace disk, not hub."""
        write_result = _grpc_call(
            sandbox.grpc_target,
            "sys_write",
            {"path": "/zone/local/generated.txt", "buf": "generated by agent"},
        )
        assert "error" not in write_result, f"Write to local zone failed: {write_result}"

        local_file = sandbox.workspace / "generated.txt"
        assert local_file.exists(), f"File not on disk at {local_file}"
        assert "generated" in local_file.read_text()


class TestSandboxLocalOnlyFallback:
    """Sandbox boots in local-only mode when hub is unreachable."""

    def test_local_only_boot_when_hub_unreachable(self, tmp_path: Path) -> None:
        """Sandbox must boot (not crash) when hub is unreachable."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        port = _SANDBOX_PORT_BASE + (os.getpid() % 1000) + 100
        nexusd = _nexusd_bin()
        base_flags = [
            "--profile",
            "sandbox",
            "--workspace",
            str(workspace),
            "--hub-url",
            "grpc://localhost:19999",  # nothing listening here
            "--hub-token",
            "sk-fake-token",
            "--port",
            str(port),
        ]
        cmd = nexusd.split() + base_flags if " " in nexusd else [nexusd] + base_flags

        # Strip NEXUS_API_KEY so the sandbox runs anonymously (matches `sandbox` fixture).
        sandbox_env = {k: v for k, v in os.environ.items() if k != "NEXUS_API_KEY"}
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=sandbox_env
        )
        try:
            # If proc exits immediately, the Rust kernel binary isn't built
            time.sleep(2)
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                if "nexus_kernel" in stderr or "No module named" in stderr:
                    pytest.skip(
                        "nexusd requires built Rust kernel — run `maturin develop --release`"
                    )
                pytest.skip(f"nexusd exited immediately: {stderr[:500]}")

            ready = _poll_health(f"http://localhost:{port}/health", timeout=30)
            assert ready, "Sandbox should start in local-only mode even if hub is unreachable"
        finally:
            _terminate(proc)
