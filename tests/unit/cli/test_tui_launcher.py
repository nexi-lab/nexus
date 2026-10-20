"""Public TUI commands select the installed checkout and its runtime settings."""

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

cli = importlib.import_module("nexus.cli.main")
repo = Path(cli.__file__).resolve().parents[3]


def workspace(root: Path, scope: str = "@nexus-ai-fs") -> Path:
    for relative in (
        "packages/nexus-tui/src/index.tsx",
        "packages/nexus-api-client/dist/index.js",
        f"packages/nexus-tui/node_modules/{scope}/api-client/package.json",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    return root / "packages/nexus-tui"


@pytest.mark.parametrize("nested", [False, True])
def test_public_commands_use_checkout_runtime_and_preserve_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    package = workspace(tmp_path)
    monkeypatch.chdir(package / "src" if nested else tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/tools/{name}")
    calls = []

    def execute(binary, arguments):
        calls.append((binary, arguments))
        raise SystemExit(23)

    monkeypatch.setattr(cli.os, "execvp", execute)
    connection = [
        "--url",
        "http://native:2028",
        "--search-url",
        "http://search-owner:2028",
        "--api-key",
        "explicit-native-test-key",
        "--zone-id",
        "sharedzone",
    ]
    runner = CliRunner()
    for command, forwarded in (([], []), (["tui", *connection], connection)):
        result = runner.invoke(cli.main, command)
        assert result.exit_code == 23, result.output
        assert calls[-1] == (
            "/tools/bun",
            ["bun", "run", "--cwd", str(package), "start", *forwarded],
        )


def test_checkout_without_current_dependency_uses_installed_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace(tmp_path, "@nexus")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/tools/{name}")
    calls = []

    def execute(binary, arguments):
        calls.append((binary, arguments))
        raise SystemExit(0)

    monkeypatch.setattr(cli.os, "execvp", execute)
    result = CliRunner().invoke(cli.main, ["tui", "--zone-id", "root"])
    assert result.exit_code == 0, result.output
    assert calls == [("/tools/nexus-tui", ["nexus-tui", "--zone-id", "root"])]


@pytest.mark.skipif(
    not shutil.which("bun")
    or not (repo / "packages/nexus-api-client/dist/index.js").is_file()
    or not (
        repo / "packages/nexus-tui/node_modules/@nexus-ai-fs/api-client/package.json"
    ).is_file(),
    reason="Requires Bun and the installed local terminal workspace",
)
@pytest.mark.parametrize("nested", [False, True])
def test_actual_bun_launcher_reaches_terminal_entrypoint(nested: bool) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from nexus.cli.main import _exec_tui; _exec_tui(['--help'])"],
        cwd=repo / "packages/nexus-tui/src" if nested else repo,
        env=os.environ | {"PYTHONPATH": str(repo / "src"), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "nexus-tui" in result.stdout and "Terminal UI for Nexus" in result.stdout
    assert "--search-url" in result.stdout
    assert "package.json scripts" not in result.stdout
