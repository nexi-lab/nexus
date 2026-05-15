"""``nexus env`` and ``nexus run`` — connection env var management.

``nexus env`` prints environment variables for the running Nexus stack
in various formats (shell export, .env, JSON).  ``nexus run`` wraps a
subprocess with those variables injected.

Both commands read from ``nexus.yaml`` (declarative config) overlaid with
``{data_dir}/.state.json`` (runtime state from the last ``nexus up``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click

from nexus.cli.state import (
    load_project_config,
    load_runtime_state,
    resolve_connection_env,
)
from nexus.cli.theme import console

# ---------------------------------------------------------------------------
# Shell formatting helpers
# ---------------------------------------------------------------------------


def _detect_shell() -> str:
    """Detect the user's shell from $SHELL."""
    shell_path = os.environ.get("SHELL", "/bin/bash")
    name = Path(shell_path).name
    if name in ("bash", "zsh", "fish", "sh"):
        return name
    return "bash"


def _shell_escape(value: str, shell: str) -> str:
    """Escape a value for safe embedding in shell syntax.

    For bash/zsh/sh: single-quote with internal single-quotes escaped as '\\''
    For fish: single-quote with internal single-quotes escaped as \\'
    For powershell: single-quote with internal single-quotes doubled
    """
    if shell == "powershell":
        return "'" + value.replace("'", "''") + "'"
    if shell == "fish":
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    # bash / zsh / sh: end quote, escaped literal quote, reopen quote
    return "'" + value.replace("'", "'\\''") + "'"


def _format_shell(env_vars: dict[str, str], shell: str) -> str:
    """Format env vars for the target shell."""
    lines: list[str] = []
    for key, value in sorted(env_vars.items()):
        escaped = _shell_escape(value, shell)
        if shell == "fish":
            lines.append(f"set -gx {key} {escaped};")
        elif shell == "powershell":
            lines.append(f"$env:{key} = {escaped}")
        else:
            # bash / zsh / sh
            lines.append(f"export {key}={escaped}")
    return "\n".join(lines)


def _format_dotenv(env_vars: dict[str, str]) -> str:
    """Format as .env file (KEY=VALUE with quoting for special chars)."""
    lines: list[str] = []
    for k, v in sorted(env_vars.items()):
        # Quote values that contain special chars (spaces, quotes, #, etc.)
        if any(c in v for c in ("'", '"', " ", "\n", "#", "=")):
            # Double-quote with internal double-quotes and backslashes escaped
            escaped = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            lines.append(f'{k}="{escaped}"')
        else:
            lines.append(f"{k}={v}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def register_commands(cli: click.Group) -> None:
    """Register env and run commands."""
    cli.add_command(env_cmd)
    cli.add_command(run)


@click.command(name="env")
@click.option(
    "--shell",
    "shell_name",
    type=click.Choice(["bash", "zsh", "fish", "powershell", "sh"]),
    default=None,
    help="Shell format (default: auto-detect from $SHELL).",
)
@click.option(
    "--dotenv",
    is_flag=True,
    default=False,
    help="Output in .env format (KEY=VALUE, no export).",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Output as JSON object.",
)
def env_cmd(
    shell_name: str | None,
    dotenv: bool,
    json_output: bool,
) -> None:
    """Print environment variables for the running Nexus stack.

    Reads nexus.yaml and .state.json to emit all connection variables.

    Usage patterns:

    \b
        eval $(nexus env)              # load into current shell
        nexus env --dotenv > .env      # write .env file
        nexus env --json               # machine-readable
        nexus env --shell fish | source

    Examples:

    \b
        nexus env
        nexus env --json
        nexus env --dotenv
        nexus env --shell fish
    """
    config = load_project_config()
    data_dir = config.get("data_dir", "./nexus-data")
    state = load_runtime_state(data_dir)
    env_vars = resolve_connection_env(config, state)

    if not env_vars:
        console.print(
            "[nexus.warning]No connection info found.[/nexus.warning] Run `nexus up` first."
        )
        raise SystemExit(1)

    if json_output:
        import json

        click.echo(json.dumps(env_vars, indent=2))
        return

    if dotenv:
        click.echo(_format_dotenv(env_vars))
        return

    effective_shell = shell_name or _detect_shell()
    click.echo(_format_shell(env_vars, effective_shell))


@click.command(name="run")
@click.argument("command", nargs=-1, required=True)
def run(command: tuple[str, ...]) -> None:
    """Run a command with Nexus environment variables injected.

    Spawns the given command as a subprocess with NEXUS_URL, NEXUS_API_KEY,
    and other connection variables set in the environment.  Stdin, stdout,
    and stderr are passed through — interactive commands (like ``bash``)
    work as expected.

    Examples:

    \b
        nexus run python my_agent.py
        nexus run pytest tests/
        nexus run bash
    """
    config = load_project_config()
    data_dir = config.get("data_dir", "./nexus-data")
    state = load_runtime_state(data_dir)
    env_vars = resolve_connection_env(config, state)

    run_env = {**os.environ, **env_vars}
    try:
        result = subprocess.run(list(command), env=run_env)
        sys.exit(result.returncode)
    except FileNotFoundError as err:
        console.print(f"[nexus.error]Error:[/nexus.error] Command not found: {command[0]}")
        raise SystemExit(127) from err
