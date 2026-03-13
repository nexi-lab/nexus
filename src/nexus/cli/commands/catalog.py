"""Catalog CLI commands -- schema extraction and column search (Issue #2930).

Examples:
    nexus catalog schema /workspace/demo/data/sales.csv
    nexus catalog search --column amount
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import click

from nexus.cli.utils import add_backend_options, console

logger = logging.getLogger(__name__)


def register_commands(cli: click.Group) -> None:
    """Register catalog commands."""
    cli.add_command(catalog)


@click.group(name="catalog")
def catalog() -> None:
    """Data catalog operations -- schema extraction and search."""


@catalog.command(name="schema")
@click.argument("path")
@add_backend_options
def catalog_schema(path: str, remote_url: str | None, remote_api_key: str | None) -> None:
    """Show extracted schema for a data file.

    Extracts or retrieves the schema (columns, types, row count) for a
    structured data file (CSV, JSON, Parquet).

    Example:
        nexus catalog schema /workspace/demo/data/sales.csv
    """
    from nexus.cli.api_client import get_api_client_from_options

    client = get_api_client_from_options(remote_url, remote_api_key)
    encoded_path = quote(path.lstrip("/"), safe="")

    try:
        result: dict[str, Any] = client.get(f"/api/v2/catalog/schema/{encoded_path}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e

    schema: dict[str, Any] | None = result.get("schema")
    if schema is None:
        console.print(f"[yellow]No schema available for {path}[/yellow]")
        return

    console.print(f"[bold]Schema for {path}[/bold]")
    console.print(f"  URN:    {result.get('entity_urn', 'n/a')}")
    console.print(f"  Format: {schema.get('format', 'unknown')}")
    if schema.get("row_count") is not None:
        console.print(f"  Rows:   {schema['row_count']}")
    console.print(f"  Confidence: {schema.get('confidence', 'n/a')}")
    console.print()

    columns: list[dict[str, Any]] = schema.get("columns", [])
    if columns:
        console.print("  Columns:")
        for col in columns:
            nullable = " (nullable)" if col.get("nullable", "False").lower() == "true" else ""
            console.print(f"    {col['name']:20s} {col['type']}{nullable}")
    else:
        console.print("  [dim]No columns detected[/dim]")


@catalog.command(name="search")
@click.option("--column", "-c", required=True, help="Column name to search for")
@add_backend_options
def catalog_search(column: str, remote_url: str | None, remote_api_key: str | None) -> None:
    """Find files containing a specific column.

    Searches all cataloged data files for columns matching the given name.

    Example:
        nexus catalog search --column amount
    """
    from nexus.cli.api_client import get_api_client_from_options

    client = get_api_client_from_options(remote_url, remote_api_key)

    try:
        result: dict[str, Any] = client.get("/api/v2/catalog/search", params={"column": column})
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e

    results: list[dict[str, Any]] = result.get("results", [])
    if not results:
        console.print(f"[yellow]No files found with column '{column}'[/yellow]")
        return

    console.print(f"[bold]Files containing column '{column}':[/bold]")
    for r in results:
        console.print(f"  {r['entity_urn']}")
        console.print(f"    Column: {r['column_name']} ({r['column_type']})")

    if result.get("capped"):
        console.print(
            f"\n[yellow]Results capped at {result['total']}. Refine your search.[/yellow]"
        )
