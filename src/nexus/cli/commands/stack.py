"""Stack lifecycle commands — up, down, logs, restart, upgrade.

These commands manage the Docker Compose stack for ``shared`` and ``demo``
presets.  They wrap ``docker compose`` via subprocess, adding pre-flight
port conflict detection, parallel health polling, and rich status output.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import click
import yaml

from nexus.cli.commands.init_cmd import (
    ADDON_PROFILE_MAP,
    DEFAULT_IMAGE_REGISTRY,
    _resolve_image_ref,
)
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


def _resolve_image_ref_from_config(config: dict[str, Any]) -> str:
    """Resolve the effective image reference from config + env overrides.

    Precedence (highest to lowest):
      1. NEXUS_IMAGE_REF environment variable
      2. config["image_ref"]
      3. NEXUS_IMAGE_TAG environment variable (deprecated compat)
      4. config["image_tag"] (deprecated compat — maps to full ref)
      5. Empty string (no image pinning)
    """
    # New model: NEXUS_IMAGE_REF env var wins
    env_ref = os.environ.get("NEXUS_IMAGE_REF", "")
    if env_ref:
        return env_ref

    # Config image_ref (set by nexus init)
    config_ref = config.get("image_ref", "")
    if config_ref:
        return config_ref

    # Deprecated: NEXUS_IMAGE_TAG env var → expand to full ref
    env_tag = os.environ.get("NEXUS_IMAGE_TAG", "")
    if env_tag:
        return f"{DEFAULT_IMAGE_REGISTRY}:{env_tag}"

    # Deprecated: config image_tag → expand to full ref
    config_tag = config.get("image_tag", "")
    if config_tag:
        return f"{DEFAULT_IMAGE_REGISTRY}:{config_tag}"

    return ""


def _derive_project_env(
    config: dict[str, Any],
    resolved_ports: dict[str, int] | None = None,
) -> dict[str, str]:
    """Build the compose environment from nexus.yaml config.

    Returns a dict with COMPOSE_PROJECT_NAME, NEXUS_HOST_DATA_DIR,
    port vars, auth type, image ref, and TLS settings — everything
    compose commands need to target the correct project.

    When *resolved_ports* is provided (e.g. after conflict resolution),
    those values are used instead of config["ports"].
    """
    data_dir = str(Path(config.get("data_dir", "./nexus-data")).resolve())
    project_hash = hashlib.md5(data_dir.encode()).hexdigest()[:8]
    project_name = f"nexus-{project_hash}"

    ports = resolved_ports or config.get("ports", {})
    env: dict[str, str] = {
        "COMPOSE_PROJECT_NAME": project_name,
        "NEXUS_PORT": str(ports.get("http", 2026)),
        "NEXUS_GRPC_PORT": str(ports.get("grpc", 2028)),
        "POSTGRES_PORT": str(ports.get("postgres", 5432)),
        "DRAGONFLY_PORT": str(ports.get("dragonfly", 6379)),
        "ZOEKT_PORT": str(ports.get("zoekt", 6070)),
        "NEXUS_HOST_DATA_DIR": data_dir,
        "NEXUS_ADMIN_USER": str(config.get("admin_user", "admin")),
        "NEXUS_AUTH_TYPE": config.get("auth", "none"),
    }

    # Pass the API key to the container so the production entrypoint
    # can register it without generating a new one.
    api_key = config.get("api_key", "")
    if api_key:
        env["NEXUS_API_KEY"] = api_key

    # Resolve the image reference (supports new image_ref and deprecated image_tag)
    image_ref = _resolve_image_ref_from_config(config)
    if image_ref:
        env["NEXUS_IMAGE_REF"] = image_ref

    if config.get("tls"):
        env["NEXUS_TLS_ENABLED"] = "true"
        env["NEXUS_TLS_CERT"] = "/app/data/tls/server.crt"
        env["NEXUS_TLS_KEY"] = "/app/data/tls/server.key"
        env["NEXUS_TLS_CA"] = "/app/data/tls/ca.crt"
        # TLS enabled — let the server use mTLS for gRPC
        env["NEXUS_GRPC_INSECURE"] = "false"
    else:
        # No TLS — skip TOFU mTLS so CLI can connect without certs
        env["NEXUS_GRPC_INSECURE"] = "true"

    return env


def _resolve_profiles(
    config: dict[str, Any],
    cli_addons: tuple[str, ...] = (),
) -> list[str]:
    """Build the complete list of compose profiles from config + CLI addons.

    Single source of truth for addon → profile mapping. Used by up, down,
    logs, and restart.
    """
    profiles = list(config.get("compose_profiles", []))
    # Add CLI add-ons
    for addon in cli_addons:
        profile = ADDON_PROFILE_MAP.get(addon, addon)
        if profile not in profiles:
            profiles.append(profile)
    # Add configured add-ons from nexus.yaml
    for addon in config.get("addons", []):
        profile = ADDON_PROFILE_MAP.get(addon, addon)
        if profile not in profiles:
            profiles.append(profile)
    return profiles


# ---------------------------------------------------------------------------
# Docker Compose helpers
# ---------------------------------------------------------------------------


def _compose_profiles(compose_file: str) -> set[str]:
    """Return the set of profiles defined across all services."""
    try:
        with open(compose_file) as f:
            stack = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return set()
    except yaml.YAMLError as exc:
        console.print(f"[yellow]Warning:[/yellow] Failed to parse {compose_file}: {exc}")
        return set()

    if not isinstance(stack, dict):
        console.print(f"[yellow]Warning:[/yellow] {compose_file} does not contain a valid mapping")
        return set()

    profiles: set[str] = set()
    for svc in (stack.get("services") or {}).values():
        if isinstance(svc, dict):
            for p in svc.get("profiles") or []:
                profiles.add(p)
    return profiles


@functools.lru_cache(maxsize=1)
def _find_docker_compose() -> str:
    """Return the docker compose command prefix (cached for the process lifetime)."""
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
    cli.add_command(upgrade)


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
@click.option(
    "--build/--no-build",
    default=None,
    help="Build images locally instead of pulling from GHCR (default: pull).",
)
@click.option(
    "--timeout", type=int, default=180, show_default=True, help="Health check timeout in seconds."
)
def up(
    detach: bool,
    addons: tuple[str, ...],
    port_strategy: str,
    compose_file: str | None,
    build: bool | None,
    timeout: int,
) -> None:
    """Start the Nexus stack.

    Reads nexus.yaml, resolves port conflicts, starts Docker Compose
    services, waits for health, and prints the service table.

    Portable stacks (installed via pip) pull the prebuilt image from
    GHCR — no local Docker build required.  Repo-checkout stacks
    (with ``build:`` directives) rebuild automatically.

    Use ``--build`` to force a local build, or ``--no-build`` to skip.

    Examples:
        nexus up                        # start from nexus.yaml
        nexus up --with nats            # add NATS event bus
        nexus up --port-strategy prompt # ask on conflicts
        nexus up --build                # force rebuild images
        nexus up --no-build             # skip rebuild
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

    # Default: pull prebuilt image.  Only build when explicitly requested
    # via --build (local dev iteration).
    if build is None:
        build = False

    # Build profiles list (single source of truth via _resolve_profiles)
    profiles = _resolve_profiles(config, addons)

    # Validate profiles against what the compose file actually defines.
    # This catches attempts to use repo-only add-ons (frontend, langgraph,
    # observability) with the portable bundled stack.
    available_profiles = _compose_profiles(cf)
    if available_profiles:
        missing = [p for p in profiles if p not in available_profiles]
        if missing:
            for p in missing:
                console.print(
                    f"  [yellow]Warning: profile '{p}' not found in {Path(cf).name}, skipping[/yellow]"
                )
            profiles = [p for p in profiles if p in available_profiles]

    # Port conflict resolution — check all ports in config (not filtered by services)
    ports = config.get("ports", {})
    active_services = config.get("services", [])
    resolved_ports, port_messages = resolve_ports(ports, strategy=port_strategy)

    # Print header
    console.print()
    console.print(f"[bold]Starting Nexus preset: {preset}[/bold]")
    console.print(f"  Using stack: {cf}")
    if build:
        console.print("  Image: [green]local build[/green] (from Dockerfile)")
    else:
        image_ref = _resolve_image_ref_from_config(config)
        if image_ref:
            console.print(f"  Image: {image_ref}")
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

    # Build environment from config (project name, ports, data dir, auth, image, TLS)
    compose_env = _derive_project_env(config, resolved_ports=resolved_ports)

    # When --build is requested, drop NEXUS_IMAGE_REF so docker compose
    # uses the ``build:`` directive from the compose file (local source)
    # instead of pulling the pinned remote image.
    if build:
        compose_env.pop("NEXUS_IMAGE_REF", None)

    data_dir = compose_env["NEXUS_HOST_DATA_DIR"]

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

    # Bootstrap auth: prefer the key from nexus.yaml (generated by nexus init),
    # fall back to .admin-api-key written by docker-entrypoint.sh.
    data_dir = config.get("data_dir", "./nexus-data")
    admin_api_key: str | None = config.get("api_key") or None
    if not admin_api_key:
        api_key_file = Path(data_dir) / ".admin-api-key"
        if api_key_file.exists():
            admin_api_key = api_key_file.read_text().strip()
            if admin_api_key:
                config["api_key"] = admin_api_key
                _save_project_config(config)

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

    # Surface the admin API key so the user can authenticate downstream
    if admin_api_key:
        console.print()
        console.print("[bold]Admin API key:[/bold]")
        console.print(f"  export NEXUS_API_KEY='{admin_api_key}'")
        console.print(f"  export NEXUS_URL='http://localhost:{http_port}'")

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
    profiles = _resolve_profiles(config)

    args: list[str] = ["down"]
    if volumes:
        args.append("--volumes")

    compose_env = _derive_project_env(config)

    console.print(f"[bold]Stopping Nexus preset: {preset}[/bold]")
    result = _run_compose(cf, profiles, *args, extra_env=compose_env)
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
    profiles = _resolve_profiles(config)
    compose_env = _derive_project_env(config)

    args: list[str] = ["logs", "--tail", str(tail)]
    if follow:
        args.append("--follow")
    if service:
        args.append(service)

    _run_compose(cf, profiles, *args, extra_env=compose_env)


@click.command()
@click.option(
    "--build/--no-build",
    default=None,
    help="Build images (auto-detected from compose file).",
)
def restart(build: bool | None) -> None:
    """Restart the Nexus stack.

    Equivalent to `nexus down && nexus up`.

    Examples:
        nexus restart           # restart services
        nexus restart --build   # force rebuild and restart
        nexus restart --no-build  # skip rebuild
    """
    config = _load_project_config()
    preset = config.get("preset", "local")

    if preset == "local":
        console.print("[yellow]Preset 'local' has no Docker services to restart.[/yellow]")
        raise SystemExit(0)

    cf = config.get("compose_file", "./nexus-stack.yml")
    profiles = _resolve_profiles(config)
    compose_env = _derive_project_env(config)

    # Default: pull prebuilt image (same as `up`)
    if build is None:
        build = False

    console.print(f"[bold]Restarting Nexus preset: {preset}[/bold]")
    _run_compose(cf, profiles, "down", extra_env=compose_env)

    args: list[str] = ["up", "-d"]
    if build:
        args.append("--build")
    result = _run_compose(cf, profiles, *args, extra_env=compose_env)
    if result.returncode == 0:
        console.print("[green]✓[/green] Stack restarted.")
    else:
        console.print("[red]Error:[/red] Failed to restart stack.")
        raise SystemExit(result.returncode)


@click.command()
@click.option(
    "--channel",
    default=None,
    help="Override the channel to resolve from (default: use config channel).",
)
@click.option(
    "--image-tag",
    default=None,
    help="Pin to an explicit tag instead of resolving from channel.",
)
@click.option(
    "--image-digest",
    default=None,
    help="Pin to an explicit digest (sha256:...).",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def upgrade(
    channel: str | None,
    image_tag: str | None,
    image_digest: str | None,
    yes: bool,
) -> None:
    """Upgrade the pinned image reference.

    Re-resolves the release channel to a new concrete image ref and
    updates nexus.yaml.  Does NOT restart the stack — run ``nexus restart``
    after reviewing the change.

    Examples:
        nexus upgrade                        # re-resolve stable channel
        nexus upgrade --channel edge         # switch to edge
        nexus upgrade --image-tag 0.10.0     # pin to specific version
        nexus upgrade --image-digest sha256:abc123...
    """
    from nexus.cli.commands.init_cmd import VALID_CHANNELS

    config = _load_project_config()
    preset = config.get("preset", "local")

    if preset == "local":
        console.print("[yellow]Preset 'local' does not use a prebuilt image.[/yellow]")
        raise SystemExit(0)

    # Validate --channel against known channels
    if channel and channel not in VALID_CHANNELS:
        console.print(
            f"[red]Error:[/red] Unknown channel '{channel}'. "
            f"Valid channels: {', '.join(VALID_CHANNELS)}"
        )
        raise SystemExit(1)

    # Warn if config is explicitly pinned (--image-tag or --image-digest at init)
    pin_mode = config.get("image_pin", "")
    if pin_mode and not (image_tag or image_digest or channel):
        console.print(
            f"[yellow]Warning:[/yellow] This config is pinned via {pin_mode}. "
            "Use --image-tag or --image-digest to change the pin, "
            "or --channel to switch to channel-following mode."
        )
        return

    current_ref = config.get("image_ref", config.get("image_tag", "(unknown)"))
    effective_channel = channel or config.get("image_channel", "stable")
    effective_accel = config.get("image_accelerator", "cpu")

    new_ref = _resolve_image_ref(
        effective_channel,
        effective_accel,
        image_tag=image_tag,
        image_digest=image_digest,
    )

    if new_ref == current_ref:
        console.print(f"[green]Already up to date:[/green] {current_ref}")
        return

    console.print("[bold]Image upgrade:[/bold]")
    console.print(f"  Current: {current_ref}")
    console.print(f"  New:     {new_ref}")
    console.print(f"  Channel: {effective_channel}")

    if not yes and not click.confirm("  Apply this change?", default=True):
        console.print("  Cancelled.")
        return

    config["image_ref"] = new_ref
    # Update pin/channel tracking
    if image_digest:
        config["image_pin"] = "digest"
        config.pop("image_channel", None)
    elif image_tag:
        config["image_pin"] = "tag"
        config.pop("image_channel", None)
    else:
        config["image_channel"] = effective_channel
        config.pop("image_pin", None)
    # Clean up deprecated image_tag if present
    config.pop("image_tag", None)

    _save_project_config(config)
    console.print(f"[green]✓[/green] Updated nexus.yaml → {new_ref}")
    console.print("  Run `nexus restart` to apply.")
