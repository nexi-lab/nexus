"""Search and discovery commands - glob, grep, semantic search."""

from collections import defaultdict
from typing import Any, cast

import click

from nexus.cli.output import OutputOptions, add_output_options, render_error, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
    open_filesystem,
)


def register_commands(cli: click.Group) -> None:
    """Register all search and discovery commands."""
    cli.add_command(glob)
    cli.add_command(grep)
    cli.add_command(semantic_search_group)


@click.command()
@click.argument("pattern", type=str)
@click.argument("path", type=str, default="/", required=False)
@click.option("-l", "--long", is_flag=True, help="Show detailed listing with size and date")
@click.option(
    "-t", "--type", type=click.Choice(["f", "d"]), help="Filter by type: f=files, d=directories"
)
@add_output_options
@add_backend_options
async def glob(
    pattern: str,
    path: str,
    long: bool,
    type: str | None,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Find files matching a glob pattern.

    Supports:
    - * (matches any characters except /)
    - ** (matches any characters including /)
    - ? (matches single character)
    - [...] (character classes)

    Examples:
        nexus glob "**/*.py"
        nexus glob "*.txt" /workspace
        nexus glob -l "**/*.py"
        nexus glob "**/*.py" --json
        nexus glob -t f "**/*"
    """
    timing = CommandTiming()

    try:
        async with open_filesystem(remote_url, remote_api_key) as nx:
            with timing.phase("connect"):
                pass  # connection already established by async with

            with timing.phase("server"):
                result = nx.service("search").glob(pattern, path)
                matches = (
                    result["matches"]
                    if isinstance(result, dict) and "matches" in result
                    else result
                )

            if not matches:
                render_output(
                    data=[],
                    output_opts=output_opts,
                    timing=timing,
                    message=f"No files match pattern: {pattern}",
                )
                return

            # Filter by type if specified
            if type:
                filtered = []
                for match in matches:
                    is_dir = (
                        await nx.sys_is_directory(match)
                        if hasattr(nx, "sys_is_directory")
                        else match.endswith("/")
                    )
                    if (type == "d" and is_dir) or (type == "f" and not is_dir):
                        filtered.append(match)
                matches = filtered

            # For --long, use batch metadata instead of N+1 reads
            match_data: list[dict[str, Any]]
            if long and matches:
                # Try batch metadata via sys_readdir on parent paths
                metadata_map: dict[str, dict[str, Any]] = {}
                try:
                    parent_path = path if path != "/" else "/"
                    all_details = await nx.sys_readdir(parent_path, recursive=True, details=True)
                    details_list = cast(list[dict[str, Any]], all_details)
                    metadata_map = {d["path"]: d for d in details_list}
                except Exception:
                    pass

                match_data = []
                for m in matches:
                    meta = metadata_map.get(m, {})
                    match_data.append(
                        {
                            "path": m,
                            "size": meta.get("size", 0),
                            "modified_at": meta.get("modified_at", ""),
                        }
                    )
            else:
                match_data = [{"path": m} for m in matches]

        def _print_human(entries: list[dict[str, Any]]) -> None:
            console.print(
                f"[green]Found {len(entries)} files matching[/green] [cyan]{pattern}[/cyan]:"
            )
            if long:
                for entry in entries:
                    console.print(
                        f"  {entry.get('size', 0):>10}  {entry.get('modified_at', '')}  {entry['path']}"
                    )
            else:
                for entry in entries:
                    console.print(f"  {entry['path']}")

        render_output(
            data=match_data, output_opts=output_opts, timing=timing, human_formatter=_print_human
        )
    except Exception as e:
        if output_opts.json_output:
            from nexus.cli.exit_codes import ExitCode

            render_error(
                error=e, output_opts=output_opts, exit_code=ExitCode.GENERAL_ERROR, timing=timing
            )
        else:
            handle_error(e)


@click.command()
@click.argument("pattern", type=str)
@click.argument("path", type=str, default="/", required=False)
@click.option("-f", "--file-pattern", help="Filter files by glob pattern (e.g., *.py)")
@click.option("-i", "--ignore-case", is_flag=True, help="Case-insensitive search")
@click.option("-n", "--line-number", is_flag=True, help="Show line numbers (like grep -n)")
@click.option("-l", "--files-with-matches", is_flag=True, help="Show only filenames with matches")
@click.option("-c", "--count", is_flag=True, help="Show count of matches per file")
@click.option("--invert-match", is_flag=True, help="Invert match (not yet wired to core grep)")
@click.option(
    "-A",
    "--after-context",
    type=int,
    default=0,
    help="Show N lines after match (not yet wired to core grep)",
)
@click.option(
    "-B",
    "--before-context",
    type=int,
    default=0,
    help="Show N lines before match (not yet wired to core grep)",
)
@click.option(
    "-C",
    "--context",
    type=int,
    default=0,
    help="Show N lines before and after match (not yet wired to core grep)",
)
@click.option("-m", "--max-results", default=100, help="Maximum results to show")
@click.option(
    "--search-mode",
    type=click.Choice(["auto", "parsed", "raw"]),
    default="auto",
    help="Search mode: auto (try parsed, fallback to raw), parsed (only parsed), raw (only raw)",
    show_default=True,
)
@add_output_options
@add_backend_options
async def grep(
    pattern: str,
    path: str,
    file_pattern: str | None,
    ignore_case: bool,
    line_number: bool,
    files_with_matches: bool,
    count: bool,
    invert_match: bool,  # noqa: ARG001 - not yet wired to core grep
    after_context: int,  # noqa: ARG001 - not yet wired to core grep
    before_context: int,  # noqa: ARG001 - not yet wired to core grep
    context: int,  # noqa: ARG001 - not yet wired to core grep
    max_results: int,
    search_mode: str,
    output_opts: OutputOptions,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Search file contents using regex patterns.

    Examples:
        nexus grep "TODO"
        nexus grep -n "error" /workspace
        nexus grep "TODO" --json
        nexus grep -l "TODO" .
        nexus grep -i "error" --json --fields file,line

    \b
        nexus grep "revenue" -f "**/*.pdf" --search-mode=parsed
    """
    timing = CommandTiming()

    try:
        async with open_filesystem(remote_url, remote_api_key) as nx:
            with timing.phase("connect"):
                pass  # connection already established by async with

            with timing.phase("server"):
                result = nx.service("search").grep(
                    pattern,
                    path=path,
                    file_pattern=file_pattern,
                    ignore_case=ignore_case,
                    max_results=max_results,
                    search_mode=search_mode,
                )

        # Normalize result format
        if isinstance(result, dict) and "results" in result:
            matches = result["results"]
        elif isinstance(result, dict) and "matches" in result:
            matches = result["matches"]
        else:
            matches = result

        if not matches:
            render_output(
                data=[],
                output_opts=output_opts,
                timing=timing,
                message=f"No matches found for: {pattern}",
            )
            return

        # Group by file
        matches_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for match in matches:
            matches_by_file[match["file"]].append(match)

        # Structure data for JSON output
        data = {
            "pattern": pattern,
            "total_matches": len(matches),
            "files_matched": len(matches_by_file),
            "matches": matches,
        }

        def _print_human(d: dict[str, Any]) -> None:
            m_list = d["matches"]
            by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for m in m_list:
                by_file[m["file"]].append(m)

            if files_with_matches:
                for filename in sorted(by_file.keys()):
                    console.print(filename)
                return

            if count:
                for filename in sorted(by_file.keys()):
                    console.print(f"{filename}:{len(by_file[filename])}")
                return

            console.print(f"[green]Found {len(m_list)} matches[/green] for [cyan]{pattern}[/cyan]")
            if search_mode != "auto":
                console.print(f"[dim]Search mode: {search_mode}[/dim]")
            console.print()

            for filename in sorted(by_file.keys()):
                console.print(f"[bold cyan]{filename}[/bold cyan]")
                for m in by_file[filename]:
                    ln = f"{m['line']}:" if line_number else ""
                    console.print(f"  [yellow]{ln}[/yellow] {m['content']}")
                console.print()

        render_output(
            data=data, output_opts=output_opts, timing=timing, human_formatter=_print_human
        )
    except Exception as e:
        if output_opts.json_output:
            from nexus.cli.exit_codes import ExitCode

            render_error(
                error=e, output_opts=output_opts, exit_code=ExitCode.GENERAL_ERROR, timing=timing
            )
        else:
            handle_error(e)


# Semantic Search Commands (v0.4.0)


@click.group(name="search")
def semantic_search_group() -> None:
    """Semantic search commands using natural language queries."""
    pass


@semantic_search_group.command(name="init")
@click.option(
    "--provider",
    type=click.Choice(["openai", "voyage"]),
    default=None,
    help="Embedding provider (default: None = keyword-only; recommended: openai)",
)
@click.option("--model", help="Embedding model name (uses provider default if not specified)")
@click.option("--api-key", help="API key for the embedding provider (if using remote)")
@click.option("--chunk-size", type=int, default=1024, help="Chunk size in tokens")
@click.option(
    "--chunk-strategy",
    type=click.Choice(["fixed", "semantic", "overlapping"]),
    default="semantic",
    help="Chunking strategy",
)
@add_backend_options
async def search_init(
    provider: str | None,
    model: str | None,
    api_key: str | None,
    chunk_size: int,
    chunk_strategy: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Initialize semantic search engine.

    Uses existing database (SQLite/PostgreSQL) with FTS for keyword search.
    Optionally add embeddings for semantic/hybrid search.

    Search Modes:
    - Keyword-only (default): Uses FTS5/tsvector, no embeddings needed
    - Semantic: Requires --provider (recommended: openai)
    - Hybrid: Best results, combines keyword + semantic

    Examples:
        # Keyword-only search (no embeddings, minimal deps)
        nexus search init

        # Semantic search with OpenAI (recommended, lightweight)
        nexus search init --provider openai --api-key sk-xxx

        # Semantic search with Voyage AI (specialized embeddings)
        nexus search init --provider voyage --api-key pa-xxx

        # Custom settings
        nexus search init --provider openai --chunk-size 2048
    """
    try:
        nx = await get_filesystem(remote_url, remote_api_key)

        with console.status("[yellow]Initializing search engine...[/yellow]", spinner="dots"):
            nx.service("search").ainitialize_semantic_search(
                nx=nx,
                record_store_engine=None,
                embedding_provider=provider,
                embedding_model=model,
                api_key=api_key,
                chunk_size=chunk_size,
                chunk_strategy=chunk_strategy,
            )

        console.print("[green]✓ Search engine initialized successfully![/green]")
        console.print("  Mode: [cyan]Remote (server-side)[/cyan]")
        console.print(f"  Provider: [cyan]{provider or 'None (keyword-only)'}[/cyan]")
        console.print(f"  Chunk size: [cyan]{chunk_size}[/cyan] tokens")
        console.print(f"  Chunk strategy: [cyan]{chunk_strategy}[/cyan]")

        if not provider:
            console.print("\n[yellow]Note:[/yellow] Keyword-only mode enabled (FTS).")
            console.print(
                "For semantic/hybrid search, reinitialize with --provider openai (recommended) or voyage"
            )

        nx.close()
    except Exception as e:
        handle_error(e)


@semantic_search_group.command(name="index")
@click.argument("path", default="/")
@click.option("--recursive/--no-recursive", default=True, help="Index directory recursively")
@add_backend_options
async def search_index(
    path: str,
    recursive: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Index documents for semantic search.

    This command chunks documents and generates embeddings for semantic search.

    Examples:
        # Index all documents
        nexus search index

        # Index specific directory
        nexus search index /docs

        # Index single file
        nexus search index /docs/README.md
    """
    try:
        nx = await get_filesystem(remote_url, remote_api_key)

        with console.status(f"[yellow]Indexing {path}...[/yellow]", spinner="dots"):
            search_svc = nx.service("search")
            raw_results = search_svc.semantic_search_index(path, recursive=recursive)

        # RPC handler wraps results as {"indexed": {path: count, ...}, ...}
        if isinstance(raw_results, dict) and "indexed" in raw_results:
            results = raw_results["indexed"]
            total_chunks = raw_results.get("total_chunks", 0)
        else:
            results = raw_results
            total_chunks = sum(v for v in results.values() if isinstance(v, int) and v > 0)

        # Display results
        successful = sum(1 for v in results.values() if isinstance(v, int) and v > 0)
        failed = sum(1 for v in results.values() if isinstance(v, int) and v < 0)

        console.print("\n[green]✓ Indexing complete![/green]")
        console.print(f"  Files indexed: [cyan]{successful}[/cyan]")
        console.print(f"  Total chunks: [cyan]{total_chunks}[/cyan]")
        if failed > 0:
            console.print(f"  Failed: [yellow]{failed}[/yellow]")

        # Show stats
        stats: dict[str, Any] = search_svc.semantic_search_stats()
        console.print("\n[bold cyan]Index Statistics:[/bold cyan]")
        console.print(f"  Total indexed files: [green]{stats['indexed_files']}[/green]")
        console.print(f"  Total chunks: [green]{stats['total_chunks']}[/green]")

        nx.close()
    except Exception as e:
        handle_error(e)


@semantic_search_group.command(name="query")
@click.argument("query", type=str)
@click.option("-p", "--path", default="/", help="Root path to search")
@click.option("-n", "--limit", default=10, help="Maximum number of results")
@click.option(
    "-m",
    "--mode",
    type=click.Choice(["keyword", "semantic", "hybrid"]),
    default="semantic",
    help="Search mode (default: semantic)",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@add_backend_options
async def search_query(
    query: str,
    path: str,
    limit: int,
    mode: str,
    json_output: bool,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Search documents using natural language queries.

    Examples:
        # Search for authentication information
        nexus search query "How does authentication work?"

        # Search in specific directory
        nexus search query "database migration" --path /docs

        # Get more results
        nexus search query "error handling" --limit 20

        # JSON output
        nexus search query "API endpoints" --json
    """
    try:
        nx = await get_filesystem(remote_url, remote_api_key)

        with console.status(f"[yellow]Searching for: {query}[/yellow]", spinner="dots"):
            search_svc = nx.service("search")
            raw = search_svc.semantic_search(query, path=path, limit=limit, search_mode=mode)
            # RPC handler wraps as {"results": [...]}, unwrap if needed
            results: list[dict[str, Any]] = (
                raw["results"] if isinstance(raw, dict) and "results" in raw else raw
            )

        if json_output:
            import json

            console.print(json.dumps(results, indent=2))
        else:
            if not results:
                console.print(f"[yellow]No results found for:[/yellow] {query}")
                nx.close()
                return

            console.print(
                f"\n[green]Found {len(results)} results for:[/green] [cyan]{query}[/cyan]\n"
            )

            for i, result in enumerate(results, 1):
                score = result["score"]
                file_path = result["path"]
                chunk_text = result["chunk_text"]

                # Truncate long text
                if len(chunk_text) > 200:
                    chunk_text = chunk_text[:200] + "..."

                console.print(f"[bold]{i}. {file_path}[/bold]")
                console.print(f"   Score: [green]{score:.3f}[/green]")
                console.print(f"   [dim]{chunk_text}[/dim]")
                console.print()

        nx.close()
    except Exception as e:
        handle_error(e)


@semantic_search_group.command(name="stats")
@add_backend_options
async def search_stats(remote_url: str | None, remote_api_key: str | None) -> None:
    """Show semantic search statistics.

    Examples:
        nexus search stats
    """
    try:
        nx = await get_filesystem(remote_url, remote_api_key)

        stats: dict[str, Any] = nx.service("search").semantic_search_stats()

        console.print("\n[bold cyan]Semantic Search Statistics[/bold cyan]")
        console.print(
            f"  Engine: [green]{stats.get('engine', stats.get('database_type', 'unknown'))}[/green]"
        )
        console.print(
            f"  Indexed files: [green]{stats.get('total_files', stats.get('indexed_files', 0))}[/green]"
        )
        console.print(f"  Total chunks: [green]{stats.get('total_chunks', 0)}[/green]")
        if stats.get("embedding_model"):
            console.print(f"  Embedding model: [cyan]{stats['embedding_model']}[/cyan]")
        if stats.get("chunk_size"):
            console.print(f"  Chunk size: [cyan]{stats['chunk_size']}[/cyan] tokens")
        if stats.get("chunk_strategy"):
            console.print(f"  Chunk strategy: [cyan]{stats['chunk_strategy']}[/cyan]")

        nx.close()
    except Exception as e:
        handle_error(e)
