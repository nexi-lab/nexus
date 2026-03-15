"""Federation / Raft extension smoke suite (Issue #2961, Section G.11).

Validates that federation is an explicit extension layered on top of the
shared-node story, not a first-run preset. Tests the CLI surface for
federation commands and the init → federation enable flow shape.

Gated behind ``e2e`` and ``federation`` markers.

Usage:
    pytest tests/e2e/test_federation_smoke.py -m "e2e and federation" -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.federation,
]


class TestFederationIsNotFirstRunPreset:
    """Federation should be an extension, not a first-run preset."""

    def test_federation_not_in_valid_presets(self) -> None:
        """Verify 'federated' is not a valid init preset."""
        from nexus.cli.commands.init_cmd import VALID_PRESETS

        assert "federated" not in VALID_PRESETS
        assert "federation" not in VALID_PRESETS

    def test_init_rejects_federated_preset(self, tmp_path: Path) -> None:
        """nexus init --preset federated should fail."""
        result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "federated",
                "--config-path",
                str(tmp_path / "nexus.yaml"),
                "--data-dir",
                str(tmp_path / "data"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0


class TestFederationCliSurface:
    """Test that federation CLI commands exist and respond gracefully."""

    def test_federation_help(self) -> None:
        """nexus federation --help should work."""
        result = subprocess.run(
            ["nexus", "federation", "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert "federation" in result.stdout.lower()

    def test_federation_status_without_stack(self, tmp_path: Path) -> None:
        """nexus federation status should fail gracefully without a stack."""
        result = subprocess.run(
            ["nexus", "federation", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(tmp_path),
        )
        # Should fail gracefully, not crash — various exit codes acceptable
        assert "Traceback" not in result.stderr
        assert result.stdout or result.stderr, "Should print some error message"

    def test_federation_enable_help(self) -> None:
        """nexus federation enable --help should document the extension flow."""
        result = subprocess.run(
            ["nexus", "federation", "enable", "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # Enable subcommand may or may not exist yet — test graceful behavior
        assert "Traceback" not in result.stderr


class TestSharedToFederationFlow:
    """Test the intended flow: shared preset → federation enable."""

    def test_shared_init_then_federation_help(self, tmp_path: Path) -> None:
        """Verify the intended user flow shape exists."""
        config_path = tmp_path / "nexus.yaml"
        data_dir = tmp_path / "nexus-data"

        # Step 1: nexus init --preset shared
        init_result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert init_result.returncode == 0

        # Verify config is shared, not federated
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "shared"

        # Step 2: federation should be available as extension command
        fed_result = subprocess.run(
            ["nexus", "federation", "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert fed_result.returncode == 0

    def test_shared_config_has_no_federation_preset(self, tmp_path: Path) -> None:
        """Shared config should not include federation topology by default."""
        config_path = tmp_path / "nexus.yaml"
        init_result = subprocess.run(
            [
                "nexus",
                "init",
                "--preset",
                "shared",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(tmp_path / "data"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert init_result.returncode == 0

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "shared"
        assert "federation" not in cfg
        assert "topology" not in cfg
