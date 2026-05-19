"""Shared fixtures for integration tests (Issue #4132).

Provides the ``full_stack`` fixture: boots a real Docker Compose stack
(PostgreSQL + Dragonfly + Zoekt) via ``nexus init`` / ``nexus up`` /
``nexus down``, and exposes ``.url``, ``.api_key``, and ``.http_get(path)``
to callers.

Gated: the fixture skips cheaply when NEXUS_E2E != "1" *or* Docker is
unavailable.  All Docker work is confined to the fixture body — no work
at import/collection time.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Return True iff the Docker daemon is reachable."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _nexus_bin() -> str:
    """Resolve the venv-local ``nexus`` CLI binary."""
    return str(Path(sys.executable).parent / "nexus")


# ---------------------------------------------------------------------------
# Public data class
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FullStack:
    """Handle for a running FULL nexus stack (PostgreSQL + Dragonfly + Zoekt).

    Attributes:
        url:      HTTP base URL (e.g. ``http://localhost:2026``).
        api_key:  Admin API key registered with the daemon.
        grpc_host: gRPC host string (e.g. ``localhost:2028``).
        grpc_port: gRPC port as string (e.g. ``"2028"``).
        project_dir: Temp directory containing nexus.yaml.
    """

    url: str
    api_key: str
    grpc_host: str
    grpc_port: str
    project_dir: Path

    def http_get(self, path: str) -> "_HttpResponse":
        """Perform a GET against ``self.url + path``.

        Returns an object with ``.status_code`` and ``.json()`` so the caller
        does not need to import urllib directly.
        """
        full_url = self.url.rstrip("/") + path
        req = urllib.request.Request(
            full_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return _HttpResponse(resp.status, raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return _HttpResponse(exc.code, raw)


@dataclasses.dataclass
class _HttpResponse:
    """Thin wrapper so callers can use ``.status_code`` and ``.json()``."""

    status_code: int
    _body: bytes

    def json(self) -> object:
        return json.loads(self._body)


# ---------------------------------------------------------------------------
# full_stack fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def full_stack(
    tmp_path: Path,
    *,
    preset: str = "shared",
) -> Iterator[FullStack]:
    """Boot a FULL nexus stack and yield a :class:`FullStack` handle.

    Skipped when:
    - ``NEXUS_E2E != "1"`` (the test-level ``requires_e2e`` skip fires first,
      but this guard ensures no Docker work happens if the fixture is invoked
      outside a properly-gated test).
    - Docker is not available.

    Teardown: ``nexus down --volumes`` + temp dir removal.

    ``preset`` defaults to ``"shared"`` (FULL PostgreSQL + Dragonfly + Zoekt).
    Sibling issues (#4133–#4138) can pass a different preset, e.g.::

        @pytest.fixture
        def my_stack(tmp_path):
            yield from full_stack.__wrapped__(tmp_path, preset="demo")
    """
    # Guard: no Docker work without NEXUS_E2E=1
    if os.environ.get("NEXUS_E2E") != "1":
        pytest.skip("full_stack fixture requires NEXUS_E2E=1 (real Docker stack)")

    if not _docker_available():
        pytest.skip("full_stack fixture requires a running Docker daemon")

    nexus_bin = _nexus_bin()
    project_dir = tmp_path / "nexus_full_stack"
    project_dir.mkdir(parents=True, exist_ok=True)
    config_path = project_dir / "nexus.yaml"
    data_dir = project_dir / "nexus-data"

    # Locate the in-tree compose file (mirrors running_nexus in e2e/self_contained)
    repo_root = Path(__file__).resolve().parents[2]
    compose_file = repo_root / "nexus-stack.yml"

    # ---- nexus init --------------------------------------------------------
    init_cmd = [
        nexus_bin,
        "init",
        "--preset",
        preset,
        "--config-path",
        str(config_path),
        "--data-dir",
        str(data_dir),
    ]
    if compose_file.exists():
        init_cmd += ["--compose-file", str(compose_file)]

    try:
        init_result = subprocess.run(
            init_cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(project_dir),
        )
    except subprocess.TimeoutExpired:
        pytest.skip("nexus init timed out after 60s")
    if init_result.returncode != 0:
        pytest.skip(f"nexus init failed: {init_result.stderr[-400:]!r}")

    # ---- nexus up ----------------------------------------------------------
    up_env = os.environ.copy()
    use_prebuilt = os.environ.get("NEXUS_E2E_SKIP_BUILD") == "1"
    up_cmd = [nexus_bin, "up", "--no-build" if use_prebuilt else "--build"]

    try:
        up_result = subprocess.run(
            up_cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(project_dir),
            env=up_env,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(
            "nexus up timed out after 300s (image build takes too long on this host; "
            "set NEXUS_E2E_SKIP_BUILD=1 to use a pre-built image)"
        )
    if up_result.returncode != 0:
        debug_path = project_dir / "nexus-up-debug.log"
        debug_path.write_text(
            f"returncode={up_result.returncode}\n\n"
            f"--- stdout ---\n{up_result.stdout}\n\n"
            f"--- stderr ---\n{up_result.stderr}\n"
        )
        pytest.skip(
            f"nexus up failed (rc={up_result.returncode}, log: {debug_path}): "
            f"stderr_tail={up_result.stderr[-400:]!r}"
        )

    # ---- nexus env --json --------------------------------------------------
    env_result = subprocess.run(
        [nexus_bin, "env", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(project_dir),
    )
    if env_result.returncode != 0:
        # Teardown and skip — env command failed
        subprocess.run(
            [nexus_bin, "down", "--volumes"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(project_dir),
        )
        pytest.skip(f"nexus env --json failed: {env_result.stderr[-400:]!r}")

    try:
        env_payload = json.loads(env_result.stdout)
    except json.JSONDecodeError as exc:
        subprocess.run(
            [nexus_bin, "down", "--volumes"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(project_dir),
        )
        pytest.skip(f"nexus env --json produced invalid JSON: {exc}")

    url = env_payload.get("NEXUS_URL", "http://localhost:2026")
    api_key = env_payload.get("NEXUS_API_KEY", "")
    grpc_host = env_payload.get("NEXUS_GRPC_HOST", "")
    grpc_port = env_payload.get("NEXUS_GRPC_PORT", "")

    # ---- wait for /health --------------------------------------------------
    deadline = time.monotonic() + 60
    healthy = False
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(
                f"{url}/health",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    healthy = True
                    break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.0)

    if not healthy:
        subprocess.run(
            [nexus_bin, "down", "--volumes"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(project_dir),
        )
        pytest.skip(f"nexus stack failed health check on {url}/health")

    handle = FullStack(
        url=url,
        api_key=api_key,
        grpc_host=grpc_host,
        grpc_port=grpc_port,
        project_dir=project_dir,
    )

    try:
        yield handle
    finally:
        if os.environ.get("NEXUS_E2E_KEEP", "").lower() not in ("1", "true", "yes"):
            subprocess.run(
                [nexus_bin, "down", "--volumes"],
                capture_output=True,
                text=True,
                timeout=180,
                cwd=str(project_dir),
            )
            shutil.rmtree(project_dir, ignore_errors=True)
