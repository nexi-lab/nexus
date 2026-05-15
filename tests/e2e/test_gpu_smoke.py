"""GPU extension smoke suite (Issue #2961, Section G.10).

Validates the CUDA image variant init path. Does NOT require a GPU
runner — it only tests that the CLI correctly generates CUDA image
references. These are CLI/config tests, not Docker tests.

Gated behind ``e2e`` and ``gpu`` markers.

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
        assert cfg.get("image_pin") == "tag", "Explicit tag should set image_pin"

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
        assert cfg.get("image_channel") == "edge"
        # Channel-following: should NOT have image_pin
        assert "image_pin" not in cfg

    def test_cuda_upgrade_validates_channel(self, project_dir: Path) -> None:
        """nexus upgrade --channel invalid should fail with clear error."""
        config_path = project_dir / "nexus.yaml"
        # First init
        subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(project_dir / "data"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        result = subprocess.run(
            ["nexus", "upgrade", "--channel", "stabel"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(project_dir),
        )
        assert result.returncode != 0
        assert "Unknown channel" in result.stdout or "stabel" in result.stdout
