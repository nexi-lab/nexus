"""Command-line interface for Nexus."""

from pathlib import Path

import click
from rich.console import Console

console = Console()


@click.group()
@click.version_option(version="0.1.0")
def main():
    """Nexus: AI-Native Distributed Filesystem"""
    pass


@main.command()
@click.option(
    "--mode", type=click.Choice(["embedded", "monolithic", "distributed"]), default="monolithic"
)
@click.option("--host", default="0.0.0.0", help="Host to bind to")
@click.option("--port", default=8080, help="Port to bind to")
@click.option("--config", type=click.Path(exists=True), help="Path to config file")
def server(mode: str, host: str, port: int, config: str | None):
    """Start Nexus server"""
    console.print(f"[bold green]Starting Nexus server in {mode} mode...[/bold green]")
    console.print(f"[dim]Host: {host}:{port}[/dim]")
    if config:
        console.print(f"[dim]Config: {config}[/dim]")

    # TODO: Implement server startup
    console.print("[yellow]Server not yet implemented[/yellow]")


@main.command()
@click.argument("data_dir", type=click.Path(), default="./nexus-data")
def init(data_dir: str):
    """Initialize a new Nexus workspace"""
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)

    # Create directory structure
    (path / "workspace").mkdir(exist_ok=True)
    (path / "shared").mkdir(exist_ok=True)

    console.print(f"[bold green]✓[/bold green] Initialized Nexus workspace at {path}")


@main.command()
@click.argument("source")
@click.argument("destination")
def copy(source: str, destination: str):
    """Copy files"""
    console.print(f"[dim]Copying {source} → {destination}[/dim]")
    # TODO: Implement
    console.print("[yellow]Not yet implemented[/yellow]")


@main.command()
@click.argument("path")
@click.option("--recursive", "-r", is_flag=True, help="List recursively")
def ls(path: str, recursive: bool):
    """List files"""
    console.print(f"[dim]Listing {path}[/dim]")
    # TODO: Implement
    console.print("[yellow]Not yet implemented[/yellow]")


@main.command()
@click.argument("query")
@click.option("--path", default="/", help="Base path to search")
@click.option("--limit", default=10, help="Maximum results")
def search(query: str, path: str, limit: int):
    """Semantic search"""
    console.print(f"[dim]Searching for: {query}[/dim]")
    # TODO: Implement
    console.print("[yellow]Not yet implemented[/yellow]")


if __name__ == "__main__":
    main()
