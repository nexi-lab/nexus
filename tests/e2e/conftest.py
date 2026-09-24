"""Pytest configuration and fixtures for e2e tests.

Provides fixtures for:
- isolated_db: Isolated SQLite database for each test
- metadata_store: Raft metadata store (from integration tests)
- record_store: In-memory SQLAlchemy record store (from integration tests)
- nexus_server: Actual nexusd process running on a free port
- test_app: httpx client for making real HTTP requests
- nexus_fs: Direct NexusFS instance (no server)

Merged from tests/integration/conftest.py and tests/e2e/conftest.py.
"""

import gc
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from nexus.core.config import PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.record_store import SQLAlchemyRecordStore

# Conditionally ignore MCP tests if fastmcp is not installed
# This must be done at collection time, before any imports from test files
try:
    import fastmcp  # noqa: F401
except ImportError:
    collect_ignore_glob = ["self_contained/mcp/*"]

# Add src directory to Python path for local development
_src_path = Path(__file__).parent.parent.parent / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))


class MembershipStub:
    def __init__(self, *, status: str = "active", role: str = "member", revision: int = 1):
        self.token = f"membership-{uuid.uuid4().hex}"
        state = {"status": status, "role": role, "revision": revision}
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.split("?", 1)[0] != "/api/v1/internal/zone-membership":
                    self.send_error(404)
                    return
                if self.headers.get("Authorization") != f"Bearer {token}":
                    self.send_error(403)
                    return
                payload = json.dumps(state).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = int(self.server.server_address[1])
        self.url = f"http://127.0.0.1:{port}/api/v1/internal/zone-membership"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_membership_stub() -> MembershipStub:
    """Start the shared fixed-membership HTTP stub used by real-process E2E tests."""
    return MembershipStub()


def find_free_port(width: int = 1) -> int:
    """Find a free consecutive low port range outside Windows' ephemeral pool."""
    first_port = 25_000
    last_port = 45_000 - width + 1
    span = last_port - first_port + 1
    start = first_port + (uuid.uuid4().int % span)
    for offset_from_start in range(span):
        sockets: list[socket.socket] = []
        try:
            port = first_port + ((start - first_port + offset_from_start) % span)
            first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(first)
            first.bind(("127.0.0.1", port))
            for offset in range(1, width):
                candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(candidate)
                candidate.bind(("127.0.0.1", port + offset))
            return port
        except OSError:
            continue
        finally:
            for candidate in sockets:
                candidate.close()
    raise RuntimeError(f"could not reserve {width} consecutive loopback ports in 25000-45000")


def _mint_kernel_admin_key(env: dict[str, str], tmp_path: Path) -> str:
    """Mint the full-profile test key into the exact kernel data directory."""
    from nexus.remote.kernel_client import _resolve_kernel_binary

    kernel_binary = _resolve_kernel_binary()
    kernel_data_dir = tmp_path / "metastore"
    identity_dir = tmp_path / "kernel-identity"
    env["NEXUS_API_KEY_SECRET"] = "test-e2e-kernel-secret-12345"
    env["NEXUS_IDENTITY_DIR"] = str(identity_dir)
    env["NEXUS_NO_TLS"] = "true"

    mint_env = env.copy()
    mint_env["NEXUS_DATA_DIR"] = str(kernel_data_dir)
    result = subprocess.run(
        [
            kernel_binary,
            "auth",
            "mint",
            "--subject-type",
            "user",
            "--subject-id",
            "e2e-admin",
            "--admin",
            "--name",
            "python-e2e",
        ],
        env=mint_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    tokens = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("sk-")]
    if result.returncode != 0 or len(tokens) != 1:
        pytest.fail(
            "Failed to mint the Rust-kernel E2E admin key.\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    return tokens[0]


def _drain_pipe(pipe, lines: list[str], ready: "threading.Event | None" = None):
    """Read lines from a subprocess pipe (runs in daemon thread).

    If *ready* is provided and a line contains "Application startup complete",
    the event is set — giving the caller an event-driven readiness signal
    instead of polling /health.
    """
    try:
        for raw in iter(pipe.readline, b""):
            decoded = raw.decode(errors="replace")
            lines.append(decoded)
            if ready and "Application startup complete" in decoded:
                ready.set()
    except ValueError:
        pass  # pipe closed
    finally:
        pipe.close()


def _windows_child_kernel_pids(parent_pid: int) -> list[int]:
    """Capture the exact kernel children before a Windows tree kill can orphan them."""
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process "
                f"| Where-Object {{ $_.ParentProcessId -eq {parent_pid} "
                "-and $_.Name -eq 'nexusd-cluster.exe' }} "
                "| Select-Object -ExpandProperty ProcessId",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


# Aggressive cleanup to prevent SQLite "database is locked" errors
@pytest.fixture(autouse=True)
def cleanup_gc():
    """Force garbage collection after each test."""
    yield
    gc.collect()


@pytest.fixture(scope="function")
def isolated_db(tmp_path, monkeypatch):
    """Create an isolated database path for tests that need guaranteed fresh state.

    This fixture ensures each test gets a completely unique database path
    to prevent any cross-test pollution. It also clears environment variables
    that could override the database path.

    Returns:
        Path: Unique database file path in temporary directory
    """
    # Clear environment variables that would override db_path
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    unique_id = str(uuid.uuid4())[:8]
    db_path = tmp_path / f"test_db_{unique_id}.db"

    yield db_path

    # Clean up database file after test
    if db_path.exists():
        from contextlib import suppress

        with suppress(Exception):  # Best effort cleanup
            db_path.unlink()


@pytest.fixture(scope="function")
def nexus_server(isolated_db, tmp_path):
    """Start actual nexusd process for true e2e testing.

    This fixture:
    1. Creates storage directory and database
    2. Finds a free port
    3. Starts ``nexusd`` as subprocess
    4. Waits for server to be ready
    5. Yields server info (port, base_url)
    6. Kills server process on cleanup

    Returns:
        dict with 'port', 'base_url', 'process'
    """
    membership = start_membership_stub()

    # Set up environment
    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)
    home_path = tmp_path / "home"
    home_path.mkdir(exist_ok=True)

    # The Python HTTP server uses ``port``; its internal kernel and public
    # gRPC face use the next two ports.
    port = find_free_port(3)
    base_url = f"http://127.0.0.1:{port}"

    # Environment for the server process
    env = os.environ.copy()
    env["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"
    env["HOME"] = str(home_path)
    # Allow PostgreSQL via NEXUS_E2E_DATABASE_URL env var; default to SQLite
    env["NEXUS_DATABASE_URL"] = os.environ.get("NEXUS_E2E_DATABASE_URL", f"sqlite:///{isolated_db}")
    env["PYTHONPATH"] = str(_src_path)

    # Full-profile startup requires a real credential provider.  Mint one key
    # into the same Rust data directory the local KernelClient will open, then
    # use that key for both the Python HTTP auth adapter and internal gRPC.
    env["NEXUS_API_KEY"] = _mint_kernel_admin_key(env, tmp_path)
    env["NEXUS_ZONE_DELEGATION_ISSUERS"] = "moss-e2e"
    env["NEXUS_ZONE_MEMBERSHIP_URL"] = membership.url
    env["NEXUS_ZONE_MEMBERSHIP_TOKEN"] = membership.token

    # Issue #788: Lower min chunk size for e2e tests (default 5MB too large for test payloads)
    env["NEXUS_UPLOAD_MIN_CHUNK_SIZE"] = "1"
    env["NEXUS_RATE_LIMIT_ENABLED"] = "false"
    env["NEXUS_SEARCH_DAEMON"] = "false"

    # Issue #2035: Enable RecordStore + ReBAC so skills subscribe/share/unshare
    # and share-link operations have a working EnhancedReBACManager.
    # Without this, the server starts in "bare kernel" mode (no ReBAC).
    env["NEXUS_RECORD_STORE_PATH"] = str(tmp_path / "record_store.db")

    # Issue #1186: Enable lock manager if Dragonfly/Redis is available
    dragonfly_url = env.get("NEXUS_DRAGONFLY_URL") or env.get("REDIS_URL")
    if dragonfly_url:
        env["NEXUS_DRAGONFLY_COORDINATION_URL"] = dragonfly_url
        env["NEXUS_ALLOW_SINGLE_DRAGONFLY"] = "true"

    # Start nexusd process
    # Using python -c to invoke the daemon entry point from source
    # --data-dir sets both storage path and database location
    # Uses FastAPI async server (default) for full API support including Graph API
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from nexus.daemon.main import main; import sys; main(sys.argv[1:])",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--data-dir",
            str(tmp_path),
            "--profile",
            "full",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Use a process group so cleanup can terminate all children.  Python's
        # native flag performs setsid safely in the child; preexec_fn can
        # deadlock when pytest has already started background threads.
        start_new_session=sys.platform != "win32",
    )

    # Event-driven readiness: drain stdout/stderr in background threads and
    # wait for uvicorn's "Application startup complete" log line.  This is
    # deterministic (no polling race) and prevents pipe-buffer deadlocks.
    stderr_lines: list[str] = []
    stdout_lines: list[str] = []
    ready = threading.Event()

    t_err = threading.Thread(
        target=_drain_pipe, args=(process.stderr, stderr_lines, ready), daemon=True
    )
    t_out = threading.Thread(target=_drain_pipe, args=(process.stdout, stdout_lines), daemon=True)
    t_err.start()
    t_out.start()

    # 120s safety ceiling — NOT a polling interval.  The event fires the
    # instant the server emits the log line, so this only triggers on a
    # genuine hang.
    if not ready.wait(timeout=120.0):
        process.terminate()
        t_err.join(timeout=2)
        t_out.join(timeout=2)
        pytest.fail(
            f"Server failed to start on port {port} "
            f"(never saw 'Application startup complete').\n"
            f"stdout: {''.join(stdout_lines)}\n"
            f"stderr: {''.join(stderr_lines)}"
        )

    yield {
        "port": port,
        "base_url": base_url,
        "process": process,
        "db_path": isolated_db,
        "storage_path": storage_path,
        "api_key": env["NEXUS_API_KEY"],
        "stderr_lines": stderr_lines,
    }

    # Cleanup: kill server process and all children
    if sys.platform != "win32":
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    else:
        # Terminating only the Python parent leaves its Rust kernel child
        # running on Windows.  Kill the exact process tree created by this
        # fixture so repeated full-profile tests do not leak daemons/ports.
        child_kernel_pids = _windows_child_kernel_pids(process.pid)
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        for child_pid in child_kernel_pids:
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/F"],
                capture_output=True,
                check=False,
            )

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    membership.close()


@pytest.fixture(scope="function")
def test_app(nexus_server):
    """Create httpx client for making real HTTP requests to the server.

    This is the main fixture tests should use. It provides an httpx client
    configured to talk to the running nexus server.

    Returns:
        httpx.Client configured with base_url
    """
    # trust_env=False prevents httpx from using system proxy settings
    # which can interfere with localhost connections
    with httpx.Client(base_url=nexus_server["base_url"], timeout=30.0, trust_env=False) as client:
        yield client


# Keep the old fixture for backward compatibility during transition
@pytest.fixture(scope="function")
async def nexus_fs(isolated_db, tmp_path):
    """Create a NexusFS instance for testing (direct, no server).

    This is useful for tests that need direct access to NexusFS
    without going through HTTP.
    """
    os.environ["NEXUS_JWT_SECRET"] = "test-secret-key-for-e2e-12345"

    from nexus.backends.storage.cas_local import CASLocalBackend

    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)
    backend = CASLocalBackend(root_path=str(storage_path))

    metadata_store = str(isolated_db).replace(".db", "")
    record_store = SQLAlchemyRecordStore()  # in-memory SQLite for tests
    nx = create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=PermissionConfig(enforce=False),
    )

    yield nx

    nx.close()


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    """Wait for server to be ready by polling /health endpoint.

    Shared helper — previously duplicated across multiple test files.
    """
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


@pytest.fixture
def metadata_store(tmp_path):
    """Create Raft metadata store for tests (primary production path).

    Uses RaftMetadataStore (Strong Consistency, primary production default).

    Returns:
        RaftMetadataStore: Raft-backed metadata store (SC mode)
    """
    store = str(tmp_path / "raft-metadata")
    yield store
    # Cleanup handled by tmp_path


@pytest.fixture
def record_store():
    """Create in-memory RecordStore for tests.

    Uses in-memory SQLite for test isolation.

    Returns:
        SQLAlchemyRecordStore: In-memory SQLite record store
    """
    store = SQLAlchemyRecordStore()  # defaults to sqlite:///:memory:
    yield store
    store.close()
