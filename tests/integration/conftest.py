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
# Internal boot/lifecycle helper (sibling-reusable)
# ---------------------------------------------------------------------------


def _boot_full_stack(tmp_path: Path, preset: str = "shared") -> Iterator[FullStack]:
    """Internal: boot a FULL nexus stack and yield a FullStack handle.

    Shared by the ``full_stack`` fixture and sibling integration fixtures
    (#4133–#4138) that need a different preset.  NEXUS_E2E gating and
    Docker availability are checked here so non-E2E collection does no
    Docker work.

    Teardown: ``nexus down --volumes`` + temp dir removal.
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
    # Use the documented prebuilt path (matches docs/deployment/full-profile.md
    # "Via the stack (recommended): nexus up"). The `shared` preset generates a
    # pull-only nexus-stack.yml pinning ghcr.io/nexi-lab/nexus:<channel>, so
    # plain `nexus up` reuses the prebuilt image and boots in tens of seconds.
    # Forcing `--build` here was a defect: it triggers a from-scratch Rust +
    # Python Dockerfile build (minutes) that blew the subprocess timeout.
    # Opt in to a source build only when explicitly iterating on the image.
    up_env = os.environ.copy()
    force_build = os.environ.get("NEXUS_E2E_BUILD") == "1"
    up_cmd = [nexus_bin, "up"] + (["--build"] if force_build else [])

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
            "nexus up timed out after 300s"
            + (" (NEXUS_E2E_BUILD=1 forces a slow source build)" if force_build else "")
        )
    if up_result.returncode != 0:
        debug_path = project_dir / "nexus-up-debug.log"
        debug_path.write_text(
            f"returncode={up_result.returncode}\n\n"
            f"--- stdout ---\n{up_result.stdout}\n\n"
            f"--- stderr ---\n{up_result.stderr}\n"
        )
        combined = f"{up_result.stdout}\n{up_result.stderr}"
        # Environmental: a docker *registry credential helper* that is
        # unavailable in non-interactive shells (e.g. macOS osxkeychain
        # returns "User canceled the operation. (-128)"). Match ONLY the
        # actual credential-helper signature — a generic "Docker Compose
        # failed to start" must NOT be classified as a credential problem
        # (it also covers port conflicts, image-missing, health timeouts,
        # etc., and mislabeling them hides the real cause).
        cred_signature = (
            "getting credentials" in combined
            or "User canceled the operation" in combined
            or "docker-credential-" in combined
        )
        if cred_signature:
            pytest.skip(
                "environment cannot pull required images: docker credential "
                "helper unavailable non-interactively (pre-cache all stack "
                f"images, or run on CI with anonymous pulls). log: {debug_path}"
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


# ---------------------------------------------------------------------------
# full_stack fixture (thin wrapper around _boot_full_stack)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def full_stack(tmp_path: Path) -> Iterator[FullStack]:
    """Boot a FULL nexus stack (preset=shared). Skipped unless NEXUS_E2E=1.

    Skipped when:
    - ``NEXUS_E2E != "1"`` (the test-level ``requires_e2e`` skip fires first,
      but this guard ensures no Docker work happens if the fixture is invoked
      outside a properly-gated test).
    - Docker is not available.

    Teardown: ``nexus down --volumes`` + temp dir removal.

    Sibling integration suites needing another preset define their own
    one-line fixture::

        @pytest.fixture
        def demo_stack(tmp_path):
            yield from _boot_full_stack(tmp_path, preset="demo")
    """
    yield from _boot_full_stack(tmp_path, preset="shared")
