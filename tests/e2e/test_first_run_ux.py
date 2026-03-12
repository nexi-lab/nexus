"""End-to-end test for the first-run UX workflow (Issue #2915).

Validates the full journey:
    nexus init --preset demo → nexus up → nexus demo init → verify → nexus down

This test requires Docker and is gated behind the ``e2e`` and ``docker``
pytest markers.  It is skipped by default and only runs in CI or when
explicitly requested with ``pytest -m e2e``.

Timeout: 5 minutes (containers need startup time).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
import yaml

# Gate behind markers
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
]


def _docker_available() -> bool:
    """Check if Docker daemon is running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(autouse=True)
def _skip_without_docker() -> None:
    if not _docker_available():
        pytest.skip("Docker is not available")


class TestFirstRunInit:
    """Test nexus init for various presets."""

    @pytest.fixture()
    def project_dir(self, tmp_path: Path) -> Path:
        """Create a temporary project directory."""
        return tmp_path

    def test_init_creates_config(self, project_dir: Path) -> None:
        """nexus init --preset demo writes nexus.yaml and data dirs."""
        config_path = project_dir / "nexus.yaml"
        data_dir = project_dir / "nexus-data"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "demo",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"init failed: {result.stderr}"
        assert config_path.exists(), "nexus.yaml not created"
        assert data_dir.exists(), "data directory not created"

        # Verify config structure
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "demo"
        assert cfg["auth"] == "database"
        assert "postgres" in cfg["services"]
        assert "dragonfly" in cfg["services"]
        assert "zoekt" in cfg["services"]
        assert cfg["ports"]["http"] == 2026

    def test_init_shared_with_tls(self, project_dir: Path) -> None:
        """nexus init --preset shared --tls enables TLS in config."""
        config_path = project_dir / "nexus.yaml"
        data_dir = project_dir / "nexus-data"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--tls",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["tls"] is True
        assert "tls_dir" in cfg

    def test_init_with_addons(self, project_dir: Path) -> None:
        """nexus init --preset shared --with nats includes add-on."""
        config_path = project_dir / "nexus.yaml"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--with",
                "nats",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(project_dir / "data"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert "nats" in cfg.get("addons", [])

    def test_init_portable_outside_repo_root(self, project_dir: Path) -> None:
        """nexus init --preset demo succeeds in a clean temp dir.

        The bundled nexus-stack.yml should be copied to the project
        directory when no local compose file is found.
        """
        config_path = project_dir / "nexus.yaml"
        data_dir = project_dir / "nexus-data"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "demo",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            # Run from project_dir, NOT the repo root
            cwd=str(project_dir),
        )

        assert result.returncode == 0, (
            f"init failed outside repo root: {result.stderr}\n{result.stdout}"
        )
        assert config_path.exists()

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        # compose_file should be set and the file should exist
        compose_file = cfg.get("compose_file", "")
        assert compose_file, "compose_file not set in config"
        assert Path(compose_file).exists(), f"compose file not found: {compose_file}"


class TestFullWorkflow:
    """Full init → up → demo init → verify → down cycle.

    These tests exercise the complete first-run UX and require Docker
    to build/pull images and start containers.
    """

    @pytest.fixture()
    def project_dir(self, tmp_path: Path) -> Path:
        return tmp_path

    @pytest.fixture()
    def initialized_project(self, project_dir: Path) -> Path:
        """Run nexus init --preset demo and return the project dir."""
        config_path = project_dir / "nexus.yaml"
        data_dir = project_dir / "nexus-data"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "demo",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"init failed: {result.stderr}"
        return project_dir

    def test_up_starts_services(self, initialized_project: Path) -> None:
        """nexus up should start Docker Compose services."""
        config_path = initialized_project / "nexus.yaml"

        # Verify compose file exists before starting
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        compose_file = cfg.get("compose_file", "")
        assert Path(compose_file).exists(), f"compose file missing: {compose_file}"

        # Run nexus up (with timeout — containers take a while)
        result = subprocess.run(
            ["nexus", "up"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(initialized_project),
        )

        try:
            assert result.returncode == 0, (
                f"nexus up failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )

            # Re-read config — nexus up may have resolved port conflicts
            # and persisted new ports back to nexus.yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f)

            # Verify health endpoint is reachable
            import urllib.request

            health_port = cfg.get("ports", {}).get("http", 2026)
            resp = urllib.request.urlopen(f"http://localhost:{health_port}/health", timeout=10)
            assert resp.status == 200, f"health check returned {resp.status}"

        finally:
            # Always clean up — run nexus down
            down_result = subprocess.run(
                ["nexus", "down"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(initialized_project),
            )
            assert down_result.returncode == 0, f"nexus down failed: {down_result.stderr}"

    def test_full_init_up_demo_down(self, initialized_project: Path) -> None:
        """Complete first-run workflow: init → up → demo init → down.

        This is the golden path from issue #2915.
        """
        config_path = initialized_project / "nexus.yaml"

        # Step 1: nexus up
        up_result = subprocess.run(
            ["nexus", "up"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(initialized_project),
        )
        assert up_result.returncode == 0, (
            f"nexus up failed:\nstdout: {up_result.stdout}\nstderr: {up_result.stderr}"
        )

        # Re-read config after up — ports may have been reassigned
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        try:
            # Wait a moment for services to stabilize
            time.sleep(2)

            # Step 2: nexus demo init
            demo_result = subprocess.run(
                ["nexus", "demo", "init"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(initialized_project),
            )
            assert demo_result.returncode == 0, (
                f"demo init failed: {demo_result.stderr}\n{demo_result.stdout}"
            )
            assert "Seeding" in demo_result.stdout or "Files" in demo_result.stdout

            # Verify manifest was created
            data_dir = cfg.get("data_dir", str(initialized_project / "nexus-data"))
            manifest_path = Path(data_dir) / ".demo-manifest.json"
            assert manifest_path.exists(), "demo manifest not created"

            # Step 3: nexus demo reset (verify cleanup works)
            reset_result = subprocess.run(
                ["nexus", "demo", "reset"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(initialized_project),
            )
            assert reset_result.returncode == 0, f"demo reset failed: {reset_result.stderr}"
            assert not manifest_path.exists(), "manifest should be removed after reset"

        finally:
            # Step 4: nexus down — must succeed and actually remove the stack
            down_result = subprocess.run(
                ["nexus", "down"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(initialized_project),
            )
            assert down_result.returncode == 0, f"nexus down failed: {down_result.stderr}"
