"""CLI commands for plugin management."""

import asyncio
import subprocess
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from nexus.plugins.registry import PluginRegistry

console = Console()


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from sync Click command context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


@click.group(name="plugins")
@click.pass_context
def plugins_cli(ctx: click.Context) -> None:
    """Manage Nexus plugins."""
    ctx.ensure_object(dict)
    if "registry" not in ctx.obj:
        registry = PluginRegistry()
        _run_async(registry.discover())
        ctx.obj["registry"] = registry


@plugins_cli.command(name="list")
@click.pass_context
def list_plugins(ctx: click.Context) -> None:
    """List all installed plugins."""
    registry: PluginRegistry = ctx.obj["registry"]
    plugins = registry.list_plugins()

    if not plugins:
        console.print("[yellow]No plugins installed.[/yellow]")
        console.print("\nInstall plugins with: pip install nexus-plugin-<name>")
        return

    table = Table(title="Installed Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Version", style="green")
    table.add_column("Description")
    table.add_column("Status", style="yellow")

    for metadata in plugins:
        plugin = registry.get_plugin_sync(metadata.name)
        status = ("Enabled" if plugin.is_enabled() else "Disabled") if plugin else "(not loaded)"
        table.add_row(metadata.name, metadata.version, metadata.description, status)

    console.print(table)


@plugins_cli.command(name="info")
@click.argument("plugin_name")
@click.pass_context
def plugin_info(ctx: click.Context, plugin_name: str) -> None:
    """Show detailed information about a plugin."""
    registry: PluginRegistry = ctx.obj["registry"]
    plugin = _run_async(registry.get_plugin(plugin_name))

    if not plugin:
        console.print(f"[red]Plugin '{plugin_name}' not found.[/red]")
        return

    metadata = plugin.metadata()

    console.print(f"\n[bold cyan]{metadata.name}[/bold cyan] v{metadata.version}")
    console.print(f"{metadata.description}\n")
    console.print(f"[bold]Author:[/bold] {metadata.author}")

    if metadata.homepage:
        console.print(f"[bold]Homepage:[/bold] {metadata.homepage}")

    if metadata.requires:
        console.print(f"[bold]Dependencies:[/bold] {', '.join(metadata.requires)}")

    commands = plugin.commands()
    if commands:
        console.print("\n[bold]Commands:[/bold]")
        for cmd_name in commands:
            console.print(f"  - nexus {plugin_name} {cmd_name}")

    hooks = plugin.hooks()
    if hooks:
        console.print("\n[bold]Hooks:[/bold]")
        for hook_name in hooks:
            console.print(f"  - {hook_name}")

    status = "Enabled" if plugin.is_enabled() else "Disabled"
    console.print(f"\n[bold]Status:[/bold] {status}")


@plugins_cli.command(name="enable")
@click.argument("plugin_name")
@click.pass_context
def enable_plugin(ctx: click.Context, plugin_name: str) -> None:
    """Enable a plugin."""
    registry: PluginRegistry = ctx.obj["registry"]
    plugin = _run_async(registry.get_plugin(plugin_name))

    if not plugin:
        console.print(f"[red]Plugin '{plugin_name}' not found.[/red]")
        return

    if plugin.is_enabled():
        console.print(f"[yellow]Plugin '{plugin_name}' is already enabled.[/yellow]")
        return

    registry.enable_plugin(plugin_name)
    console.print(f"[green]Enabled plugin '{plugin_name}'[/green]")


@plugins_cli.command(name="disable")
@click.argument("plugin_name")
@click.pass_context
def disable_plugin(ctx: click.Context, plugin_name: str) -> None:
    """Disable a plugin."""
    registry: PluginRegistry = ctx.obj["registry"]
    plugin = _run_async(registry.get_plugin(plugin_name))

    if not plugin:
        console.print(f"[red]Plugin '{plugin_name}' not found.[/red]")
        return

    if not plugin.is_enabled():
        console.print(f"[yellow]Plugin '{plugin_name}' is already disabled.[/yellow]")
        return

    registry.disable_plugin(plugin_name)
    console.print(f"[green]Disabled plugin '{plugin_name}'[/green]")


@plugins_cli.command(name="install")
@click.argument("plugin_name")
def install_plugin(plugin_name: str) -> None:
    """Install a plugin from PyPI.

    Example: nexus plugins install anthropic
    This will run: pip install nexus-plugin-anthropic
    """
    package_name = plugin_name
    if not package_name.startswith("nexus-plugin-"):
        package_name = f"nexus-plugin-{plugin_name}"

    console.print(f"Installing {package_name}...")

    try:
        subprocess.check_call(
            ["pip", "install", package_name], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        console.print(f"[green]Successfully installed {package_name}[/green]")
        console.print("\nRun 'nexus plugins list' to see the installed plugin")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Failed to install {package_name}[/red]")
        console.print(f"Error: {e.stderr.decode() if e.stderr else str(e)}")


@plugins_cli.command(name="uninstall")
@click.argument("plugin_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def uninstall_plugin(plugin_name: str, yes: bool) -> None:
    """Uninstall a plugin.

    Example: nexus plugins uninstall anthropic
    """
    package_name = plugin_name
    if not package_name.startswith("nexus-plugin-"):
        package_name = f"nexus-plugin-{plugin_name}"

    if not yes:
        confirmed = click.confirm(f"Uninstall {package_name}?")
        if not confirmed:
            console.print("Cancelled")
            return

    console.print(f"Uninstalling {package_name}...")

    try:
        subprocess.check_call(
            ["pip", "uninstall", "-y", package_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        console.print(f"[green]Successfully uninstalled {package_name}[/green]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Failed to uninstall {package_name}[/red]")
        console.print(f"Error: {e.stderr.decode() if e.stderr else str(e)}")


@plugins_cli.command(name="init")
@click.argument("name")
@click.option(
    "--type",
    "plugin_type",
    type=click.Choice(["generic", "storage", "parser"]),
    default="generic",
    help="Plugin type template",
)
@click.option("--author", default="Nexus Team", help="Plugin author name")
@click.option("--description", default="", help="Plugin description")
@click.option("--output-dir", type=click.Path(), default=".", help="Output directory")
def init_plugin(
    name: str, plugin_type: str, author: str, description: str, output_dir: str
) -> None:
    """Scaffold a new plugin project.

    Creates a complete plugin project structure with pyproject.toml,
    entry points, plugin class, tests, and README.

    Example: nexus plugins init my-backend --type=storage
    """
    from pathlib import Path

    from nexus.plugins.scaffold import PLUGIN_TYPES, scaffold_plugin

    console.print(f"Creating {plugin_type} plugin: [cyan]nexus-plugin-{name}[/cyan]")

    try:
        result = scaffold_plugin(
            name=name,
            output_dir=Path(output_dir),
            plugin_type=plugin_type,
            author=author,
            description=description,
        )

        console.print(f"\n[green]Created plugin scaffold at {result['project_dir']}[/green]")
        console.print(f"\n  Package: {result['package_name']}")
        console.print(f"  Module:  {result['module_name']}")
        console.print(f"  Class:   {result['class_name']}")
        console.print(f"  Type:    {PLUGIN_TYPES[plugin_type]}")
        console.print(f"\n  Files created ({len(result['files_created'])}):")
        for f in result["files_created"]:
            console.print(f"    {f}")

        console.print("\n[bold]Next steps:[/bold]")
        console.print(f"  cd {result['project_dir']}")
        console.print('  pip install -e ".[dev]"')
        console.print("  pytest tests/ -v")
        console.print("  nexus plugins list")

    except (ValueError, FileExistsError) as e:
        console.print(f"[red]Error: {e}[/red]")
    except Exception as e:
        console.print(f"[red]Failed to create plugin scaffold: {e}[/red]")
