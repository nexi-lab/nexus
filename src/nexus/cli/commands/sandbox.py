"""Sandbox management commands for code execution (Issue #372).

Provides CLI commands for creating, managing, and executing code in sandboxes.
Supports E2B and other sandbox providers.
"""

from __future__ import annotations

import json
import sys

import click

from nexus import NexusFilesystem
from nexus.cli.utils import get_default_filesystem


@click.group(name="sandbox")
def sandbox() -> None:
    """Manage code execution sandboxes.

    Create, run code in, pause, resume, stop, and list sandboxes for safe code execution.
    """
    pass


@sandbox.command(name="create")
@click.argument("name")
@click.option(
    "--ttl",
    "-t",
    type=int,
    default=10,
    help="Idle timeout in minutes (default: 10)",
)
@click.option(
    "--template",
    help="Provider template ID (e.g., E2B template)",
)
@click.option(
    "--json",
    "-j",
    "json_output",
    is_flag=True,
    help="Output as JSON",
)
@click.option(
    "--data-dir",
    envvar="NEXUS_DATA_DIR",
    help="Nexus data directory",
)
def create_sandbox(
    name: str,
    ttl: int,
    template: str | None,
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Create a new sandbox for code execution.

    \b
    Examples:
        nexus sandbox create my-sandbox
        nexus sandbox create data-analysis --ttl 30
        nexus sandbox create ml-training --template custom-gpu-template
        nexus sandbox create test-sandbox --json
    """
    try:
        nx: NexusFilesystem = get_default_filesystem()

        result = nx.sandbox_create(
            name=name,
            ttl_minutes=ttl,
            template_id=template,
        )

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"✓ Created sandbox: {result['sandbox_id']}")
            click.echo(f"  Name: {result['name']}")
            click.echo(f"  Status: {result['status']}")
            click.echo(f"  TTL: {result['ttl_minutes']} minutes")
            click.echo(f"  Expires: {result['expires_at']}")

    except Exception as e:
        click.echo(f"Failed to create sandbox: {e}")
        sys.exit(1)


@sandbox.command(name="run")
@click.argument("sandbox_id")
@click.option(
    "--language",
    "-l",
    default="python",
    type=click.Choice(["python", "javascript", "bash"], case_sensitive=False),
    help="Programming language (default: python)",
)
@click.option(
    "--code",
    "-c",
    help="Code to execute (use - to read from stdin)",
)
@click.option(
    "--file",
    "-f",
    type=click.Path(exists=True),
    help="File containing code to execute",
)
@click.option(
    "--timeout",
    type=int,
    default=30,
    help="Execution timeout in seconds (default: 30)",
)
@click.option(
    "--json",
    "-j",
    "json_output",
    is_flag=True,
    help="Output as JSON",
)
@click.option(
    "--data-dir",
    envvar="NEXUS_DATA_DIR",
    help="Nexus data directory",
)
def run_code(
    sandbox_id: str,
    language: str,
    code: str | None,
    file: str | None,
    timeout: int,
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Run code in a sandbox.

    \b
    Examples:
        # Run Python code
        nexus sandbox run sb_123 -c "print('Hello')"

        # Run from file
        nexus sandbox run sb_123 -f script.py

        # Run from stdin
        echo "console.log('test')" | nexus sandbox run sb_123 -l javascript -c -

        # Run bash
        nexus sandbox run sb_123 -l bash -c "ls -la"

        # JSON output
        nexus sandbox run sb_123 -c "print('test')" --json
    """
    try:
        # Get code from argument, file, or stdin
        if code == "-":
            code_to_run = sys.stdin.read()
        elif code:
            code_to_run = code
        elif file:
            with open(file) as f:
                code_to_run = f.read()
        else:
            click.echo("Error: Must provide --code/-c or --file/-f")
            sys.exit(1)

        nx: NexusFilesystem = get_default_filesystem()

        result = nx.sandbox_run(
            sandbox_id=sandbox_id,
            language=language,
            code=code_to_run,
            timeout=timeout,
        )

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            # Display output
            if result["stdout"]:
                click.echo("=== STDOUT ===")
                click.echo(result["stdout"])

            if result["stderr"]:
                click.echo("=== STDERR ===", err=True)
                click.echo(result["stderr"], err=True)

            exit_code = result["exit_code"]
            execution_time = result["execution_time"]

            if exit_code == 0:
                click.echo(f"✓ Execution completed in {execution_time:.2f}s")
            else:
                click.echo(f"✗ Execution failed with exit code {exit_code} ({execution_time:.2f}s)")
                sys.exit(exit_code)

    except Exception as e:
        click.echo(f"Failed to run code: {e}")
        sys.exit(1)


@sandbox.command(name="pause")
@click.argument("sandbox_id")
@click.option(
    "--json",
    "-j",
    "json_output",
    is_flag=True,
    help="Output as JSON",
)
@click.option(
    "--data-dir",
    envvar="NEXUS_DATA_DIR",
    help="Nexus data directory",
)
def pause_sandbox(
    sandbox_id: str,
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Pause a sandbox to save costs.

    Paused sandboxes preserve state but don't consume resources.

    \b
    Examples:
        nexus sandbox pause sb_123
        nexus sandbox pause sb_123 --json
    """
    try:
        nx: NexusFilesystem = get_default_filesystem()
        result = nx.sandbox_pause(sandbox_id=sandbox_id)

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"✓ Paused sandbox: {sandbox_id}")
            click.echo(f"  Status: {result['status']}")
            click.echo(f"  Paused at: {result['paused_at']}")

    except Exception as e:
        click.echo(f"Failed to pause sandbox: {e}")
        sys.exit(1)


@sandbox.command(name="resume")
@click.argument("sandbox_id")
@click.option(
    "--json",
    "-j",
    "json_output",
    is_flag=True,
    help="Output as JSON",
)
@click.option(
    "--data-dir",
    envvar="NEXUS_DATA_DIR",
    help="Nexus data directory",
)
def resume_sandbox(
    sandbox_id: str,
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Resume a paused sandbox.

    \b
    Examples:
        nexus sandbox resume sb_123
        nexus sandbox resume sb_123 --json
    """
    try:
        nx: NexusFilesystem = get_default_filesystem()
        result = nx.sandbox_resume(sandbox_id=sandbox_id)

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"✓ Resumed sandbox: {sandbox_id}")
            click.echo(f"  Status: {result['status']}")
            click.echo(f"  Expires: {result['expires_at']}")

    except Exception as e:
        click.echo(f"Failed to resume sandbox: {e}")
        sys.exit(1)


@sandbox.command(name="stop")
@click.argument("sandbox_id")
@click.option(
    "--json",
    "-j",
    "json_output",
    is_flag=True,
    help="Output as JSON",
)
@click.option(
    "--data-dir",
    envvar="NEXUS_DATA_DIR",
    help="Nexus data directory",
)
def stop_sandbox(
    sandbox_id: str,
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Stop and destroy a sandbox.

    This permanently destroys the sandbox and all its data.

    \b
    Examples:
        nexus sandbox stop sb_123
        nexus sandbox stop sb_123 --json
    """
    try:
        nx: NexusFilesystem = get_default_filesystem()
        result = nx.sandbox_stop(sandbox_id=sandbox_id)

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"✓ Stopped sandbox: {sandbox_id}")
            click.echo(f"  Status: {result['status']}")
            click.echo(f"  Stopped at: {result['stopped_at']}")

    except Exception as e:
        click.echo(f"Failed to stop sandbox: {e}")
        sys.exit(1)


@sandbox.command(name="list")
@click.option(
    "--json",
    "-j",
    "json_output",
    is_flag=True,
    help="Output as JSON",
)
@click.option(
    "--data-dir",
    envvar="NEXUS_DATA_DIR",
    help="Nexus data directory",
)
def list_sandboxes(
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """List all sandboxes.

    \b
    Examples:
        nexus sandbox list
        nexus sandbox list --json
    """
    try:
        nx: NexusFilesystem = get_default_filesystem()
        result = nx.sandbox_list()
        sandboxes = result["sandboxes"]

        if json_output:
            click.echo(json.dumps(sandboxes, indent=2))
        else:
            if not sandboxes:
                click.echo("No sandboxes found.")
                return

            # Display as table
            click.echo(f"{'NAME':<20} {'SANDBOX ID':<20} {'STATUS':<12} {'CREATED'}")
            click.echo("-" * 80)
            for sb in sandboxes:
                name = sb["name"][:19]
                sandbox_id = sb["sandbox_id"][:19]
                status = sb["status"]
                created = sb["created_at"][:19]
                click.echo(f"{name:<20} {sandbox_id:<20} {status:<12} {created}")

            click.echo(f"\nTotal: {len(sandboxes)} sandbox(es)")

    except Exception as e:
        click.echo(f"Failed to list sandboxes: {e}")
        sys.exit(1)


@sandbox.command(name="status")
@click.argument("sandbox_id")
@click.option(
    "--json",
    "-j",
    "json_output",
    is_flag=True,
    help="Output as JSON",
)
@click.option(
    "--data-dir",
    envvar="NEXUS_DATA_DIR",
    help="Nexus data directory",
)
def sandbox_status(
    sandbox_id: str,
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Get sandbox status and details.

    \b
    Examples:
        nexus sandbox status sb_123
        nexus sandbox status sb_123 --json
    """
    try:
        nx: NexusFilesystem = get_default_filesystem()
        result = nx.sandbox_status(sandbox_id=sandbox_id)

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"Sandbox: {result['sandbox_id']}")
            click.echo(f"  Name: {result['name']}")
            click.echo(f"  Status: {result['status']}")
            click.echo(f"  Provider: {result['provider']}")
            click.echo(f"  User: {result['user_id']}")
            click.echo(f"  Created: {result['created_at']}")
            click.echo(f"  Last Active: {result['last_active_at']}")
            click.echo(f"  TTL: {result['ttl_minutes']} minutes")
            click.echo(f"  Expires: {result.get('expires_at', 'N/A')}")
            click.echo(f"  Uptime: {result['uptime_seconds']:.0f} seconds")

    except Exception as e:
        click.echo(f"Failed to get sandbox status: {e}")
        sys.exit(1)


def register_commands(cli: click.Group) -> None:
    """Register sandbox commands with the main CLI.

    Args:
        cli: The main Click group
    """
    cli.add_command(sandbox)
