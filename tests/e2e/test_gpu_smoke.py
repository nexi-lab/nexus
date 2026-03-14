"""GPU extension smoke suite (Issue #2961, Section G.10).

Validates the CUDA image variant init path. Does NOT require a GPU
runner — it only tests that the CLI correctly generates CUDA image
references. Actual GPU inference testing requires dedicated runners.

Gated behind ``e2e`` and ``gpu`` markers. Skipped by default.

Usage:
    pytest tests/e2e/test_gpu_smoke.py -m "e2e and gpu" -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.gpu,
]


def _docker_available() -> bool:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(autouse=True)
def _skip_without_docker() -> None:
    if not _docker_available():
        pytest.skip("Docker is not available")


class TestGpuImageVariant:
    """Test that --accelerator cuda produces correct config."""

    @pytest.fixture()
    def project_dir(self, tmp_path: Path) -> Path:
        return tmp_path

    def test_cuda_init_produces_cuda_image_ref(self, project_dir: Path) -> None:
        """nexus init --preset shared --accelerator cuda pins a -cuda image."""
        config_path = project_dir / "nexus.yaml"
        data_dir = project_dir / "nexus-data"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--accelerator",
                "cuda",
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

        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        assert cfg["image_accelerator"] == "cuda"
        assert "-cuda" in cfg["image_ref"], (
            f"Expected -cuda suffix in image_ref, got: {cfg['image_ref']}"
        )

    def test_cuda_with_explicit_tag(self, project_dir: Path) -> None:
        """nexus init --accelerator cuda --image-tag 0.9.2 produces 0.9.2-cuda."""
        config_path = project_dir / "nexus.yaml"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--accelerator",
                "cuda",
                "--image-tag",
                "0.9.2",
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
        assert cfg["image_ref"] == "ghcr.io/nexi-lab/nexus:0.9.2-cuda"

    def test_cuda_with_edge_channel(self, project_dir: Path) -> None:
        """nexus init --accelerator cuda --channel edge produces edge-cuda."""
        config_path = project_dir / "nexus.yaml"

        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--accelerator",
                "cuda",
                "--channel",
                "edge",
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
        assert cfg["image_ref"] == "ghcr.io/nexi-lab/nexus:edge-cuda"
