"""E2E tests for agent registration with wallet provisioning (Issue #1210).

Tests the full stack: FastAPI server + PostgreSQL + TigerBeetle.
Verifies that registering an agent via RPC auto-provisions a TigerBeetle wallet,
and that capabilities are stored and retrievable.

Requirements:
    - PostgreSQL running at postgresql://scorpio@localhost:5432/nexus_e2e_test
    - TigerBeetle running at 127.0.0.1:3000
    - tigerbeetle Python package installed

Run with:
    pytest tests/e2e/test_agent_wallet_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from contextlib import closing, suppress
from pathlib import Path

import httpx
import pytest

POSTGRES_URL = os.getenv(
    "NEXUS_E2E_DATABASE_URL",
    "postgresql://scorpio@localhost:5432/nexus_e2e_test",
)
TIGERBEETLE_ADDRESS = os.getenv("TIGERBEETLE_ADDRESS", "127.0.0.1:3000")

_src_path = Path(__file__).parent.parent.parent / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))


def find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
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


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------


def _postgres_available() -> bool:
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(POSTGRES_URL, echo=False, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


def _tigerbeetle_available() -> bool:
    try:
        import tigerbeetle as tb

        client = tb.ClientSync(cluster_id=0, replica_addresses=TIGERBEETLE_ADDRESS)
        # Quick health check: lookup a non-existent account
        client.lookup_accounts([0])
        return True
    except Exception:
        return False


skip_no_postgres = pytest.mark.skipif(
    not _postgres_available(), reason=f"PostgreSQL not available at {POSTGRES_URL}"
)
skip_no_tigerbeetle = pytest.mark.skipif(
    not _tigerbeetle_available(),
    reason=f"TigerBeetle not available at {TIGERBEETLE_ADDRESS}",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_engine():
    from sqlalchemy import create_engine, text

    from nexus.storage.models import Base

    engine = create_engine(POSTGRES_URL, echo=False, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def tb_client():
    """Create a TigerBeetle sync client for verification."""
    import tigerbeetle as tb

    client = tb.ClientSync(cluster_id=0, replica_addresses=TIGERBEETLE_ADDRESS)
    yield client


# ---------------------------------------------------------------------------
# Server fixture with NEXUS_PAY_ENABLED
# ---------------------------------------------------------------------------


@pytest.fixture
def nexus_server_with_pay(tmp_path, pg_engine):
    """Start nexus server with PostgreSQL, database auth, AND wallet provisioning."""
    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-wallet"
    env["NEXUS_DATABASE_URL"] = POSTGRES_URL
    env["PYTHONPATH"] = str(_src_path)
    env["NEXUS_ENFORCE_PERMISSIONS"] = "false"  # Non-user permission mode
    env["NEXUS_PAY_ENABLED"] = "true"
    env["TIGERBEETLE_ADDRESS"] = TIGERBEETLE_ADDRESS
    env["TIGERBEETLE_CLUSTER_ID"] = "0"

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from nexus.cli import main; "
                f"main(['serve', '--host', '127.0.0.1', '--port', '{port}', "
                f"'--data-dir', '{tmp_path}', '--auth-type', 'database', "
                f"'--init', '--reset', '--admin-user', 'e2e-wallet-admin'])"
            ),
        ],
        env=env,
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    if not wait_for_server(base_url, timeout=30.0):
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"Server failed to start on port {port}.\n"
            f"stdout: {stdout.decode()[:2000]}\n"
            f"stderr: {stderr.decode()[:2000]}"
        )

    admin_env_file = tmp_path / ".nexus-admin-env"
    api_key = None
    if admin_env_file.exists():
        for line in admin_env_file.read_text().splitlines():
            if "NEXUS_API_KEY=" in line:
                value = line.split("NEXUS_API_KEY=", 1)[1].strip()
                api_key = value.strip("'\"")
                break

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "api_key": api_key,
    }

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


def _rpc_call(base_url: str, api_key: str, method: str, params: dict) -> dict:
    """Make an RPC call and return the response JSON."""
    response = httpx.post(
        f"{base_url}/api/nfs/{method}",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        },
        timeout=10.0,
        trust_env=False,
    )
    return response.json()


# ---------------------------------------------------------------------------
# E2E Tests
# ---------------------------------------------------------------------------


@skip_no_postgres
@skip_no_tigerbeetle
class TestWalletProvisioningE2E:
    """E2E: register agent via RPC → verify wallet created in TigerBeetle."""

    def test_register_agent_creates_tigerbeetle_wallet(
        self, nexus_server_with_pay, tb_client
    ):
        """Registering an agent via RPC auto-provisions a TigerBeetle wallet."""
        from nexus.pay.constants import ACCOUNT_CODE_WALLET, LEDGER_CREDITS, make_tb_account_id

        api_key = nexus_server_with_pay["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_with_pay["base_url"]
        agent_id = "e2e-wallet-admin,WalletTestAgent"

        # Register agent via RPC
        result = _rpc_call(
            base_url,
            api_key,
            "register_agent",
            {
                "agent_id": agent_id,
                "name": "Wallet Test Agent",
                "description": "E2E test for wallet provisioning",
            },
        )
        assert result.get("error") is None, f"register_agent error: {result.get('error')}"
        assert result["result"]["agent_id"] == agent_id

        # Verify the wallet was created in TigerBeetle
        tb_id = make_tb_account_id("default", agent_id)
        accounts = tb_client.lookup_accounts([tb_id])
        assert len(accounts) == 1, (
            f"Expected 1 TigerBeetle account for {agent_id}, found {len(accounts)}"
        )

        account = accounts[0]
        assert account.ledger == LEDGER_CREDITS
        assert account.code == ACCOUNT_CODE_WALLET
        # Verify DEBITS_MUST_NOT_EXCEED_CREDITS flag is set (overdraft protection)
        import tigerbeetle as tb
        assert account.flags & tb.AccountFlags.DEBITS_MUST_NOT_EXCEED_CREDITS

    def test_register_agent_with_capabilities(self, nexus_server_with_pay):
        """Registering an agent with capabilities stores them in metadata."""
        api_key = nexus_server_with_pay["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_with_pay["base_url"]
        agent_id = "e2e-wallet-admin,CapabilitiesAgent"

        # Register with capabilities
        result = _rpc_call(
            base_url,
            api_key,
            "register_agent",
            {
                "agent_id": agent_id,
                "name": "Capabilities Agent",
                "capabilities": ["search", "analyze", "code"],
            },
        )
        assert result.get("error") is None, f"register_agent error: {result.get('error')}"
        assert result["result"]["agent_id"] == agent_id
        assert result["result"].get("capabilities") == ["search", "analyze", "code"]

    def test_wallet_idempotent_on_re_register(self, nexus_server_with_pay, tb_client):
        """Re-registering an existing agent doesn't fail (wallet is idempotent)."""
        from nexus.pay.constants import make_tb_account_id

        api_key = nexus_server_with_pay["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_with_pay["base_url"]
        agent_id = "e2e-wallet-admin,IdempotentAgent"

        # Register once
        result1 = _rpc_call(
            base_url,
            api_key,
            "register_agent",
            {"agent_id": agent_id, "name": "Idempotent Agent"},
        )
        assert result1.get("error") is None

        # Try to register again — should get "already exists" error (NexusFS behavior)
        # but not a wallet creation crash
        result2 = _rpc_call(
            base_url,
            api_key,
            "register_agent",
            {"agent_id": agent_id, "name": "Idempotent Agent"},
        )
        # NexusFS raises ValueError for re-registration, which becomes an RPC error
        # The key assertion: it doesn't crash with a TigerBeetle error
        # Either succeeds (idempotent) or returns a clean "already exists" error
        if result2.get("error"):
            assert "already exists" in str(result2["error"]).lower() or "Agent already exists" in str(result2["error"])

        # Wallet should still exist in TigerBeetle
        tb_id = make_tb_account_id("default", agent_id)
        accounts = tb_client.lookup_accounts([tb_id])
        assert len(accounts) == 1

    def test_delete_agent_with_wallet(self, nexus_server_with_pay, tb_client):
        """Deleting an agent with wallet completes without error."""
        from nexus.pay.constants import make_tb_account_id

        api_key = nexus_server_with_pay["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_with_pay["base_url"]
        agent_id = "e2e-wallet-admin,DeleteWalletAgent"

        # Register agent (creates wallet)
        result = _rpc_call(
            base_url,
            api_key,
            "register_agent",
            {"agent_id": agent_id, "name": "Delete Wallet Agent"},
        )
        assert result.get("error") is None

        # Verify wallet exists
        tb_id = make_tb_account_id("default", agent_id)
        accounts = tb_client.lookup_accounts([tb_id])
        assert len(accounts) == 1

        # Delete agent
        del_result = _rpc_call(
            base_url,
            api_key,
            "delete_agent",
            {"agent_id": agent_id},
        )
        assert del_result.get("error") is None

        # TigerBeetle accounts are immutable — account still exists
        # but the agent is removed from the registry
        accounts_after = tb_client.lookup_accounts([tb_id])
        assert len(accounts_after) == 1  # TB accounts are never deleted

    def test_register_agent_wallet_has_zero_balance(self, nexus_server_with_pay, tb_client):
        """Newly provisioned wallet starts with zero balance."""
        from nexus.pay.constants import make_tb_account_id

        api_key = nexus_server_with_pay["api_key"]
        if not api_key:
            pytest.skip("Admin API key not found")

        base_url = nexus_server_with_pay["base_url"]
        agent_id = "e2e-wallet-admin,ZeroBalanceAgent"

        result = _rpc_call(
            base_url,
            api_key,
            "register_agent",
            {"agent_id": agent_id, "name": "Zero Balance Agent"},
        )
        assert result.get("error") is None

        tb_id = make_tb_account_id("default", agent_id)
        accounts = tb_client.lookup_accounts([tb_id])
        assert len(accounts) == 1

        account = accounts[0]
        assert account.credits_posted == 0
        assert account.debits_posted == 0
        assert account.credits_pending == 0
        assert account.debits_pending == 0


# ---------------------------------------------------------------------------
# Permissions-enabled server fixture (StaticAPIKeyAuth: admin + non-admin)
# ---------------------------------------------------------------------------

ADMIN_KEY = "sk-admin-wallet-e2e"
ALICE_KEY = "sk-alice-wallet-e2e"


def _build_permissions_startup_script(port: int, data_dir: str) -> str:
    """Build startup script with StaticAPIKeyAuth (admin + alice) and permissions ON."""
    return textwrap.dedent(f"""\
        import os, sys, logging
        logging.basicConfig(level=logging.INFO)
        sys.path.insert(0, os.getenv("PYTHONPATH", ""))

        from nexus.server.auth.static_key import StaticAPIKeyAuth
        from nexus.cli import main as cli_main

        auth_config = {{
            "api_keys": {{
                "{ADMIN_KEY}": {{
                    "subject_type": "user",
                    "subject_id": "admin",
                    "zone_id": "default",
                    "is_admin": True,
                }},
                "{ALICE_KEY}": {{
                    "subject_type": "user",
                    "subject_id": "alice",
                    "zone_id": "default",
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

        cli_main([
            'serve', '--host', '127.0.0.1', '--port', '{port}',
            '--data-dir', '{data_dir}',
            '--auth-type', 'static', '--api-key', '{ADMIN_KEY}',
        ])
    """)


@pytest.fixture
def nexus_server_permissions(tmp_path, pg_engine):
    """Start nexus server with permissions ON, wallet provisioning, and multi-key auth."""
    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["NEXUS_DATABASE_URL"] = POSTGRES_URL
    env["PYTHONPATH"] = str(_src_path)
    env["NEXUS_ENFORCE_PERMISSIONS"] = "true"
    env["NEXUS_PAY_ENABLED"] = "true"
    env["TIGERBEETLE_ADDRESS"] = TIGERBEETLE_ADDRESS
    env["TIGERBEETLE_CLUSTER_ID"] = "0"

    script = _build_permissions_startup_script(port, str(tmp_path))

    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env=env,
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    if not wait_for_server(base_url, timeout=30.0):
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"Server (permissions) failed to start on port {port}.\n"
            f"stdout: {stdout.decode()[:2000]}\n"
            f"stderr: {stderr.decode()[:2000]}"
        )

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "admin_key": ADMIN_KEY,
        "alice_key": ALICE_KEY,
    }

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


# ---------------------------------------------------------------------------
# E2E Tests — Permissions Enabled + Non-Admin User
# ---------------------------------------------------------------------------


@skip_no_postgres
@skip_no_tigerbeetle
class TestWalletProvisioningWithPermissions:
    """E2E: wallet provisioning with NEXUS_ENFORCE_PERMISSIONS=true and non-admin user."""

    def test_non_admin_registers_agent_with_wallet(
        self, nexus_server_permissions, tb_client
    ):
        """Non-admin user (alice) can register an agent and get a wallet provisioned."""
        from nexus.pay.constants import ACCOUNT_CODE_WALLET, LEDGER_CREDITS, make_tb_account_id

        srv = nexus_server_permissions
        agent_id = "alice,PermWalletAgent"

        result = _rpc_call(
            srv["base_url"],
            srv["alice_key"],
            "register_agent",
            {"agent_id": agent_id, "name": "Perm Wallet Agent"},
        )
        assert result.get("error") is None, f"register_agent error: {result.get('error')}"
        assert result["result"]["agent_id"] == agent_id

        # Verify wallet in TigerBeetle
        tb_id = make_tb_account_id("default", agent_id)
        accounts = tb_client.lookup_accounts([tb_id])
        assert len(accounts) == 1
        assert accounts[0].ledger == LEDGER_CREDITS
        assert accounts[0].code == ACCOUNT_CODE_WALLET

    def test_non_admin_registers_agent_with_capabilities(
        self, nexus_server_permissions
    ):
        """Non-admin user can register an agent with capabilities under permissions."""
        srv = nexus_server_permissions
        agent_id = "alice,PermCapsAgent"

        result = _rpc_call(
            srv["base_url"],
            srv["alice_key"],
            "register_agent",
            {
                "agent_id": agent_id,
                "name": "Perm Caps Agent",
                "capabilities": ["search", "code"],
            },
        )
        assert result.get("error") is None, f"register_agent error: {result.get('error')}"
        assert result["result"].get("capabilities") == ["search", "code"]

    def test_non_admin_delete_agent_with_wallet(
        self, nexus_server_permissions, tb_client
    ):
        """Non-admin user can delete their own agent (wallet stays in TB)."""
        from nexus.pay.constants import make_tb_account_id

        srv = nexus_server_permissions
        agent_id = "alice,PermDeleteAgent"

        # Register
        reg = _rpc_call(
            srv["base_url"],
            srv["alice_key"],
            "register_agent",
            {"agent_id": agent_id, "name": "Perm Delete Agent"},
        )
        assert reg.get("error") is None

        # Delete
        del_result = _rpc_call(
            srv["base_url"],
            srv["alice_key"],
            "delete_agent",
            {"agent_id": agent_id},
        )
        assert del_result.get("error") is None

        # TB account still exists (immutable)
        tb_id = make_tb_account_id("default", agent_id)
        accounts = tb_client.lookup_accounts([tb_id])
        assert len(accounts) == 1
