"""Public TUI commands select the installed checkout and its runtime settings."""

import importlib
from pathlib import Path

import pytest
from click.testing import CliRunner

cli = importlib.import_module("nexus.cli.main")


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
            ["bun", "--cwd", str(package), "run", "start", *forwarded],
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
