"""Knowledge graph CLI commands — entity queries and traversal.

Maps to /api/v2/graph/* endpoints via GraphClient.
Issue #2812.
"""

from __future__ import annotations

import click

from nexus.cli.clients.graph import GraphClient
from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION


@click.group()
def graph() -> None:
    """Knowledge graph queries.

    \b
    Query entities, traverse neighbors, and search the knowledge graph.

    \b
    Examples:
        nexus graph entity ent_123 --json
        nexus graph neighbors ent_123 --hops 2
        nexus graph search "machine learning"
    """


@graph.command("entity")
@click.argument("entity_id")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=GraphClient)
def graph_entity(client: GraphClient, entity_id: str) -> ServiceResult:
    """Get entity details from the knowledge graph.

    \b
    Examples:
        nexus graph entity ent_123
        nexus graph entity ent_123 --json
    """
    data = client.entity(entity_id)

    def _render(d: dict) -> None:
        from nexus.cli.utils import console

        console.print(f"[bold cyan]Entity: {entity_id}[/bold cyan]")
        console.print(f"  Type:   {d.get('type', 'N/A')}")
        console.print(f"  Label:  {d.get('label', d.get('name', 'N/A'))}")
        props = d.get("properties", {})
        if props:
            console.print("  Properties:")
            for k, v in list(props.items())[:10]:
                console.print(f"    {k}: {v}")

    return ServiceResult(data=data, human_formatter=_render)


@graph.command("neighbors")
@click.argument("entity_id")
@click.option("--hops", default=1, show_default=True, help="Number of hops for neighbor traversal")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=GraphClient)
def graph_neighbors(client: GraphClient, entity_id: str, hops: int) -> ServiceResult:
    """N-hop neighbor traversal from an entity.

    \b
    Examples:
        nexus graph neighbors ent_123
        nexus graph neighbors ent_123 --hops 2 --json
    """
    data = client.neighbors(entity_id, hops=hops)

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.utils import console

        neighbors = d.get("neighbors", [])
        if not neighbors:
            console.print(f"[yellow]No neighbors within {hops} hop(s)[/yellow]")
            return

        table = Table(title=f"Neighbors of {entity_id} ({hops} hop(s), {len(neighbors)} found)")
        table.add_column("Entity ID", style="dim")
        table.add_column("Type")
        table.add_column("Label")
        table.add_column("Relation")

        for n in neighbors:
            table.add_row(
                n.get("entity_id", ""),
                n.get("type", ""),
                n.get("label", n.get("name", "")),
                n.get("relation", ""),
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


@graph.command("subgraph")
@click.argument("entity_id")
@click.option("--depth", default=2, show_default=True, help="Subgraph extraction depth")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=GraphClient)
def graph_subgraph(client: GraphClient, entity_id: str, depth: int) -> ServiceResult:
    """Extract subgraph around an entity.

    \b
    Examples:
        nexus graph subgraph ent_123
        nexus graph subgraph ent_123 --depth 3 --json
    """
    data = client.subgraph(entity_id, depth=depth)

    def _render(d: dict) -> None:
        from nexus.cli.utils import console

        nodes = d.get("nodes", [])
        edges = d.get("edges", [])
        console.print(f"[bold cyan]Subgraph around {entity_id}[/bold cyan]")
        console.print(f"  Nodes: {len(nodes)}")
        console.print(f"  Edges: {len(edges)}")

    return ServiceResult(data=data, human_formatter=_render)


@graph.command("search")
@click.argument("query")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=GraphClient)
def graph_search(client: GraphClient, query: str) -> ServiceResult:
    """Search entities by embedding similarity.

    \b
    Examples:
        nexus graph search "machine learning"
        nexus graph search "agent collaboration" --json
    """
    data = client.search(query)

    def _render(d: dict) -> None:
        from rich.table import Table

        from nexus.cli.utils import console

        results = d.get("results", d.get("entities", []))
        if not results:
            console.print("[yellow]No matching entities[/yellow]")
            return

        table = Table(title=f"Search Results ({len(results)})")
        table.add_column("Entity ID", style="dim")
        table.add_column("Type")
        table.add_column("Label")
        table.add_column("Score", justify="right", style="green")

        for r in results:
            table.add_row(
                r.get("entity_id", ""),
                r.get("type", ""),
                r.get("label", r.get("name", "")),
                f"{r.get('score', 0):.3f}" if r.get("score") is not None else "",
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)
