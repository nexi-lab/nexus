"""Shared fixtures for CLI command tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


@pytest.fixture()
def cli_runner() -> CliRunner:
    """Click CLI test runner with isolated filesystem."""
    return CliRunner()


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
