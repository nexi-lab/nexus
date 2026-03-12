"""Nexus init command — initialize a project with a preset.

Presets:
  local   — embedded-only quickstart, no daemon, no Docker
  shared  — one shared node with Postgres, Dragonfly, Zoekt
  demo    — shared node + demo-ready seed data settings

Writes a project-local ``nexus.yaml`` with all defaults materialized.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click
import yaml

from nexus.cli.port_utils import DEFAULT_PORTS
from nexus.cli.utils import console

# ---------------------------------------------------------------------------
# Preset definitions
# ---------------------------------------------------------------------------

VALID_PRESETS = ("local", "shared", "demo")

# Services activated per preset (maps to Docker Compose profiles)
PRESET_SERVICES: dict[str, list[str]] = {
    "local": [],
    "shared": ["nexus", "postgres", "dragonfly", "zoekt"],
    "demo": ["nexus", "postgres", "dragonfly", "zoekt"],
}

# Default auth mode per preset
PRESET_AUTH: dict[str, str] = {
    "local": "none",
    "shared": "static",
    "demo": "database",
}

# Compose profiles activated per preset
PRESET_COMPOSE_PROFILES: dict[str, list[str]] = {
    "local": [],
    "shared": ["core", "cache", "search"],
    "demo": ["core", "cache", "search"],
}

# Port keys relevant to each preset
PRESET_PORT_KEYS: dict[str, list[str]] = {
    "local": [],
    "shared": ["http", "grpc", "postgres", "dragonfly", "zoekt"],
    "demo": ["http", "grpc", "postgres", "dragonfly", "zoekt"],
}


def _build_config(
    preset: str,
    data_dir: str,
    tls: bool,
    ports: dict[str, int],
    addons: tuple[str, ...],
) -> dict[str, Any]:
    """Build the materialized nexus.yaml config dict."""
    config: dict[str, Any] = {
        "preset": preset,
        "data_dir": data_dir,
        "auth": PRESET_AUTH[preset],
        "tls": tls,
    }

    if preset != "local":
        # Only include services/ports/compose for non-local presets
        config["services"] = list(PRESET_SERVICES[preset])
        config["ports"] = {k: v for k, v in ports.items() if k in PRESET_PORT_KEYS[preset]}
        config["compose_profiles"] = list(PRESET_COMPOSE_PROFILES[preset])
        config["compose_file"] = "./nexus-stack.yml"

    if addons:
        config.setdefault("addons", []).extend(addons)

    if tls:
        config["tls_dir"] = os.path.join(data_dir, "tls")
        config["tls_cert"] = os.path.join(data_dir, "tls", "server.crt")
        config["tls_key"] = os.path.join(data_dir, "tls", "server.key")
        config["tls_ca"] = os.path.join(data_dir, "tls", "ca.crt")

    return config


def _write_config(config: dict[str, Any], config_path: Path) -> None:
    """Write the nexus.yaml config file."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def _create_data_dirs(data_dir: Path) -> None:
    """Create the data directory structure."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "cas").mkdir(exist_ok=True)


def _print_local_summary(config_path: Path, data_dir: Path) -> None:
    """Print summary for local preset."""
    console.print()
    console.print("[green]Initialized Nexus preset: local[/green]")
    console.print(f"  Config:   {config_path}")
    console.print(f"  Data dir: {data_dir}")
    console.print()
    console.print("[bold]Next step:[/bold]")
    console.print("  nexus doctor")


def _print_shared_summary(config: dict[str, Any], config_path: Path, data_dir: Path) -> None:
    """Print summary for shared/demo presets."""
    preset = config["preset"]
    services = ", ".join(config.get("services", []))
    ports = config.get("ports", {})

    console.print()
    console.print(f"[green]Initialized Nexus preset: {preset}[/green]")
    console.print(f"  Config:     {config_path}")
    console.print(f"  Data dir:   {data_dir}")
    console.print(f"  Services:   {services}")
    if config.get("tls"):
        console.print("  TLS:        enabled")
        console.print(f"  TLS dir:    {config.get('tls_dir', '')}")
    console.print(f"  Auth:       {config.get('auth', 'static')}")
    if ports:
        console.print(f"  HTTP port:  {ports.get('http', DEFAULT_PORTS['http'])}")
        console.print(f"  gRPC port:  {ports.get('grpc', DEFAULT_PORTS['grpc'])}")
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  nexus up")
    if preset == "demo":
        console.print("  nexus demo init")


@click.command(name="init")
@click.option(
    "--preset",
    type=click.Choice(VALID_PRESETS),
    default="local",
    show_default=True,
    help="Initialization preset: local (embedded), shared (one node), demo (shared + seed data).",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    default="./nexus-data",
    show_default=True,
    help="Path to the data directory.",
)
@click.option(
    "--tls",
    is_flag=True,
    default=False,
    help="Enable TLS (scaffolds certificate material).",
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
    "--config-path",
    type=click.Path(),
    default="./nexus.yaml",
    show_default=True,
    help="Path for the generated config file.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing nexus.yaml without prompting.",
)
def init(
    preset: str,
    data_dir: str,
    tls: bool,
    addons: tuple[str, ...],
    config_path: str,
    force: bool,
) -> None:
    """Initialize a new Nexus project with a preset.

    Creates data directories, writes a nexus.yaml config file, and prints
    the next command to run.

    Examples:
        nexus init                         # local embedded (default)
        nexus init --preset shared         # one shared node
        nexus init --preset shared --tls   # secure shared node
        nexus init --preset demo           # demo-ready with seed settings
        nexus init --preset shared --with nats --with mcp
    """
    cfg_path = Path(config_path)
    d_dir = Path(data_dir)

    # Guard: don't overwrite existing config without --force
    if cfg_path.exists() and not force:
        console.print(
            f"[yellow]Warning:[/yellow] {cfg_path} already exists. Use --force to overwrite."
        )
        raise SystemExit(1)

    # Build ports dict from defaults
    ports = dict(DEFAULT_PORTS)

    # Build and write config
    config = _build_config(preset, str(d_dir), tls, ports, addons)
    _write_config(config, cfg_path)

    # Create data directories
    _create_data_dirs(d_dir)

    # Validate compose file exists for non-local presets
    if preset != "local":
        compose_file = config.get("compose_file", "./nexus-stack.yml")
        if not Path(compose_file).exists():
            console.print(f"[yellow]Warning:[/yellow] Compose file not found: {compose_file}")
            console.print("  The stack requires nexus-stack.yml in the project root.")
            console.print("  If you cloned the Nexus repo, run `nexus init` from the repo root.")

    # Initialize local Nexus workspace for 'local' preset
    if preset == "local":
        try:
            import nexus

            nx = nexus.connect(config={"data_dir": str(d_dir)})
            nx.sys_mkdir("/workspace", exist_ok=True)
            nx.sys_mkdir("/shared", exist_ok=True)
            nx.close()
        except Exception:
            # Non-fatal — directories were already created
            pass
        _print_local_summary(cfg_path, d_dir)
    else:
        _print_shared_summary(config, cfg_path, d_dir)
