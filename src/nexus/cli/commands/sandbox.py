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
    "--provider",
    "-p",
    default="e2b",
    type=click.Choice(["e2b", "docker"], case_sensitive=False),
    help="Sandbox provider (default: e2b)",
)
@click.option(
    "--template",
    help="Provider template ID (e.g., E2B template or Docker image)",
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
    provider: str,
    template: str | None,
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Create a new sandbox for code execution.

    \b
    Examples:
        nexus sandbox create my-sandbox
        nexus sandbox create data-analysis --ttl 30 --provider docker
        nexus sandbox create ml-training --template custom-gpu-template
        nexus sandbox create test-sandbox --json
        nexus sandbox create docker-box --provider docker --template python:3.11-slim
    """
    try:
        nx: NexusFilesystem = get_default_filesystem()

        result = nx.sandbox_create(
            name=name,
            ttl_minutes=ttl,
            provider=provider,
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


@sandbox.command(name="get-or-create")
@click.argument("name")
@click.option(
    "--ttl",
    "-t",
    type=int,
    default=10,
    help="Idle timeout in minutes (default: 10)",
)
@click.option(
    "--provider",
    "-p",
    default="docker",
    type=click.Choice(["e2b", "docker"], case_sensitive=False),
    help="Sandbox provider (default: docker)",
)
@click.option(
    "--template",
    help="Provider template ID (e.g., E2B template or Docker image)",
)
@click.option(
    "--verify/--no-verify",
    default=True,
    help="Verify sandbox status with provider (default: verify)",
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
def get_or_create_sandbox(
    name: str,
    ttl: int,
    provider: str,
    template: str | None,
    verify: bool,
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Get existing sandbox or create new one (idempotent).

    This command handles the common pattern where you want to reuse an
    existing sandbox if it's still running, or create a new one if not.
    Perfect for agent workflows where each user+agent should have one
    persistent sandbox.

    The command will:
    1. Check if a sandbox with this name exists for current user
    2. Verify it's actually running (if --verify is enabled)
    3. Return existing sandbox if found and active
    4. Create new sandbox if none found or existing one is dead

    \b
    Examples:
        # Get or create sandbox (with verification)
        nexus sandbox get-or-create my-agent-sandbox

        # Agent workflow: user,agent naming
        nexus sandbox get-or-create alice,agent_ml

        # Without verification (faster but may be stale)
        nexus sandbox get-or-create my-sandbox --no-verify

        # With custom TTL and provider
        nexus sandbox get-or-create my-sandbox --ttl 30 --provider e2b

        # JSON output
        nexus sandbox get-or-create my-sandbox --json
    """
    try:
        nx: NexusFilesystem = get_default_filesystem()

        result = nx.sandbox_get_or_create(
            name=name,
            ttl_minutes=ttl,
            provider=provider,
            template_id=template,
            verify_status=verify,
        )

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            # Check if it was created or reused
            action = "Found and verified" if result.get("verified") else "Got"

            click.echo(f"✓ {action} sandbox: {result['sandbox_id']}")
            click.echo(f"  Name: {result['name']}")
            click.echo(f"  Status: {result['status']}")
            click.echo(f"  TTL: {result['ttl_minutes']} minutes")
            click.echo(f"  Expires: {result['expires_at']}")

            if verify:
                click.echo(f"  Verified: {result.get('verified', False)}")

    except Exception as e:
        click.echo(f"Failed to get or create sandbox: {e}")
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
    "--user-id",
    "-u",
    help="Filter by user ID",
)
@click.option(
    "--agent-id",
    "-a",
    help="Filter by agent ID",
)
@click.option(
    "--tenant-id",
    "-t",
    help="Filter by tenant ID",
)
@click.option(
    "--verify",
    "-v",
    is_flag=True,
    help="Verify status with provider (slower but accurate)",
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
def list_sandboxes(
    user_id: str | None,
    agent_id: str | None,
    tenant_id: str | None,
    verify: bool,
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """List sandboxes with optional filtering.

    By default, lists sandboxes for the current user. Use filter options
    to narrow results by user, agent, or tenant.

    The --verify flag checks actual status with Docker/E2B provider (slower
    but ensures accuracy). Without --verify, status comes from database cache
    which may be stale if sandboxes were killed externally.

    \b
    Examples:
        # List all sandboxes for current user
        nexus sandbox list

        # List sandboxes for specific user
        nexus sandbox list --user-id alice

        # List sandboxes for specific agent
        nexus sandbox list --agent-id agent_123

        # List sandboxes for specific tenant
        nexus sandbox list --tenant-id tenant_456

        # Combine filters (sandboxes for specific agent and tenant)
        nexus sandbox list --agent-id agent_123 --tenant-id tenant_456

        # Verify status with provider (slower but accurate)
        nexus sandbox list --verify

        # JSON output
        nexus sandbox list --json
    """
    try:
        nx: NexusFilesystem = get_default_filesystem()

        # Call with filter parameters (not context)
        result = nx.sandbox_list(
            user_id=user_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            verify_status=verify,
        )
        sandboxes = result["sandboxes"]

        if json_output:
            click.echo(json.dumps(sandboxes, indent=2))
        else:
            if not sandboxes:
                click.echo("No sandboxes found.")
                return

            # Display as table
            if verify:
                # Include verification column if --verify was used
                click.echo(
                    f"{'NAME':<20} {'SANDBOX ID':<20} {'STATUS':<12} {'VERIFIED':<10} {'CREATED'}"
                )
                click.echo("-" * 90)
                for sb in sandboxes:
                    name = sb["name"][:19]
                    sandbox_id = sb["sandbox_id"][:19]
                    status = sb["status"]
                    verified = "✓" if sb.get("verified", False) else "✗"
                    created = sb["created_at"][:19]
                    click.echo(f"{name:<20} {sandbox_id:<20} {status:<12} {verified:<10} {created}")
            else:
                click.echo(f"{'NAME':<20} {'SANDBOX ID':<20} {'STATUS':<12} {'CREATED'}")
                click.echo("-" * 80)
                for sb in sandboxes:
                    name = sb["name"][:19]
                    sandbox_id = sb["sandbox_id"][:19]
                    status = sb["status"]
                    created = sb["created_at"][:19]
                    click.echo(f"{name:<20} {sandbox_id:<20} {status:<12} {created}")

            click.echo(f"\nTotal: {len(sandboxes)} sandbox(es)")
            if verify:
                click.echo("Note: Status verified with provider")

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


@sandbox.command(name="connect")
@click.argument("sandbox_id")
@click.option(
    "--provider",
    "-p",
    default="e2b",
    type=click.Choice(["e2b", "docker"], case_sensitive=False),
    help="Sandbox provider (default: e2b)",
)
@click.option(
    "--sandbox-api-key",
    envvar="E2B_API_KEY",
    required=False,
    help="Sandbox provider API key (optional, only for user-managed sandboxes)",
)
@click.option(
    "--mount-path",
    default="/mnt/nexus",
    help="Mount path in sandbox (default: /mnt/nexus)",
)
@click.option(
    "--nexus-url",
    envvar="NEXUS_URL",
    help="Nexus server URL for sandbox to connect to",
)
@click.option(
    "--nexus-api-key",
    envvar="NEXUS_API_KEY",
    help="Nexus API key for sandbox authentication",
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
@click.option(
    "--agent-id",
    type=str,
    default=None,
    help="Agent ID for version attribution. "
    "When set, file modifications will be attributed to this agent.",
)
def connect_sandbox(
    sandbox_id: str,
    provider: str,
    sandbox_api_key: str,
    mount_path: str,
    nexus_url: str | None,
    nexus_api_key: str | None,
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
    agent_id: str | None,
) -> None:
    """Connect and mount Nexus to a user-managed sandbox.

    This is a one-time operation for sandboxes you manage externally.
    Nexus will mount the filesystem to your sandbox without taking
    over lifecycle management.

    \b
    Examples:
        # Connect to E2B sandbox (API key from env)
        export E2B_API_KEY=your_key
        export NEXUS_URL=http://localhost:8080
        export NEXUS_API_KEY=sk-xxx
        nexus sandbox connect sb_xxx

        # Connect with explicit options
        nexus sandbox connect sb_xxx \\
            --sandbox-api-key your_e2b_key \\
            --nexus-url http://localhost:8080 \\
            --nexus-api-key sk-xxx

        # Custom mount path
        nexus sandbox connect sb_xxx --mount-path /home/user/nexus

        # JSON output
        nexus sandbox connect sb_xxx --json
    """
    try:
        nx: NexusFilesystem = get_default_filesystem()

        result = nx.sandbox_connect(
            sandbox_id=sandbox_id,
            provider=provider,
            sandbox_api_key=sandbox_api_key,
            mount_path=mount_path,
            nexus_url=nexus_url,
            nexus_api_key=nexus_api_key,
            agent_id=agent_id,
        )

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            if result.get("success", False):
                click.echo(f"✓ Connected to sandbox: {result['sandbox_id']}")
                click.echo(f"  Provider: {result['provider']}")
                click.echo(f"  Mount path: {result['mount_path']}")
                click.echo(f"  Mounted at: {result['mounted_at']}")

                # Show mount status if available
                mount_status = result.get("mount_status", {})
                if mount_status.get("success"):
                    files_visible = mount_status.get("files_visible", 0)
                    click.echo("  ✓ Nexus mounted successfully")
                    click.echo(f"    Files visible: {files_visible}")
                else:
                    click.echo(f"  ✗ Mount failed: {mount_status.get('message', 'Unknown error')}")
            else:
                click.echo(
                    f"✗ Failed to connect to sandbox: {result.get('mount_status', {}).get('message', 'Unknown error')}"
                )
                sys.exit(1)

    except Exception as e:
        click.echo(f"Failed to connect to sandbox: {e}")
        sys.exit(1)


@sandbox.command(name="disconnect")
@click.argument("sandbox_id")
@click.option(
    "--provider",
    "-p",
    default="e2b",
    type=click.Choice(["e2b", "docker"], case_sensitive=False),
    help="Sandbox provider (default: e2b)",
)
@click.option(
    "--sandbox-api-key",
    envvar="E2B_API_KEY",
    required=False,
    help="Sandbox provider API key (optional, only for user-managed sandboxes)",
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
def disconnect_sandbox(
    sandbox_id: str,
    provider: str,
    sandbox_api_key: str,
    json_output: bool,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Disconnect and unmount Nexus from a user-managed sandbox.

    \b
    Examples:
        # Disconnect from E2B sandbox
        export E2B_API_KEY=your_key
        nexus sandbox disconnect sb_xxx

        # Disconnect with explicit API key
        nexus sandbox disconnect sb_xxx --sandbox-api-key your_key

        # JSON output
        nexus sandbox disconnect sb_xxx --json
    """
    try:
        nx: NexusFilesystem = get_default_filesystem()

        result = nx.sandbox_disconnect(
            sandbox_id=sandbox_id,
            provider=provider,
            sandbox_api_key=sandbox_api_key,
        )

        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"✓ Disconnected from sandbox: {result['sandbox_id']}")
            click.echo(f"  Provider: {result['provider']}")
            click.echo(f"  Unmounted at: {result['unmounted_at']}")

    except Exception as e:
        click.echo(f"Failed to disconnect from sandbox: {e}")
        sys.exit(1)


def register_commands(cli: click.Group) -> None:
    """Register sandbox commands with the main CLI.

    Args:
        cli: The main Click group
    """
    cli.add_command(sandbox)
