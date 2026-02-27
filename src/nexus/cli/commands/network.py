"""Nexus CLI Network Commands — WireGuard mesh for federation.

Subcommands:
    network init         Generate WireGuard identity for this node
    network add-peer     Register a federation peer
    network remove-peer  Remove a peer
    network config       Show generated wg-quick config
    network up           Bring up WireGuard tunnel
    network down         Tear down tunnel
    network status       Show tunnel status
"""

import click
from rich.panel import Panel
from rich.table import Table

from nexus.cli.utils import console


@click.group()
def network() -> None:
    """WireGuard mesh network for federation.

    Creates encrypted tunnels between Nexus nodes for cross-machine
    federation.  IP scheme: 10.99.0.{node_id}/24.

    Requires WireGuard installed:
      Windows: winget install WireGuard.WireGuard
      macOS:   brew install wireguard-tools
      Linux:   apt install wireguard-tools
    """
    pass


@network.command()
@click.option(
    "--node-id",
    required=True,
    type=click.IntRange(1, 254),
    help="Unique node ID (1-254). Determines WireGuard IP: 10.99.0.{node_id}",
)
@click.option(
    "--listen-port",
    default=51820,
    type=int,
    show_default=True,
    help="WireGuard listen port (UDP).",
)
def init(node_id: int, listen_port: int) -> None:
    """Generate WireGuard identity for this node.

    Creates a keypair and saves to ~/.nexus/network/identity.json.
    Share the public key with peers to establish tunnels.
    """
    from nexus.network.wireguard import init_identity

    try:
        identity = init_identity(node_id, listen_port)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e

    console.print(
        Panel.fit(
            f"[bold green]Node initialized[/bold green]\n\n"
            f"  Node ID:      {identity['node_id']}\n"
            f"  WireGuard IP: {identity['ip']}\n"
            f"  Listen Port:  {identity['listen_port']}\n"
            f"  Public Key:   [cyan]{identity['public_key']}[/cyan]\n\n"
            f"[dim]Share the public key with peers.[/dim]",
            title="nexus network",
        )
    )


@network.command(name="add-peer")
@click.option(
    "--node-id",
    required=True,
    type=click.IntRange(1, 254),
    help="Peer's node ID (1-254).",
)
@click.option(
    "--public-key",
    required=True,
    help="Peer's WireGuard public key.",
)
@click.option(
    "--endpoint",
    required=True,
    help="Peer's reachable address (ip:port), e.g. 192.168.1.50:51820",
)
def add_peer(node_id: int, public_key: str, endpoint: str) -> None:
    """Register a federation peer."""
    from nexus.network.wireguard import add_peer as _add_peer

    peer = _add_peer(node_id, public_key, endpoint)
    console.print(
        f"[green]Peer added:[/green] node={peer['node_id']} "
        f"ip={peer['ip']} endpoint={peer['endpoint']}"
    )


@network.command(name="remove-peer")
@click.option(
    "--node-id",
    required=True,
    type=click.IntRange(1, 254),
    help="Peer's node ID to remove.",
)
def remove_peer(node_id: int) -> None:
    """Remove a registered peer."""
    from nexus.network.wireguard import remove_peer as _remove_peer

    if _remove_peer(node_id):
        console.print(f"[green]Peer {node_id} removed.[/green]")
    else:
        console.print(f"[yellow]Peer {node_id} not found.[/yellow]")


@network.command()
def config() -> None:
    """Show the generated wg-quick config (without activating)."""
    from nexus.network.wireguard import generate_wg_config, load_identity, load_peers

    try:
        identity = load_identity()
        peers = load_peers()
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e

    wg_config = generate_wg_config(identity, peers)
    console.print(Panel(wg_config, title=f"wg-quick config (node {identity['node_id']})"))


@network.command()
def up() -> None:
    """Bring up the WireGuard tunnel.

    Requires administrator/sudo privileges.
    """
    from nexus.network.wireguard import tunnel_up

    try:
        msg = tunnel_up()
        console.print(f"[green]{msg}[/green]")
    except (RuntimeError, FileNotFoundError) as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e


@network.command()
def down() -> None:
    """Tear down the WireGuard tunnel."""
    from nexus.network.wireguard import tunnel_down

    try:
        msg = tunnel_down()
        console.print(f"[green]{msg}[/green]")
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e


@network.command()
def status() -> None:
    """Show WireGuard tunnel status and peer info."""
    from nexus.network.wireguard import load_identity, load_peers, tunnel_status

    # Show local identity
    try:
        identity = load_identity()
        console.print(
            f"[bold]Local node:[/bold] id={identity['node_id']} "
            f"ip={identity['ip']} port={identity['listen_port']}"
        )
    except FileNotFoundError:
        console.print("[yellow]No identity configured. Run `nexus network init` first.[/yellow]")
        return

    # Show configured peers
    peers = load_peers()
    if peers:
        table = Table(title="Configured Peers")
        table.add_column("Node ID", style="cyan")
        table.add_column("WireGuard IP")
        table.add_column("Endpoint")
        table.add_column("Public Key")
        for peer in peers:
            table.add_row(
                str(peer["node_id"]),
                peer["ip"],
                peer["endpoint"],
                peer["public_key"][:20] + "...",
            )
        console.print(table)
    else:
        console.print("[dim]No peers configured.[/dim]")

    # Show live tunnel status
    console.print()
    console.print("[bold]Tunnel status:[/bold]")
    wg_output = tunnel_status()
    console.print(wg_output)


def register_commands(cli: click.Group) -> None:
    """Register network commands to the main CLI."""
    cli.add_command(network)
