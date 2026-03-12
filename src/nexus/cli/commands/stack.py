"""Stack lifecycle commands — up, down, logs, restart.

These commands manage the Docker Compose stack for ``shared`` and ``demo``
presets.  They wrap ``docker compose`` via subprocess, adding pre-flight
port conflict detection, parallel health polling, and rich status output.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import click
import yaml

from nexus.cli.port_utils import (
    VALID_STRATEGIES,
    check_port_available,
    resolve_ports,
)
from nexus.cli.utils import console

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

CONFIG_SEARCH_PATHS = ("./nexus.yaml", "./nexus.yml")


def _load_project_config() -> dict[str, Any]:
    """Load the project-local nexus.yaml."""
    for candidate in CONFIG_SEARCH_PATHS:
        p = Path(candidate)
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f) or {}
    console.print("[red]Error:[/red] No nexus.yaml found. Run `nexus init` first.")
    raise SystemExit(1)


def _save_project_config(config: dict[str, Any], path: str | None = None) -> None:
    """Persist config back to nexus.yaml (e.g. after port resolution)."""
    target = Path(path) if path else None
    if target is None:
        for candidate in CONFIG_SEARCH_PATHS:
            if Path(candidate).exists():
                target = Path(candidate)
                break
    if target is None:
        target = Path(CONFIG_SEARCH_PATHS[0])
    with open(target, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Docker Compose helpers
# ---------------------------------------------------------------------------


def _find_docker_compose() -> str:
    """Return the docker compose command prefix."""
    # Prefer `docker compose` (Compose V2, plugin)
    if shutil.which("docker"):
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return "docker compose"
    # Fallback to standalone `docker-compose`
    if shutil.which("docker-compose"):
        return "docker-compose"
    console.print("[red]Error:[/red] Docker Compose is not installed.")
    console.print(
        "[yellow]Hint:[/yellow] Install Docker Desktop or the compose plugin: "
        "https://docs.docker.com/compose/install/"
    )
    raise SystemExit(1)


def _compose_cmd(
    compose_file: str,
    profiles: list[str],
    *args: str,
) -> list[str]:
    """Build the full docker compose command list."""
    base = _find_docker_compose().split()
    cmd = [*base, "-f", compose_file]
    for profile in profiles:
        cmd.extend(["--profile", profile])
    cmd.extend(args)
    return cmd


def _run_compose(
    compose_file: str,
    profiles: list[str],
    *args: str,
    extra_env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute a docker compose command."""
    import os

    cmd = _compose_cmd(compose_file, profiles, *args)
    run_env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        cmd,
        env=run_env,
        text=True,
        capture_output=capture,
    )


# ---------------------------------------------------------------------------
# Health polling
# ---------------------------------------------------------------------------

# Service health endpoints (service_name -> (url_template, timeout_seconds))
HEALTH_ENDPOINTS: dict[str, tuple[str, int]] = {
    "nexus": ("http://localhost:{http}/health", 120),
    "postgres": ("", 30),  # checked via pg_isready in container
    "dragonfly": ("", 15),
    "zoekt": ("http://localhost:{zoekt}/", 30),
}


async def _poll_service_health(
    service: str,
    ports: dict[str, int],
    timeout: int,
) -> tuple[str, float, bool]:
    """Poll a single service until healthy or timeout.

    Returns (service_name, elapsed_seconds, healthy).
    """
    start = time.monotonic()
    url_template, default_timeout = HEALTH_ENDPOINTS.get(service, ("", timeout))
    effective_timeout = min(timeout, default_timeout) if default_timeout else timeout

    # For services without HTTP health endpoints, just check port availability
    port_key_map = {
        "nexus": "http",
        "postgres": "postgres",
        "dragonfly": "dragonfly",
        "zoekt": "zoekt",
    }
    port_key = port_key_map.get(service)
    if not port_key or port_key not in ports:
        return service, 0.0, True

    port = ports[port_key]
    delay = 0.5

    while (time.monotonic() - start) < effective_timeout:
        # Check if port is accepting connections (i.e. NOT available = service is up)
        if not check_port_available(port):
            # Port is in use — service is likely healthy
            if url_template:
                # Also verify HTTP health endpoint
                url = url_template.format(**ports)
                try:
                    import urllib.request

                    req = urllib.request.Request(url, method="GET")
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        if resp.status == 200:
                            return service, time.monotonic() - start, True
                except Exception:
                    pass
            else:
                # No HTTP endpoint — port connectivity is enough
                return service, time.monotonic() - start, True

        await asyncio.sleep(delay)
        delay = min(delay * 2, 4.0)  # exponential backoff, cap at 4s

    return service, time.monotonic() - start, False


async def _poll_all_services(
    services: list[str],
    ports: dict[str, int],
    timeout: int,
) -> list[tuple[str, float, bool]]:
    """Poll all services in parallel."""
    tasks = [_poll_service_health(service, ports, timeout) for service in services]
    return list(await asyncio.gather(*tasks))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def register_commands(cli: click.Group) -> None:
    """Register stack lifecycle commands."""
    cli.add_command(up)
    cli.add_command(down)
    cli.add_command(logs)
    cli.add_command(restart)


@click.command()
@click.option(
    "--detach/--no-detach", "-d", default=True, show_default=True, help="Run in background."
)
@click.option(
    "--with",
    "addons",
    multiple=True,
    type=click.Choice(
        ["nats", "mcp", "frontend", "langgraph", "observability"],
        case_sensitive=False,
    ),
    help="Optional add-on services (repeatable).",
)
@click.option(
    "--port-strategy",
    type=click.Choice(VALID_STRATEGIES),
    default="auto",
    show_default=True,
    help="How to handle port conflicts: auto (pick next free), prompt, fail.",
)
@click.option(
    "--compose-file",
    type=click.Path(exists=True),
    default=None,
    help="Override the compose file path.",
)
@click.option("--build", is_flag=True, default=False, help="Rebuild images before starting.")
@click.option(
    "--timeout", type=int, default=180, show_default=True, help="Health check timeout in seconds."
)
def up(
    detach: bool,
    addons: tuple[str, ...],
    port_strategy: str,
    compose_file: str | None,
    build: bool,
    timeout: int,
) -> None:
    """Start the Nexus stack.

    Reads nexus.yaml, resolves port conflicts, starts Docker Compose
    services, waits for health, and prints the service table.

    Examples:
        nexus up                        # start from nexus.yaml
        nexus up --with nats            # add NATS event bus
        nexus up --port-strategy prompt # ask on conflicts
        nexus up --build                # rebuild images first
    """
    config = _load_project_config()
    preset = config.get("preset", "local")

    if preset == "local":
        console.print(
            "[yellow]Preset 'local' does not use Docker.[/yellow] "
            "Use `nexus serve` to start a local server, or re-init with "
            "`nexus init --preset shared`."
        )
        raise SystemExit(0)

    # Determine compose file
    cf = compose_file or config.get("compose_file", "./nexus-stack.yml")
    if not Path(cf).exists():
        console.print(f"[red]Error:[/red] Compose file not found: {cf}")
        console.print("[yellow]Hint:[/yellow] Ensure nexus-stack.yml is in the project root.")
        raise SystemExit(1)

    # Build profiles list
    profiles = list(config.get("compose_profiles", []))
    # Add add-on profiles
    addon_profile_map = {
        "nats": "events",
        "mcp": "mcp",
        "frontend": "frontend",
        "langgraph": "langgraph",
        "observability": "observability",
    }
    for addon in addons:
        profile = addon_profile_map.get(addon, addon)
        if profile not in profiles:
            profiles.append(profile)
    # Also add configured add-ons
    for addon in config.get("addons", []):
        profile = addon_profile_map.get(addon, addon)
        if profile not in profiles:
            profiles.append(profile)

    # Port conflict resolution — check all ports in config (not filtered by services)
    ports = config.get("ports", {})
    active_services = config.get("services", [])
    resolved_ports, port_messages = resolve_ports(ports, strategy=port_strategy)

    # Print header
    console.print()
    console.print(f"[bold]Starting Nexus preset: {preset}[/bold]")
    console.print(f"  Using stack: {cf}")
    if addons:
        console.print(f"  Add-ons: {', '.join(addons)}")
    console.print()

    # Print port resolution messages
    for msg in port_messages:
        console.print(f"  [yellow]{msg}[/yellow]")

    # Persist resolved ports back to config
    if resolved_ports != ports:
        config["ports"] = resolved_ports
        _save_project_config(config)

    # Build environment variables for compose — nexus.yaml is the SSOT
    compose_env: dict[str, str] = {
        # Ports
        "NEXUS_PORT": str(resolved_ports.get("http", 2026)),
        "NEXUS_GRPC_PORT": str(resolved_ports.get("grpc", 2028)),
        "POSTGRES_PORT": str(resolved_ports.get("postgres", 5432)),
        "DRAGONFLY_PORT": str(resolved_ports.get("dragonfly", 6379)),
        "ZOEKT_PORT": str(resolved_ports.get("zoekt", 6070)),
        # Data directory (host path for volume mount)
        "NEXUS_HOST_DATA_DIR": str(config.get("data_dir", "./nexus-data")),
        # Admin user
        "NEXUS_ADMIN_USER": str(config.get("admin_user", "admin")),
    }

    # Auth config
    auth = config.get("auth", "none")
    if auth == "database":
        compose_env["NEXUS_AUTH_TYPE"] = "database"

    # TLS config — paths must be container-relative (/app/data/tls/...)
    # since the host data_dir is mounted at /app/data inside the container.
    if config.get("tls"):
        compose_env["NEXUS_TLS_ENABLED"] = "true"
        compose_env["NEXUS_TLS_CERT"] = "/app/data/tls/server.crt"
        compose_env["NEXUS_TLS_KEY"] = "/app/data/tls/server.key"
        compose_env["NEXUS_TLS_CA"] = "/app/data/tls/ca.crt"

    # Start compose
    compose_args: list[str] = ["up"]
    if detach:
        compose_args.append("-d")
    if build:
        compose_args.append("--build")

    result = _run_compose(cf, profiles, *compose_args, extra_env=compose_env)
    if result.returncode != 0:
        console.print("[red]Error:[/red] Docker Compose failed to start.")
        raise SystemExit(result.returncode)

    if not detach:
        # Foreground mode — compose handles output
        return

    # Health polling
    console.print("[bold]Waiting for services...[/bold]")
    health_services = [s for s in active_services if s in HEALTH_ENDPOINTS]
    results = asyncio.run(_poll_all_services(health_services, resolved_ports, timeout))

    # Print results
    console.print()
    all_healthy = True
    for service, elapsed, healthy in results:
        if healthy:
            console.print(f"  [green]✓[/green] {service} ({elapsed:.1f}s)")
        else:
            console.print(f"  [red]✗[/red] {service} (timed out after {elapsed:.0f}s)")
            all_healthy = False

    if not all_healthy:
        console.print()
        console.print("[yellow]Some services did not become healthy.[/yellow]")
        console.print("  Run `nexus logs` to investigate.")
        raise SystemExit(1)

    # Print final status table
    console.print()
    console.print("[bold]Healthy services:[/bold]")
    http_port = resolved_ports.get("http", 2026)
    grpc_port = resolved_ports.get("grpc", 2028)
    pg_port = resolved_ports.get("postgres", 5432)
    df_port = resolved_ports.get("dragonfly", 6379)
    zk_port = resolved_ports.get("zoekt", 6070)

    console.print(f"  nexus       http://localhost:{http_port}")
    console.print(f"  grpc        localhost:{grpc_port}")
    if "postgres" in active_services:
        console.print(f"  postgres    localhost:{pg_port}")
    if "dragonfly" in active_services:
        console.print(f"  dragonfly   localhost:{df_port}")
    if "zoekt" in active_services:
        console.print(f"  zoekt       http://localhost:{zk_port}")

    # Print next steps
    console.print()
    console.print("[bold]Next steps:[/bold]")
    if preset == "demo":
        console.print("  nexus demo init")
    console.print("  nexus status")


@click.command()
@click.option("--volumes", "-v", is_flag=True, default=False, help="Also remove volumes.")
def down(volumes: bool) -> None:
    """Stop the Nexus stack.

    Stops all Docker Compose services started by `nexus up`.

    Examples:
        nexus down             # stop services
        nexus down --volumes   # stop and remove volumes
    """
    config = _load_project_config()
    preset = config.get("preset", "local")

    if preset == "local":
        console.print("[yellow]Preset 'local' has no Docker services to stop.[/yellow]")
        raise SystemExit(0)

    cf = config.get("compose_file", "./nexus-stack.yml")
    profiles = list(config.get("compose_profiles", []))
    for addon in config.get("addons", []):
        addon_map = {
            "nats": "events",
            "mcp": "mcp",
            "frontend": "frontend",
            "langgraph": "langgraph",
        }
        profile = addon_map.get(addon, addon)
        if profile not in profiles:
            profiles.append(profile)

    args: list[str] = ["down"]
    if volumes:
        args.append("--volumes")

    console.print(f"[bold]Stopping Nexus preset: {preset}[/bold]")
    result = _run_compose(cf, profiles, *args)
    if result.returncode == 0:
        console.print("[green]✓[/green] Stack stopped.")
    else:
        console.print("[red]Error:[/red] Failed to stop stack.")
        raise SystemExit(result.returncode)


@click.command()
@click.option(
    "--follow/--no-follow", "-f", default=True, show_default=True, help="Follow log output."
)
@click.option("--tail", type=int, default=100, show_default=True, help="Number of lines to show.")
@click.argument("service", required=False, default=None)
def logs(follow: bool, tail: int, service: str | None) -> None:
    """View logs from the Nexus stack.

    Examples:
        nexus logs              # all services
        nexus logs nexus        # single service
        nexus logs --tail 50    # last 50 lines
    """
    config = _load_project_config()
    cf = config.get("compose_file", "./nexus-stack.yml")
    profiles = list(config.get("compose_profiles", []))

    args: list[str] = ["logs", "--tail", str(tail)]
    if follow:
        args.append("--follow")
    if service:
        args.append(service)

    _run_compose(cf, profiles, *args)


@click.command()
@click.option("--build", is_flag=True, default=False, help="Rebuild images.")
def restart(build: bool) -> None:
    """Restart the Nexus stack.

    Equivalent to `nexus down && nexus up`.

    Examples:
        nexus restart           # restart services
        nexus restart --build   # rebuild and restart
    """
    config = _load_project_config()
    preset = config.get("preset", "local")

    if preset == "local":
        console.print("[yellow]Preset 'local' has no Docker services to restart.[/yellow]")
        raise SystemExit(0)

    cf = config.get("compose_file", "./nexus-stack.yml")
    profiles = list(config.get("compose_profiles", []))

    console.print(f"[bold]Restarting Nexus preset: {preset}[/bold]")
    _run_compose(cf, profiles, "down")

    args: list[str] = ["up", "-d"]
    if build:
        args.append("--build")
    result = _run_compose(cf, profiles, *args)
    if result.returncode == 0:
        console.print("[green]✓[/green] Stack restarted.")
    else:
        console.print("[red]Error:[/red] Failed to restart stack.")
        raise SystemExit(result.returncode)
