"""CLI commands for plugin management."""

import click
from rich.console import Console
from rich.table import Table

from nexus.plugins.registry import PluginRegistry

console = Console()


@click.group(name="plugins")
def plugins_cli() -> None:
    """Manage Nexus plugins."""
    pass


@plugins_cli.command(name="list")
def list_plugins() -> None:
    """List all installed plugins."""
    registry = PluginRegistry()
    plugins = registry.discover()

    if not plugins:
        console.print("[yellow]No plugins installed.[/yellow]")
        console.print("\nInstall plugins with: pip install nexus-plugin-<name>")
        return

    table = Table(title="Installed Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Version", style="green")
    table.add_column("Description")
    table.add_column("Status", style="yellow")

    for plugin_name in plugins:
        plugin = registry.get_plugin(plugin_name)
        if plugin:
            metadata = plugin.metadata()
            status = "✓ Enabled" if plugin.is_enabled() else "✗ Disabled"
            table.add_row(metadata.name, metadata.version, metadata.description, status)

    console.print(table)


@plugins_cli.command(name="info")
@click.argument("plugin_name")
def plugin_info(plugin_name: str) -> None:
    """Show detailed information about a plugin."""
    registry = PluginRegistry()
    registry.discover()

    plugin = registry.get_plugin(plugin_name)
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

    # Show commands
    commands = plugin.commands()
    if commands:
        console.print("\n[bold]Commands:[/bold]")
        for cmd_name in commands:
            console.print(f"  • nexus {plugin_name} {cmd_name}")

    # Show hooks
    hooks = plugin.hooks()
    if hooks:
        console.print("\n[bold]Hooks:[/bold]")
        for hook_name in hooks:
            console.print(f"  • {hook_name}")

    status = "✓ Enabled" if plugin.is_enabled() else "✗ Disabled"
    console.print(f"\n[bold]Status:[/bold] {status}")


@plugins_cli.command(name="enable")
@click.argument("plugin_name")
def enable_plugin(plugin_name: str) -> None:
    """Enable a plugin."""
    registry = PluginRegistry()
    registry.discover()

    plugin = registry.get_plugin(plugin_name)
    if not plugin:
        console.print(f"[red]Plugin '{plugin_name}' not found.[/red]")
        return

    if plugin.is_enabled():
        console.print(f"[yellow]Plugin '{plugin_name}' is already enabled.[/yellow]")
        return

    registry.enable_plugin(plugin_name)
    console.print(f"[green]✓ Enabled plugin '{plugin_name}'[/green]")


@plugins_cli.command(name="disable")
@click.argument("plugin_name")
def disable_plugin(plugin_name: str) -> None:
    """Disable a plugin."""
    registry = PluginRegistry()
    registry.discover()

    plugin = registry.get_plugin(plugin_name)
    if not plugin:
        console.print(f"[red]Plugin '{plugin_name}' not found.[/red]")
        return

    if not plugin.is_enabled():
        console.print(f"[yellow]Plugin '{plugin_name}' is already disabled.[/yellow]")
        return

    registry.disable_plugin(plugin_name)
    console.print(f"[green]✓ Disabled plugin '{plugin_name}'[/green]")


@plugins_cli.command(name="install")
@click.argument("plugin_name")
def install_plugin(plugin_name: str) -> None:
    """Install a plugin from PyPI.

    Example: nexus plugins install anthropic
    This will run: pip install nexus-plugin-anthropic
    """
    import subprocess

    # Convert short name to full package name
    package_name = plugin_name
    if not package_name.startswith("nexus-plugin-"):
        package_name = f"nexus-plugin-{plugin_name}"

    console.print(f"Installing {package_name}...")

    try:
        subprocess.check_call(
            ["pip", "install", package_name], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        console.print(f"[green]✓ Successfully installed {package_name}[/green]")
        console.print("\nRun 'nexus plugins list' to see the installed plugin")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]✗ Failed to install {package_name}[/red]")
        console.print(f"Error: {e.stderr.decode() if e.stderr else str(e)}")


@plugins_cli.command(name="uninstall")
@click.argument("plugin_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def uninstall_plugin(plugin_name: str, yes: bool) -> None:
    """Uninstall a plugin.

    Example: nexus plugins uninstall anthropic
    """
    import subprocess

    # Convert short name to full package name
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
        console.print(f"[green]✓ Successfully uninstalled {package_name}[/green]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]✗ Failed to uninstall {package_name}[/red]")
        console.print(f"Error: {e.stderr.decode() if e.stderr else str(e)}")
