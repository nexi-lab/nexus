"""Sandbox management commands for code execution (Issue #372).

Provides CLI commands for creating, managing, and executing code in sandboxes.
Supports E2B and other sandbox providers.
"""

import sys
from typing import Any

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
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
@add_output_options
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
    output_opts: OutputOptions,
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
    timing = CommandTiming()
    try:
        nx: Any = get_default_filesystem()

        with timing.phase("server"):
            result = nx._sandbox_rpc_service.sandbox_create(
                name=name,
                ttl_minutes=ttl,
                provider=provider,
                template_id=template,
            )

        def _render(d: dict[str, Any]) -> None:
            click.echo(f"Created sandbox: {d['sandbox_id']}")
            click.echo(f"  Name: {d['name']}")
            click.echo(f"  Status: {d['status']}")
            click.echo(f"  TTL: {d['ttl_minutes']} minutes")
            click.echo(f"  Expires: {d['expires_at']}")

        render_output(
            data=result,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render,
        )

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
@add_output_options
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
    output_opts: OutputOptions,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Get existing sandbox or create new one (idempotent).

    \b
    Examples:
        nexus sandbox get-or-create my-agent-sandbox
        nexus sandbox get-or-create my-sandbox --no-verify
        nexus sandbox get-or-create my-sandbox --ttl 30 --provider e2b
        nexus sandbox get-or-create my-sandbox --json
    """
    timing = CommandTiming()
    try:
        nx: Any = get_default_filesystem()

        with timing.phase("server"):
            result = nx._sandbox_rpc_service.sandbox_get_or_create(
                name=name,
                ttl_minutes=ttl,
                provider=provider,
                template_id=template,
                verify_status=verify,
            )

        def _render(d: dict[str, Any]) -> None:
            action = "Found and verified" if d.get("verified") else "Got"
            click.echo(f"{action} sandbox: {d['sandbox_id']}")
            click.echo(f"  Name: {d['name']}")
            click.echo(f"  Status: {d['status']}")
            click.echo(f"  TTL: {d['ttl_minutes']} minutes")
            click.echo(f"  Expires: {d['expires_at']}")
            if verify:
                click.echo(f"  Verified: {d.get('verified', False)}")

        render_output(data=result, output_opts=output_opts, timing=timing, human_formatter=_render)

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
@click.option("--code", "-c", help="Code to execute (use - to read from stdin)")
@click.option("--file", "-f", type=click.Path(exists=True), help="File containing code to execute")
@click.option("--timeout", type=int, default=30, help="Execution timeout in seconds (default: 30)")
@add_output_options
@click.option("--data-dir", envvar="NEXUS_DATA_DIR", help="Nexus data directory")
def run_code(
    sandbox_id: str,
    language: str,
    code: str | None,
    file: str | None,
    timeout: int,
    output_opts: OutputOptions,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Run code in a sandbox.

    \b
    Examples:
        nexus sandbox run sb_123 -c "print('Hello')"
        nexus sandbox run sb_123 -f script.py
        nexus sandbox run sb_123 -c "print('test')" --json
    """
    timing = CommandTiming()
    try:
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

        nx: Any = get_default_filesystem()
        with timing.phase("server"):
            result = nx._sandbox_rpc_service.sandbox_run(
                sandbox_id=sandbox_id, language=language, code=code_to_run, timeout=timeout
            )

        def _render(d: dict[str, Any]) -> None:
            if d["stdout"]:
                click.echo("=== STDOUT ===")
                click.echo(d["stdout"])
            if d["stderr"]:
                click.echo("=== STDERR ===", err=True)
                click.echo(d["stderr"], err=True)
            exit_code = d["exit_code"]
            execution_time = d["execution_time"]
            if exit_code == 0:
                click.echo(f"Execution completed in {execution_time:.2f}s")
            else:
                click.echo(f"Execution failed with exit code {exit_code} ({execution_time:.2f}s)")
                sys.exit(exit_code)

        render_output(data=result, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        click.echo(f"Failed to run code: {e}")
        sys.exit(1)


@sandbox.command(name="pause")
@click.argument("sandbox_id")
@add_output_options
@click.option("--data-dir", envvar="NEXUS_DATA_DIR", help="Nexus data directory")
def pause_sandbox(sandbox_id: str, output_opts: OutputOptions, data_dir: str | None) -> None:  # noqa: ARG001
    """Pause a sandbox to save costs.

    \b
    Examples:
        nexus sandbox pause sb_123
        nexus sandbox pause sb_123 --json
    """
    timing = CommandTiming()
    try:
        nx: Any = get_default_filesystem()
        with timing.phase("server"):
            result = nx._sandbox_rpc_service.sandbox_pause(sandbox_id=sandbox_id)

        def _render(d: dict[str, Any]) -> None:
            click.echo(f"Paused sandbox: {sandbox_id}")
            click.echo(f"  Status: {d['status']}")
            click.echo(f"  Paused at: {d['paused_at']}")

        render_output(data=result, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        click.echo(f"Failed to pause sandbox: {e}")
        sys.exit(1)


@sandbox.command(name="resume")
@click.argument("sandbox_id")
@add_output_options
@click.option("--data-dir", envvar="NEXUS_DATA_DIR", help="Nexus data directory")
def resume_sandbox(sandbox_id: str, output_opts: OutputOptions, data_dir: str | None) -> None:  # noqa: ARG001
    """Resume a paused sandbox.

    \b
    Examples:
        nexus sandbox resume sb_123
        nexus sandbox resume sb_123 --json
    """
    timing = CommandTiming()
    try:
        nx: Any = get_default_filesystem()
        with timing.phase("server"):
            result = nx._sandbox_rpc_service.sandbox_resume(sandbox_id=sandbox_id)

        def _render(d: dict[str, Any]) -> None:
            click.echo(f"Resumed sandbox: {sandbox_id}")
            click.echo(f"  Status: {d['status']}")
            click.echo(f"  Expires: {d['expires_at']}")

        render_output(data=result, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        click.echo(f"Failed to resume sandbox: {e}")
        sys.exit(1)


@sandbox.command(name="stop")
@click.argument("sandbox_id")
@add_output_options
@click.option("--data-dir", envvar="NEXUS_DATA_DIR", help="Nexus data directory")
def stop_sandbox(sandbox_id: str, output_opts: OutputOptions, data_dir: str | None) -> None:  # noqa: ARG001
    """Stop and destroy a sandbox.

    \b
    Examples:
        nexus sandbox stop sb_123
        nexus sandbox stop sb_123 --json
    """
    timing = CommandTiming()
    try:
        nx: Any = get_default_filesystem()
        with timing.phase("server"):
            result = nx._sandbox_rpc_service.sandbox_stop(sandbox_id=sandbox_id)

        def _render(d: dict[str, Any]) -> None:
            click.echo(f"Stopped sandbox: {sandbox_id}")
            click.echo(f"  Status: {d['status']}")
            click.echo(f"  Stopped at: {d['stopped_at']}")

        render_output(data=result, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        click.echo(f"Failed to stop sandbox: {e}")
        sys.exit(1)


@sandbox.command(name="list")
@click.option("--user-id", "-u", help="Filter by user ID")
@click.option("--agent-id", "-a", help="Filter by agent ID")
@click.option("--zone-id", "-z", help="Filter by zone ID")
@click.option("--verify", is_flag=True, help="Verify status with provider (slower but accurate)")
@add_output_options
@click.option("--data-dir", envvar="NEXUS_DATA_DIR", help="Nexus data directory")
def list_sandboxes(
    user_id: str | None,
    agent_id: str | None,
    zone_id: str | None,
    verify: bool,
    output_opts: OutputOptions,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """List sandboxes with optional filtering.

    \b
    Examples:
        nexus sandbox list
        nexus sandbox list --user-id alice
        nexus sandbox list --verify
        nexus sandbox list --json
    """
    timing = CommandTiming()
    try:
        nx: Any = get_default_filesystem()
        with timing.phase("server"):
            result = nx._sandbox_rpc_service.sandbox_list(
                user_id=user_id, agent_id=agent_id, zone_id=zone_id, verify_status=verify
            )
        sandboxes = result["sandboxes"]

        def _render(data: list[dict[str, Any]]) -> None:
            if not data:
                click.echo("No sandboxes found.")
                return
            if verify:
                click.echo(
                    f"{'NAME':<20} {'SANDBOX ID':<20} {'STATUS':<12} {'VERIFIED':<10} {'CREATED'}"
                )
                click.echo("-" * 90)
                for sb in data:
                    click.echo(
                        f"{sb['name'][:19]:<20} {sb['sandbox_id'][:19]:<20} {sb['status']:<12} {'yes' if sb.get('verified', False) else 'no':<10} {sb['created_at'][:19]}"
                    )
            else:
                click.echo(f"{'NAME':<20} {'SANDBOX ID':<20} {'STATUS':<12} {'CREATED'}")
                click.echo("-" * 80)
                for sb in data:
                    click.echo(
                        f"{sb['name'][:19]:<20} {sb['sandbox_id'][:19]:<20} {sb['status']:<12} {sb['created_at'][:19]}"
                    )
            click.echo(f"\nTotal: {len(data)} sandbox(es)")
            if verify:
                click.echo("Note: Status verified with provider")

        render_output(
            data=sandboxes, output_opts=output_opts, timing=timing, human_formatter=_render
        )
    except Exception as e:
        click.echo(f"Failed to list sandboxes: {e}")
        sys.exit(1)


@sandbox.command(name="status")
@click.argument("sandbox_id")
@add_output_options
@click.option("--data-dir", envvar="NEXUS_DATA_DIR", help="Nexus data directory")
def sandbox_status(sandbox_id: str, output_opts: OutputOptions, data_dir: str | None) -> None:  # noqa: ARG001
    """Get sandbox status and details.

    \b
    Examples:
        nexus sandbox status sb_123
        nexus sandbox status sb_123 --json
    """
    timing = CommandTiming()
    try:
        nx: Any = get_default_filesystem()
        with timing.phase("server"):
            result = nx._sandbox_rpc_service.sandbox_status(sandbox_id=sandbox_id)

        def _render(d: dict[str, Any]) -> None:
            click.echo(f"Sandbox: {d['sandbox_id']}")
            click.echo(f"  Name: {d['name']}")
            click.echo(f"  Status: {d['status']}")
            click.echo(f"  Provider: {d['provider']}")
            click.echo(f"  User: {d['user_id']}")
            click.echo(f"  Created: {d['created_at']}")
            click.echo(f"  Last Active: {d['last_active_at']}")
            click.echo(f"  TTL: {d['ttl_minutes']} minutes")
            click.echo(f"  Expires: {d.get('expires_at', 'N/A')}")
            click.echo(f"  Uptime: {d['uptime_seconds']:.0f} seconds")

        render_output(data=result, output_opts=output_opts, timing=timing, human_formatter=_render)
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
    "--sandbox-api-key", envvar="E2B_API_KEY", required=False, help="Sandbox provider API key"
)
@click.option(
    "--mount-path", default="/mnt/nexus", help="Mount path in sandbox (default: /mnt/nexus)"
)
@click.option("--nexus-url", envvar="NEXUS_URL", help="Nexus server URL for sandbox to connect to")
@click.option(
    "--nexus-api-key", envvar="NEXUS_API_KEY", help="Nexus API key for sandbox authentication"
)
@add_output_options
@click.option("--data-dir", envvar="NEXUS_DATA_DIR", help="Nexus data directory")
@click.option("--agent-id", type=str, default=None, help="Agent ID for version attribution.")
def connect_sandbox(
    sandbox_id: str,
    provider: str,
    sandbox_api_key: str,
    mount_path: str,
    nexus_url: str | None,
    nexus_api_key: str | None,
    output_opts: OutputOptions,
    data_dir: str | None,  # noqa: ARG001
    agent_id: str | None,
) -> None:
    """Connect and mount Nexus to a user-managed sandbox.

    \b
    Examples:
        nexus sandbox connect sb_xxx
        nexus sandbox connect sb_xxx --mount-path /home/user/nexus
        nexus sandbox connect sb_xxx --json
    """
    timing = CommandTiming()
    try:
        nx: Any = get_default_filesystem()
        with timing.phase("server"):
            result = nx._sandbox_rpc_service.sandbox_connect(
                sandbox_id=sandbox_id,
                provider=provider,
                sandbox_api_key=sandbox_api_key,
                mount_path=mount_path,
                nexus_url=nexus_url,
                nexus_api_key=nexus_api_key,
                agent_id=agent_id,
            )

        def _render(d: dict[str, Any]) -> None:
            if d.get("success", False):
                click.echo(f"Connected to sandbox: {d['sandbox_id']}")
                click.echo(f"  Provider: {d['provider']}")
                click.echo(f"  Mount path: {d['mount_path']}")
                click.echo(f"  Mounted at: {d['mounted_at']}")
                ms = d.get("mount_status", {})
                if ms.get("success"):
                    click.echo(f"  Nexus mounted successfully ({ms.get('files_visible', 0)} files)")
                else:
                    click.echo(f"  Mount failed: {ms.get('message', 'Unknown error')}")
            else:
                msg = d.get("mount_status", {}).get("message", "Unknown error")
                click.echo(f"Failed to connect to sandbox: {msg}")
                sys.exit(1)

        render_output(data=result, output_opts=output_opts, timing=timing, human_formatter=_render)
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
    "--sandbox-api-key", envvar="E2B_API_KEY", required=False, help="Sandbox provider API key"
)
@add_output_options
@click.option("--data-dir", envvar="NEXUS_DATA_DIR", help="Nexus data directory")
def disconnect_sandbox(
    sandbox_id: str,
    provider: str,
    sandbox_api_key: str,
    output_opts: OutputOptions,
    data_dir: str | None,  # noqa: ARG001
) -> None:
    """Disconnect and unmount Nexus from a user-managed sandbox.

    \b
    Examples:
        nexus sandbox disconnect sb_xxx
        nexus sandbox disconnect sb_xxx --json
    """
    timing = CommandTiming()
    try:
        nx: Any = get_default_filesystem()
        with timing.phase("server"):
            result = nx._sandbox_rpc_service.sandbox_disconnect(
                sandbox_id=sandbox_id, provider=provider, sandbox_api_key=sandbox_api_key
            )

        def _render(d: dict[str, Any]) -> None:
            click.echo(f"Disconnected from sandbox: {d['sandbox_id']}")
            click.echo(f"  Provider: {d['provider']}")
            click.echo(f"  Unmounted at: {d['unmounted_at']}")

        render_output(data=result, output_opts=output_opts, timing=timing, human_formatter=_render)
    except Exception as e:
        click.echo(f"Failed to disconnect from sandbox: {e}")
        sys.exit(1)


def register_commands(cli: click.Group) -> None:
    """Register sandbox commands with the main CLI."""
    cli.add_command(sandbox)
