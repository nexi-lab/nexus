"""CLI extractor: parse _REGISTER_COMMANDS dict from cli/commands/__init__.py."""

from pathlib import Path

from scripts.surface_coverage.extract_cli import extract_cli_commands


def test_registered_nested_click_commands_and_source_lines(tmp_path: Path):
    (tmp_path / "__init__.py").write_text("_REGISTER_COMMANDS = {'mcp': ('mcp',)}\n")
    module = tmp_path / "mcp.py"
    module.write_text(
        "import click\n"
        "@click.group()\ndef mcp(): pass\n"
        "@mcp.group(name='profile')\ndef profiles(): pass\n"
        "@profiles.command(name='assign')\ndef assign_profile(): pass\n"
        "@click.command()\ndef unregistered(): pass\n"
    )
    by_name = {r.name: r for r in extract_cli_commands(tmp_path / "__init__.py")}
    assert set(by_name) == {"nexus mcp", "nexus mcp profile", "nexus mcp profile assign"}
    assert by_name["nexus mcp profile assign"].source == f"{module}:6"


def test_registered_attribute_keeps_the_public_group_alias(tmp_path: Path):
    (tmp_path / "__init__.py").write_text(
        "_REGISTER_COMMANDS = {}\n_ADD_COMMAND = {'tools': ('tools', 'tool_group')}\n"
    )
    (tmp_path / "tools.py").write_text(
        "import click\n"
        "@click.group(name='internal')\ndef tool_group(): pass\n"
        "@tool_group.command(name='list')\ndef list_tools(): pass\n"
    )
    names = {r.name for r in extract_cli_commands(tmp_path / "__init__.py")}
    assert names == {"nexus tools", "nexus tools list"}


def test_later_registration_replaces_root_and_its_nested_commands(tmp_path: Path):
    (tmp_path / "__init__.py").write_text(
        "_REGISTER_COMMANDS = {'old': ('tools',)}\n_ADD_COMMAND = {'new': ('tools', 'tools')}\n"
    )
    (tmp_path / "old.py").write_text(
        "import click\n@click.group()\ndef tools(): pass\n@tools.command()\ndef obsolete(): pass\n"
    )
    module = tmp_path / "new.py"
    module.write_text(
        "import click\n@click.group()\ndef tools(): pass\n@tools.command()\ndef current(): pass\n"
    )
    routes = extract_cli_commands(tmp_path / "__init__.py")
    assert {r.name for r in routes} == {"nexus tools", "nexus tools current"}
    assert all(r.module_file == module for r in routes)
    assert extract_cli_commands(tmp_path / "__init__.py") == routes


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
