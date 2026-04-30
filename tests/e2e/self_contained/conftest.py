"""Conftest for self-contained E2E tests.

Provides a lightweight ``nexus_server`` fixture that spins up ``nexusd``
with a temp SQLite database. No external infrastructure required.

Also provides a ``running_nexus`` fixture that drives ``nexus init`` /
``nexus up --build`` / ``nexus down`` against a real Docker Compose
stack — used by approvals E2E (#3790 Tasks 21–23) where the Python
gRPC server on ``:2029`` and PostgreSQL backing must both be live.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, suppress
from pathlib import Path

import pytest


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _drain_pipe(pipe, lines: list[str], ready: threading.Event | None = None) -> None:
    try:
        for raw in iter(pipe.readline, b""):
            decoded = raw.decode(errors="replace")
            lines.append(decoded)
            if ready and "Application startup complete" in decoded:
                ready.set()
    except ValueError:
        pass
    finally:
        pipe.close()


@pytest.fixture(scope="function")
def nexus_server(tmp_path: Path):
    """Start a lightweight ``nexusd`` process for E2E testing.

    Uses SQLite (no PostgreSQL required). Yields a dict with 'port' and 'base_url'.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Use the console_scripts entry point from the same venv
    nexusd_bin = str(Path(sys.executable).parent / "nexusd")

    env = os.environ.copy()
    env["NEXUS_DATABASE_URL"] = f"sqlite:///{tmp_path / 'test.db'}"
    env["NEXUS_JWT_SECRET"] = "e2e-test-secret"
    env["NEXUS_RECORD_STORE_PATH"] = str(tmp_path / "record_store.db")
    env["NEXUS_DATA_DIR"] = str(tmp_path)

    process = subprocess.Popen(
        [
            nexusd_bin,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    stderr_lines: list[str] = []
    stdout_lines: list[str] = []
    ready = threading.Event()

    t_err = threading.Thread(
        target=_drain_pipe, args=(process.stderr, stderr_lines, ready), daemon=True
    )
    t_out = threading.Thread(target=_drain_pipe, args=(process.stdout, stdout_lines), daemon=True)
    t_err.start()
    t_out.start()

    if not ready.wait(timeout=60.0):
        process.terminate()
        t_err.join(timeout=2)
        t_out.join(timeout=2)
        pytest.skip(f"Server failed to start on port {port} (serve command may have been removed).")

    # Quick health check to confirm it's truly up
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with closing(socket.create_connection(("127.0.0.1", port), timeout=1)):
                break
        except OSError:
            time.sleep(0.2)

    yield {"port": port, "base_url": base_url, "process": process}

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


# ---------------------------------------------------------------------------
# running_nexus — full Docker Compose stack for approvals E2E (#3790)
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Check if Docker daemon is running (parity with test_first_run_ux.py)."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@dataclasses.dataclass
class RunningNexus:
    """Handle for a running nexus stack (started via ``nexus up --build``).

    Exposes the connection knobs Tasks 21–23 need:
      - ``http_url``: base URL for the daemon's HTTP API (``/health``,
        ``/hub/approvals/dump``).
      - ``grpc_addr``: ``host:port`` for the Python ApprovalsV1 gRPC
        server (default ``127.0.0.1:2029``).
      - ``admin_token``: the approvals admin token (Bearer secret) callers
        must send as ``authorization: Bearer <token>`` to the gRPC server
        when using the approvals-token fallback path.
      - ``admin_api_key``: the standard admin ``NEXUS_API_KEY`` registered
        in the daemon's database. Recognised by ``require_admin``-gated
        HTTP endpoints (e.g. ``POST /api/v2/auth/keys``) and by the
        ReBACCapabilityAuth gRPC pipeline as ``is_admin=True``.
      - ``diag_token``: the ``NEXUS_APPROVALS_DIAG_TOKEN`` Bearer secret
        required to access ``GET /hub/approvals/dump``. The lifespan
        disables the route entirely when this env var is unset, so tests
        that hit the diag endpoint must send this token.
      - ``zone``: a uuid-prefixed zone id for test isolation.
    """

    http_url: str
    grpc_addr: str
    admin_token: str
    admin_api_key: str
    diag_token: str
    zone: str
    project_dir: Path


@pytest.fixture(scope="class")
def running_nexus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RunningNexus]:
    """Start a full nexus stack via ``nexus up --build`` and tear down.

    Skipped automatically when Docker is unavailable (mirrors the pattern
    in ``tests/e2e/test_first_run_ux.py``). The fixture is class-scoped
    so multiple tests in the same class share one stack startup.

    Environment knobs set on the daemon process (via ``nexus.yaml``):
      - ``NEXUS_APPROVALS_ENABLED=1`` — enable the brick.
      - ``NEXUS_APPROVALS_ADMIN_TOKEN=<random>`` — gates the Python
        gRPC server's approvals-fallback bearer path. We surface the
        same token via ``.admin_token``.
      - ``NEXUS_APPROVALS_GRPC_PORT=2029`` — pinned for E2E reachability;
        the daemon defaults to this anyway, but we set it explicitly so
        the test contract is obvious.
      - ``NEXUS_API_KEY=<sk-e2e-...>`` — pinned admin API key registered
        with the daemon's database via the entrypoint's
        ``setup_admin_api_key`` step. Surfaced as ``.admin_api_key`` so
        tests can drive admin-gated HTTP endpoints (e.g.
        ``POST /api/v2/auth/keys``) and exercise the ReBACCapabilityAuth
        is_admin bypass on the gRPC path.
    """
    if not _docker_available():
        pytest.skip("nexus up requires docker")

    project_dir = tmp_path_factory.mktemp("nexus_running")
    config_path = project_dir / "nexus.yaml"
    data_dir = project_dir / "nexus-data"

    repo_root = Path(__file__).resolve().parents[3]
    compose_file = repo_root / "nexus-stack.yml"
    if not compose_file.exists():
        pytest.skip(f"compose file not found: {compose_file}")

    admin_token = f"e2e-{uuid.uuid4().hex}"
    # Standard admin API key registered with the daemon's database. Used to
    # drive admin-gated HTTP endpoints (POST /api/v2/auth/keys, etc.) and
    # as the is_admin path in ReBACCapabilityAuth. Distinct from
    # admin_token (which gates only the approvals-fallback bearer path).
    # The leading sk- prefix matches the canonical Nexus key format so the
    # CLI/auth pipeline parsers don't reject it as malformed.
    admin_api_key = f"sk-e2e-{uuid.uuid4().hex}"
    # Diag token: NEXUS_APPROVALS_DIAG_TOKEN gates GET /hub/approvals/dump.
    # The lifespan disables the route entirely when unset (#3790 round-13).
    diag_token = f"diag-{uuid.uuid4().hex}"
    zone = f"z-e2e-{uuid.uuid4().hex[:8]}"
    # Resolve the host-side approvals gRPC port. Honor an explicit
    # NEXUS_APPROVALS_GRPC_PORT override (lets ops pin a known port for
    # interactive debugging), otherwise pick a free ephemeral port so
    # parallel xdist workers and stale `nexus down` leftovers don't fight
    # over :2029. We export the resolved port back to up_env below so the
    # docker compose ${...:-2029} substitution and the lifespan's
    # NEXUS_APPROVALS_GRPC_PORT pickup line agree on the same number.
    port_override = os.environ.get("NEXUS_APPROVALS_GRPC_PORT")
    grpc_port = int(port_override) if port_override else _find_free_port()

    # Resolve the `nexus` binary from the active interpreter's venv. When the
    # venv is not on $PATH (common under pytest, which spawns Python from
    # `.venv/bin/python` without exporting the venv into the child env), a
    # bare `nexus` lookup picks up an older system-installed CLI whose
    # presets differ from this checkout. Always invoke the venv-local CLI
    # to keep init/up/down consistent with the source tree. We additionally
    # rewrite ``services``/``compose_profiles`` in the post-init nexus.yaml
    # below so even a stale CLI that lists e.g. ``zoekt`` (no service
    # definition in the in-tree compose file) cannot make ``nexus up``
    # hang on a missing service health-check.
    nexus_bin = str(Path(sys.executable).parent / "nexus")

    # nexus init writes nexus.yaml; we then export the approvals env vars
    # via the user's shell environment so docker compose picks them up.
    init_env = os.environ.copy()
    init_result = subprocess.run(
        [
            nexus_bin,
            "init",
            "--preset",
            "demo",
            "--config-path",
            str(config_path),
            "--data-dir",
            str(data_dir),
            "--compose-file",
            str(compose_file),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(project_dir),
        env=init_env,
    )
    if init_result.returncode != 0:
        pytest.skip(f"nexus init failed: {init_result.stderr}")

    # Defensively normalise nexus.yaml after init:
    #   - Pin api_key to a known value so the test caller can drive
    #     admin-gated HTTP endpoints with the same secret the daemon's
    #     database key registry will receive via NEXUS_API_KEY.
    #   - Restrict services/compose_profiles to the strict three we
    #     actually launch via the in-tree nexus-stack.yml. Older nexus
    #     CLI versions or future preset drift could otherwise list a
    #     service the compose file does not define (e.g. zoekt under
    #     a "search" profile), which makes `nexus up` hang on the
    #     missing service's health check.
    import yaml as _yaml

    with open(config_path) as _cf:
        _cfg = _yaml.safe_load(_cf) or {}
    _cfg["api_key"] = admin_api_key
    _cfg["services"] = ["nexus", "postgres", "dragonfly"]
    _cfg["compose_profiles"] = ["core", "cache"]
    with open(config_path, "w") as _cf:
        _yaml.safe_dump(_cfg, _cf, sort_keys=False)

    up_env = os.environ.copy()
    up_env["NEXUS_APPROVALS_ENABLED"] = "1"
    up_env["NEXUS_APPROVALS_ADMIN_TOKEN"] = admin_token
    up_env["NEXUS_APPROVALS_DIAG_TOKEN"] = diag_token
    up_env["NEXUS_APPROVALS_GRPC_PORT"] = str(grpc_port)
    # Mirror the pinned api_key into NEXUS_API_KEY for the docker compose
    # subprocess. ``nexus up`` already derives this from nexus.yaml's
    # ``api_key`` field, but setting it explicitly guards against future
    # CLI changes that might stop forwarding the YAML value.
    up_env["NEXUS_API_KEY"] = admin_api_key

    up_result = subprocess.run(
        [nexus_bin, "up", "--build"],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(project_dir),
        env=up_env,
    )
    if up_result.returncode != 0:
        # Persist full stdout/stderr to a debug file so we can diagnose
        # subprocess-only failures (e.g. terminal-detection or buffer
        # flush quirks that don't surface on interactive runs).
        debug_path = project_dir / "nexus-up-debug.log"
        debug_path.write_text(
            f"returncode={up_result.returncode}\n\n"
            f"--- stdout ---\n{up_result.stdout}\n\n"
            f"--- stderr ---\n{up_result.stderr}\n"
        )
        pytest.skip(
            f"nexus up failed (rc={up_result.returncode}, full log: {debug_path}): "
            f"stderr_tail={up_result.stderr[-400:]!r}"
        )

    # Re-read config — `nexus up` may have resolved port conflicts and
    # persisted new ports back to nexus.yaml.
    with open(config_path) as f:
        cfg = _yaml.safe_load(f) or {}
    http_port = cfg.get("ports", {}).get("http", 2026)
    http_url = f"http://127.0.0.1:{http_port}"

    # Wait for /health to come up (containers need a moment).
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + 60
    healthy = False
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{http_url}/health", timeout=2) as resp:
                if resp.status == 200:
                    healthy = True
                    break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.0)

    if not healthy:
        # Best-effort teardown then skip — the test cluster isn't usable.
        subprocess.run(
            [nexus_bin, "down"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(project_dir),
        )
        pytest.skip(f"nexus stack failed health check on {http_url}/health")

    handle = RunningNexus(
        http_url=http_url,
        grpc_addr=f"127.0.0.1:{grpc_port}",
        admin_token=admin_token,
        admin_api_key=admin_api_key,
        diag_token=diag_token,
        zone=zone,
        project_dir=project_dir,
    )

    try:
        yield handle
    finally:
        # Operator escape hatch: NEXUS_E2E_KEEP=1 leaves the docker stack
        # running after the test class so logs can be tailed. Set when
        # debugging by hand; never on CI.
        if os.environ.get("NEXUS_E2E_KEEP", "").lower() not in ("1", "true", "yes"):
            subprocess.run(
                [nexus_bin, "down"],
                capture_output=True,
                text=True,
                timeout=180,
                cwd=str(project_dir),
            )
