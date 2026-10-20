"""Real subprocess checks for server source isolation in the Zone journeys."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.e2e.conftest import SOURCE_ROOT, server_command


def _run_help(source: Path, *, optimized: bool = False) -> subprocess.CompletedProcess[str]:
    keep = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env.update(PYTHONPATH=str(source), PYTHONIOENCODING="utf8", PYTHONUTF8="1")
    if optimized:
        env["PYTHONOPTIMIZE"] = "1"
    return subprocess.run(
        server_command("--help"),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf8",
        timeout=30,
    )


def test_server_command_imports_the_current_checkout() -> None:
    result = _run_help(SOURCE_ROOT)
    assert result.returncode == 0, result.stderr
    assert "--data-dir" in result.stdout


@pytest.mark.parametrize("optimized", [False, True])
def test_server_command_refuses_another_checkout_before_importing_its_daemon(
    tmp_path, optimized
) -> None:
    package = tmp_path / "nexus"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf8")
    result = _run_help(tmp_path, optimized=optimized)
    assert result.returncode != 0
    assert "server imported a different Nexus checkout" in result.stderr
