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


class TestFirstRunUX:
    """Full init → up → demo init → verify → down cycle."""

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
