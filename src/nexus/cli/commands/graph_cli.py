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
        from nexus.cli.theme import console

        ent = d.get("entity", d)
        console.print(f"[bold nexus.value]Entity: {entity_id}[/bold nexus.value]")
        console.print(f"  Type:   {ent.get('type', 'N/A')}")
        console.print(f"  Label:  {ent.get('label', ent.get('name', 'N/A'))}")
        props = ent.get("properties", {})
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

        from nexus.cli.theme import console

        neighbors = d.get("neighbors", [])
        if not neighbors:
            console.print(f"[nexus.warning]No neighbors within {hops} hop(s)[/nexus.warning]")
            return

        table = Table(title=f"Neighbors of {entity_id} ({hops} hop(s), {len(neighbors)} found)")
        table.add_column("Entity ID", style="nexus.muted")
        table.add_column("Type")
        table.add_column("Label")
        table.add_column("Depth")

        for n in neighbors:
            ent = n.get("entity", n)
            table.add_row(
                ent.get("entity_id", ""),
                ent.get("type", ""),
                ent.get("label", ent.get("name", "")),
                str(n.get("depth", "")),
            )
        console.print(table)

    return ServiceResult(data=data, human_formatter=_render)


@graph.command("subgraph")
@click.argument("entity_ids", nargs=-1, required=True)
@click.option("--max-hops", default=2, show_default=True, help="Maximum hops (1-5)")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=GraphClient)
def graph_subgraph(
    client: GraphClient, entity_ids: tuple[str, ...], max_hops: int
) -> ServiceResult:
    """Extract subgraph around one or more entities.

    \b
    Examples:
        nexus graph subgraph ent_123
        nexus graph subgraph ent_123 ent_456 --max-hops 3 --json
    """
    data = client.subgraph(list(entity_ids), max_hops=max_hops)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        nodes = d.get("nodes", [])
        edges = d.get("edges", [])
        console.print(f"[bold nexus.value]Subgraph ({len(entity_ids)} seed(s))[/bold nexus.value]")
        console.print(f"  Nodes: {len(nodes)}")
        console.print(f"  Edges: {len(edges)}")

    return ServiceResult(data=data, human_formatter=_render)


@graph.command("search")
@click.argument("name")
@click.option("--entity-type", default=None, help="Filter by entity type")
@click.option("--fuzzy/--no-fuzzy", default=False, help="Enable fuzzy matching")
@add_output_options
@REMOTE_API_KEY_OPTION
@REMOTE_URL_OPTION
@service_command(client_class=GraphClient)
def graph_search(
    client: GraphClient, name: str, entity_type: str | None, fuzzy: bool
) -> ServiceResult:
    """Search entities by name.

    \b
    Examples:
        nexus graph search "machine learning"
        nexus graph search "agent" --entity-type agent --fuzzy --json
    """
    data = client.search(name, entity_type=entity_type, fuzzy=fuzzy)

    def _render(d: dict) -> None:
        from nexus.cli.theme import console

        # Server may return a single entity or None
        entity = d.get("entity")
        if entity is None:
            console.print("[nexus.warning]No matching entities[/nexus.warning]")
            return

        console.print("[bold nexus.value]Search Result[/bold nexus.value]")
        console.print(f"  Entity ID: {entity.get('entity_id', entity.get('id', 'N/A'))}")
        console.print(f"  Type:      {entity.get('type', 'N/A')}")
        console.print(f"  Name:      {entity.get('name', 'N/A')}")

    return ServiceResult(data=data, human_formatter=_render)
