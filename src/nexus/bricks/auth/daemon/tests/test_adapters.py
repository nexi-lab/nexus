"""Tests for nexus.bricks.auth.daemon.adapters."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from nexus.bricks.auth.daemon.adapters import (
    DEFAULT_SUBPROCESS_SOURCES,
    GCLOUD_SOURCE,
    GH_SOURCE,
    GWS_SOURCE,
    SubprocessSource,
)


def test_fetch_returns_stdout_bytes_on_success() -> None:
    src = SubprocessSource(name="fake", cmd=("sh", "-c", "printf hello"))
    # sh resolves on PATH on macOS/Linux. If not, skip cleanly.
    if not src.available():
        pytest.skip("sh not on PATH")
    assert src.fetch() == b"hello"


def test_fetch_returns_none_when_binary_missing() -> None:
    src = SubprocessSource(name="nope", cmd=("definitely-not-a-real-binary-xyzzy",))
    assert not src.available()
    assert src.fetch() is None


def test_fetch_returns_none_on_nonzero_exit() -> None:
    src = SubprocessSource(name="fail", cmd=("sh", "-c", "exit 1"))
    if not src.available():
        pytest.skip("sh not on PATH")
    assert src.fetch() is None


def test_fetch_returns_none_on_empty_stdout() -> None:
    src = SubprocessSource(name="empty", cmd=("sh", "-c", "printf ''"))
    if not src.available():
        pytest.skip("sh not on PATH")
    assert src.fetch() is None


def test_fetch_returns_none_on_timeout() -> None:
    """Timeout is classified as a transient failure, not a crash."""
    src = SubprocessSource(name="slow", cmd=("sh", "-c", "sleep 5"), timeout_s=0.05)
    if not src.available():
        pytest.skip("sh not on PATH")
    # Patch subprocess.run to raise TimeoutExpired regardless of system.
    with patch(
        "nexus.bricks.auth.daemon.adapters.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=list(src.cmd), timeout=0.05),
    ):
        assert src.fetch() is None


def test_default_registry_has_three_sources() -> None:
    names = {s.name for s in DEFAULT_SUBPROCESS_SOURCES}
    assert names == {"gcloud", "gh", "gws"}


def test_default_commands_match_expected_clis() -> None:
    assert GCLOUD_SOURCE.cmd[0] == "gcloud"
    assert GH_SOURCE.cmd[0] == "gh"
    assert GWS_SOURCE.cmd[0] == "gws"
    assert "print-access-token" in GCLOUD_SOURCE.cmd
    assert "token" in GH_SOURCE.cmd
