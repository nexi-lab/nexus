"""CLI extractor: parse _REGISTER_COMMANDS dict from cli/commands/__init__.py."""

from pathlib import Path

from scripts.surface_coverage.extract_cli import extract_cli_commands


def test_extract_cli_from_fixture(tmp_path: Path):
    src = tmp_path / "src/nexus/cli/commands"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text(
        '"""CLI."""\n'
        "_REGISTER_COMMANDS = {\n"
        '    "file_ops": ("init", "cat", "write"),\n'
        '    "directory": ("ls", "mkdir"),\n'
        "}\n"
    )
    (src / "file_ops.py").write_text("# fake\n")
    (src / "directory.py").write_text("# fake\n")

    results = extract_cli_commands(src / "__init__.py")
    names = {r.name for r in results}
    assert names == {"nexus init", "nexus cat", "nexus write", "nexus ls", "nexus mkdir"}
    # source should point at the module file the command lives in
    by_name = {r.name: r for r in results}
    assert str(src / "file_ops.py") in by_name["nexus init"].source
    assert str(src / "directory.py") in by_name["nexus ls"].source


def test_extract_cli_real_file_smoke(repo_root: Path):
    real = repo_root / "src/nexus/cli/commands/__init__.py"
    if not real.exists():
        return
    results = extract_cli_commands(real)
    assert len(results) > 0
    assert all(r.name.startswith("nexus ") for r in results)
