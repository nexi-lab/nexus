"""Infrastructure lifecycle commands: ``nexus up``, ``nexus down``, ``nexus logs``.

Thin wrappers around Docker Compose for managing the Nexus service stack.
"""

from __future__ import annotations

import sys

import click

from nexus.cli.utils import console, handle_error


@click.command(name="up")
@click.option(
    "--profile",
    "profiles",
    multiple=True,
    default=(),
    help=(
        "Compose profiles to activate (server, mcp, cache, events, all). "
        "Defaults to server, cache, events."
    ),
)
@click.option(
    "--detach/--no-detach",
    "-d",
    default=True,
    help="Run containers in the background (default: detach).",
    show_default=True,
)
@click.option(
    "--build",
    "build_images",
    is_flag=True,
    help="Build images before starting.",
)
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for services to be healthy before returning (default: wait).",
    show_default=True,
)
def up(profiles: tuple[str, ...], detach: bool, build_images: bool, wait: bool) -> None:
    """Start the Nexus Docker Compose stack.

    Examples:
        nexus up                          # default profiles (server, cache, events)
        nexus up --profile all            # all services
        nexus up --profile server --profile mcp
        nexus up --no-detach              # foreground (follow logs)
        nexus up --build                  # rebuild images first
    """
    from nexus.cli.compose import ComposeError, ComposeRunner

    try:
        runner = ComposeRunner()
        profile_list = list(profiles) if profiles else None

        args: list[str] = ["up"]
        if detach:
            args.append("-d")
        if build_images:
            args.append("--build")
        if wait and detach:
            args.append("--wait")

        console.print("[cyan]Starting Nexus services...[/cyan]")
        active = profile_list or ["server", "cache", "events"]
        console.print(f"  Profiles: [bold]{', '.join(active)}[/bold]")

        if detach:
            result = runner.run(*args, profiles=profile_list)
            if result.returncode != 0:
                console.print("[red]Failed to start services.[/red]")
                sys.exit(result.returncode)
            console.print("[green]Services started.[/green]")
        else:
            # Foreground mode — attach to logs, forward Ctrl+C
            exit_code = runner.run_attached(*args, profiles=profile_list)
            sys.exit(exit_code)

    except ComposeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(exc.exit_code)
    except Exception as exc:
        handle_error(exc)


@click.command(name="down")
@click.option(
    "--volumes",
    is_flag=True,
    help="Remove named volumes declared in the compose file.",
)
@click.option(
    "--profile",
    "profiles",
    multiple=True,
    default=(),
    help="Compose profiles to stop. Defaults to all running services.",
)
def down(volumes: bool, profiles: tuple[str, ...]) -> None:
    """Stop the Nexus Docker Compose stack.

    Examples:
        nexus down
        nexus down --volumes      # also remove persistent data
    """
    from nexus.cli.compose import ComposeError, ComposeRunner

    try:
        runner = ComposeRunner()
        profile_list = list(profiles) if profiles else None

        args: list[str] = ["down"]
        if volumes:
            args.append("--volumes")

        console.print("[cyan]Stopping Nexus services...[/cyan]")
        result = runner.run(*args, profiles=profile_list)
        if result.returncode != 0:
            console.print("[red]Failed to stop services.[/red]")
            sys.exit(result.returncode)
        console.print("[green]Services stopped.[/green]")

    except ComposeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(exc.exit_code)
    except Exception as exc:
        handle_error(exc)


@click.command(name="logs")
@click.argument("services", nargs=-1)
@click.option(
    "--follow/--no-follow",
    "-f",
    default=True,
    help="Follow log output (default: follow).",
    show_default=True,
)
@click.option(
    "--tail",
    type=int,
    default=100,
    help="Number of lines to show from the end of logs.",
    show_default=True,
)
def logs(services: tuple[str, ...], follow: bool, tail: int) -> None:
    """Show logs from Nexus Docker services.

    Examples:
        nexus logs                         # all services, follow
        nexus logs nexus-server            # specific service
        nexus logs --no-follow --tail 50   # last 50 lines, exit
    """
    from nexus.cli.compose import ComposeError, ComposeRunner

    try:
        runner = ComposeRunner()

        args: list[str] = ["logs"]
        if follow:
            args.append("--follow")
        args.extend(["--tail", str(tail)])
        args.extend(services)

        exit_code = runner.run_attached(*args, profiles=None)
        sys.exit(exit_code)

    except ComposeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(exc.exit_code)
    except Exception as exc:
        handle_error(exc)


def register_commands(cli: click.Group) -> None:
    """Register infrastructure lifecycle commands."""
    cli.add_command(up)
    cli.add_command(down)
    cli.add_command(logs)
