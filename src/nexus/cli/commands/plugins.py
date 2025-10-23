"""Plugin System commands - manage Nexus plugins."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import subprocess
from typing import Any, cast

import click
from rich.table import Table

from nexus.cli.utils import console, handle_error


def register_commands(cli: click.Group) -> None:
    """Register all plugin commands."""
    cli.add_command(plugins)
    # Register dynamic plugin commands (firecrawl, anthropic, etc.)
    _register_plugin_commands(cli)


@click.group(name="plugins")
def plugins() -> None:
    """Plugin System - Manage Nexus plugins.

    The Plugin System allows extending Nexus with external integrations
    while maintaining vendor neutrality:
    - Entry point-based plugin discovery
    - Custom CLI commands via `nexus <plugin> <command>`
    - Lifecycle hooks (before_write, after_read, etc.)
    - Per-plugin configuration
    - Enable/disable plugins dynamically

    Examples:
        nexus plugins list
        nexus plugins info anthropic
        nexus plugins install anthropic
        nexus plugins enable anthropic
        nexus plugins disable anthropic
        nexus plugins uninstall anthropic
    """
    pass


@plugins.command(name="list")
def plugins_list() -> None:
    """List all installed plugins."""
    try:
        from nexus.plugins.registry import PluginRegistry

        registry = PluginRegistry()
        plugin_names = registry.discover()

        if not plugin_names:
            console.print("[yellow]No plugins installed.[/yellow]")
            console.print("\nInstall plugins with: [cyan]pip install nexus-plugin-<name>[/cyan]")
            return

        table = Table(title="Installed Plugins")
        table.add_column("Name", style="cyan")
        table.add_column("Version", style="green")
        table.add_column("Description")
        table.add_column("Status", style="yellow")

        for plugin_name in plugin_names:
            plugin = registry.get_plugin(plugin_name)
            if plugin:
                metadata = plugin.metadata()
                status = "✓ Enabled" if plugin.is_enabled() else "✗ Disabled"
                table.add_row(metadata.name, metadata.version, metadata.description, status)

        console.print(table)

    except Exception as e:
        handle_error(e)


@plugins.command(name="info")
@click.argument("plugin_name")
def plugins_info(plugin_name: str) -> None:
    """Show detailed information about a plugin."""
    try:
        from nexus.plugins.registry import PluginRegistry

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

    except Exception as e:
        handle_error(e)


@plugins.command(name="enable")
@click.argument("plugin_name")
def plugins_enable(plugin_name: str) -> None:
    """Enable a plugin."""
    try:
        from nexus.plugins.registry import PluginRegistry

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

    except Exception as e:
        handle_error(e)


@plugins.command(name="disable")
@click.argument("plugin_name")
def plugins_disable(plugin_name: str) -> None:
    """Disable a plugin."""
    try:
        from nexus.plugins.registry import PluginRegistry

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

    except Exception as e:
        handle_error(e)


@plugins.command(name="install")
@click.argument("plugin_name")
def plugins_install(plugin_name: str) -> None:
    """Install a plugin from PyPI.

    Example: nexus plugins install anthropic
    This will run: pip install nexus-plugin-anthropic
    """
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
        console.print("\nRun [cyan]'nexus plugins list'[/cyan] to see the installed plugin")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]✗ Failed to install {package_name}[/red]")
        console.print(f"Error: {e.stderr.decode() if e.stderr else str(e)}")


@plugins.command(name="uninstall")
@click.argument("plugin_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def plugins_uninstall(plugin_name: str, yes: bool) -> None:
    """Uninstall a plugin.

    Example: nexus plugins uninstall anthropic
    """
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


# Dynamic plugin command registration
def _register_plugin_commands(main: click.Group) -> None:
    """Dynamically register plugin commands at CLI initialization."""
    try:
        from nexus.plugins.registry import PluginRegistry

        # Discover plugins without NexusFS (for metadata only)
        registry = PluginRegistry()
        plugin_names = registry.discover()

        for plugin_name in plugin_names:
            plugin = registry.get_plugin(plugin_name)
            if not plugin or not plugin.is_enabled():
                continue

            # Get plugin commands
            commands = plugin.commands()
            if not commands:
                continue

            # Get plugin class for later instantiation
            import importlib.metadata

            entry_points = importlib.metadata.entry_points()
            if hasattr(entry_points, "select"):
                nexus_plugins = entry_points.select(group="nexus.plugins")
            else:
                result = entry_points.get("nexus.plugins")
                nexus_plugins = cast(Any, result if result else [])

            plugin_class = None
            for ep in nexus_plugins:
                if ep.name == plugin_name:
                    plugin_class = ep.load()
                    break

            if not plugin_class:
                continue

            # Create a Click group for this plugin
            @click.group(name=plugin_name)
            def plugin_group() -> None:
                """Plugin commands."""
                pass

            # Update the docstring with plugin description
            metadata = plugin.metadata()
            plugin_group.__doc__ = (
                f"{metadata.description}\n\nPlugin: {metadata.name} v{metadata.version}"
            )

            # Add each command to the plugin group
            for cmd_name, cmd_func in commands.items():
                # Create a wrapper that handles async commands with NexusFS
                def make_command(func: Any, name: str, _p_class: Any, p_name: str) -> Any:
                    # Preserve the original function's signature
                    sig = inspect.signature(func)
                    params: list[Any] = []

                    for param_name, param in sig.parameters.items():
                        if param_name == "self":
                            continue

                        # Create Click option/argument based on parameter
                        if param.default == inspect.Parameter.empty:
                            # Required argument
                            params.append(click.Argument([param_name]))
                        else:
                            # Optional option
                            option_name = f"--{param_name.replace('_', '-')}"
                            params.append(
                                click.Option(
                                    [option_name],
                                    default=param.default,
                                    help=f"{param_name} parameter",
                                )
                            )

                    @click.command(name=name, params=params)
                    @click.pass_context
                    def wrapper(_ctx: Any, **kwargs: Any) -> None:
                        """Execute plugin command."""
                        nx = None
                        try:
                            # Initialize NexusFS for commands that need it
                            from nexus import connect
                            from nexus.plugins.registry import PluginRegistry

                            nx = connect()

                            # Re-instantiate plugin with NexusFS
                            plugin_registry = PluginRegistry(nx)  # type: ignore[arg-type]
                            plugin_registry.discover()
                            plugin_instance = plugin_registry.get_plugin(p_name)

                            if not plugin_instance:
                                console.print(f"[red]Plugin '{p_name}' not found[/red]")
                                return

                            # Get the command method from the plugin instance
                            cmd_method = plugin_instance.commands().get(name)
                            if not cmd_method:
                                console.print(f"[red]Command '{name}' not found[/red]")
                                return

                            # Handle async functions
                            if inspect.iscoroutinefunction(cmd_method):
                                asyncio.run(cmd_method(**kwargs))
                            else:
                                cmd_method(**kwargs)

                        except Exception as e:
                            handle_error(e)
                        finally:
                            # Clean up NexusFS connection
                            if nx:
                                with contextlib.suppress(BaseException):
                                    nx.close()

                    # Preserve docstring
                    wrapper.__doc__ = func.__doc__ or f"{name} command"
                    return wrapper

                cmd = make_command(cmd_func, cmd_name, plugin_class, plugin_name)
                plugin_group.add_command(cmd)

            # Add the plugin group to main CLI
            main.add_command(plugin_group)

    except Exception as e:
        # Silently fail if plugin system is not available
        # This allows the CLI to work even if plugins are broken
        import logging

        logging.debug(f"Failed to register plugin commands: {e}")
