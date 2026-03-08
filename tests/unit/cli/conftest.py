"""Shared test fixtures for CLI tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli.config import NexusCliConfig, ProfileEntry


@pytest.fixture()
def cli_runner() -> CliRunner:
    """Click CLI test runner with isolated filesystem."""
    return CliRunner()


@pytest.fixture()
def tmp_config_dir(tmp_path: Path) -> Path:
    """Temporary ~/.nexus/ directory."""
    config_dir = tmp_path / ".nexus"
    config_dir.mkdir()
    return config_dir


@pytest.fixture()
def tmp_config_file(tmp_config_dir: Path) -> Path:
    """Path to temporary config.yaml (not yet created)."""
    return tmp_config_dir / "config.yaml"


@pytest.fixture()
def sample_config() -> NexusCliConfig:
    """Sample config with two profiles."""
    return NexusCliConfig(
        current_profile="local",
        profiles={
            "local": ProfileEntry(
                url="http://localhost:2026",
                api_key="nx_test_local_dev",
                zone_id="default",
            ),
            "production": ProfileEntry(
                url="https://nexus.prod.example.com",
                api_key="nx_live_prod_abc123",
                zone_id="us-west-1",
            ),
        },
        settings={
            "output": {"format": "table", "color": True},
            "timing": {"enabled": False},
        },
    )


def write_config(path: Path, config: NexusCliConfig) -> None:
    """Write a NexusCliConfig to a YAML file."""
    with open(path, "w") as f:
        yaml.dump(config.to_dict(), f, default_flow_style=False, sort_keys=False)


@pytest.fixture()
def mock_connect() -> MagicMock:
    """Mock for nexus.connect() that captures config dicts."""
    mock = MagicMock()
    mock.return_value = MagicMock(spec=["close", "sys_read", "sys_write"])
    return mock


def make_config(
    *,
    current_profile: str | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
) -> NexusCliConfig:
    """Factory for creating NexusCliConfig in tests."""
    parsed_profiles: dict[str, ProfileEntry] = {}
    if profiles:
        for name, data in profiles.items():
            parsed_profiles[name] = ProfileEntry(
                url=data.get("url"),
                api_key=data.get("api_key") or data.get("api-key"),
                zone_id=data.get("zone_id") or data.get("zone-id"),
            )
    return NexusCliConfig(
        current_profile=current_profile,
        profiles=parsed_profiles,
        settings=settings or {},
    )


# ---------------------------------------------------------------------------
# Infrastructure / Compose fixtures (from develop)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_compose_runner() -> MagicMock:
    """Pre-configured mock for ComposeRunner."""
    runner = MagicMock()
    runner.compose_file = "/fake/docker-compose.yml"
    runner.project_dir = "/fake"
    runner.run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
    runner.run_attached.return_value = 0
    runner.ps.return_value = []
    return runner


@pytest.fixture()
def patch_compose_runner(mock_compose_runner: MagicMock):
    """Patch ComposeRunner construction to return the mock."""
    with patch("nexus.cli.commands.infra.ComposeRunner", return_value=mock_compose_runner):
        yield mock_compose_runner


@pytest.fixture()
def patch_compose_runner_status(mock_compose_runner: MagicMock):
    """Patch ComposeRunner in the status module."""
    with patch("nexus.cli.commands.status.ComposeRunner", return_value=mock_compose_runner):
        yield mock_compose_runner
