"""Search and discovery commands - glob, grep, find-duplicates."""

from __future__ import annotations

import sys
from typing import Any, cast

import click

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)
from nexus.core.nexus_fs import NexusFS


def register_commands(cli: click.Group) -> None:
    """Register all search and discovery commands."""
    cli.add_command(glob)
    cli.add_command(grep)
    cli.add_command(find_duplicates)
    cli.add_command(semantic_search_group)


@click.command()
@click.argument("pattern", type=str)
@click.argument("path", type=str, default="/", required=False)
@click.option("-l", "--long", is_flag=True, help="Show detailed listing with size and date")
@click.option(
    "-t", "--type", type=click.Choice(["f", "d"]), help="Filter by type: f=files, d=directories"
)
@add_backend_options
def glob(
    pattern: str,
    path: str,
    long: bool,
    type: str | None,
    backend_config: BackendConfig,
) -> None:
    """Find files matching a glob pattern.

    Supports:
    - * (matches any characters except /)
    - ** (matches any characters including /)
    - ? (matches single character)
    - [...] (character classes)

    Examples:
        # Basic patterns
        nexus glob "**/*.py"
        nexus glob "*.txt" /workspace

        # With details (like ls -l)
        nexus glob -l "**/*.py"

        # Filter by type
        nexus glob -t f "**/*"          # Only files
        nexus glob -t d "/workspace/*"  # Only directories
    """
    try:
        nx = get_filesystem(backend_config)
        matches = nx.glob(pattern, path)

        if not matches:
            console.print(f"[yellow]No files match pattern:[/yellow] {pattern}")
            nx.close()
            return

        # Filter by type if specified
        if type:
            filtered_matches = []
            for match in matches:
                is_dir = (
                    nx.is_directory(match) if hasattr(nx, "is_directory") else match.endswith("/")
                )
                if (type == "d" and is_dir) or (type == "f" and not is_dir):
                    filtered_matches.append(match)
            matches = filtered_matches

        # Get metadata if needed for long format
        match_data = []
        if long:
            for match in matches:
                try:
                    metadata = nx.read(match, return_metadata=True)
                    if isinstance(metadata, dict):
                        match_data.append(
                            {
                                "path": match,
                                "size": metadata.get("size", 0),
                                "mtime": metadata.get("modified_at", ""),
                            }
                        )
                    else:
                        match_data.append({"path": match, "size": 0, "mtime": ""})
                except Exception:
                    match_data.append({"path": match, "size": 0, "mtime": ""})

        nx.close()

        console.print(f"[green]Found {len(matches)} files matching[/green] [cyan]{pattern}[/cyan]:")

        if long:
            # Show detailed listing
            for data in match_data:
                console.print(f"  {data['size']:>10}  {data['mtime']}  {data['path']}")
        else:
            # Simple listing
            for match in matches:
                console.print(f"  {match}")
    except Exception as e:
        handle_error(e)


@click.command()
@click.argument("pattern", type=str)
@click.argument("path", type=str, default="/", required=False)
@click.option("-f", "--file-pattern", help="Filter files by glob pattern (e.g., *.py)")
@click.option("-i", "--ignore-case", is_flag=True, help="Case-insensitive search")
@click.option("-n", "--line-number", is_flag=True, help="Show line numbers (like grep -n)")
@click.option("-l", "--files-with-matches", is_flag=True, help="Show only filenames with matches")
@click.option("-c", "--count", is_flag=True, help="Show count of matches per file")
@click.option("-v", "--invert-match", is_flag=True, help="Invert match (show non-matching lines)")
@click.option("-A", "--after-context", type=int, default=0, help="Show N lines after match")
@click.option("-B", "--before-context", type=int, default=0, help="Show N lines before match")
@click.option("-C", "--context", type=int, default=0, help="Show N lines before and after match")
@click.option("-m", "--max-results", default=100, help="Maximum results to show")
@click.option(
    "--search-mode",
    type=click.Choice(["auto", "parsed", "raw"]),
    default="auto",
    help="Search mode: auto (try parsed, fallback to raw), parsed (only parsed), raw (only raw)",
    show_default=True,
)
@add_backend_options
def grep(
    pattern: str,
    path: str,
    file_pattern: str | None,
    ignore_case: bool,
    line_number: bool,
    files_with_matches: bool,
    count: bool,
    invert_match: bool,  # noqa: ARG001 - TODO: implement invert match
    after_context: int,  # noqa: ARG001 - TODO: implement context lines
    before_context: int,  # noqa: ARG001 - TODO: implement context lines
    context: int,  # noqa: ARG001 - TODO: implement context lines
    max_results: int,
    search_mode: str,
    backend_config: BackendConfig,
) -> None:
    """Search file contents using regex patterns.

    Search Modes:
    - auto: Try parsed text first, fallback to raw (default)
    - parsed: Only search parsed text (great for PDFs/docs)
    - raw: Only search raw file content (skip parsing)

    Examples:
        # Basic search
        nexus grep "TODO"

        # Linux-style with common flags
        nexus grep -n "error" /workspace          # Show line numbers
        nexus grep -l "TODO" .                    # Only filenames
        nexus grep -c "import" **/*.py            # Count matches
        nexus grep -A 3 -B 3 "def main"           # Context lines
        nexus grep -C 5 "class"                   # 5 lines context
        nexus grep -v "test" file.txt             # Invert match
        nexus grep -i "error"                     # Case insensitive

        # Nexus-specific
        nexus grep "revenue" -f "**/*.pdf" --search-mode=parsed
    """
    try:
        nx = get_filesystem(backend_config)

        # TODO: Handle context lines when implemented in core grep()
        # Currently after_context, before_context, and context are not used

        matches = nx.grep(
            pattern,
            path=path,
            file_pattern=file_pattern,
            ignore_case=ignore_case,
            max_results=max_results,
            search_mode=search_mode,
        )
        nx.close()

        if not matches:
            console.print(f"[yellow]No matches found for:[/yellow] {pattern}")
            return

        # Group matches by file for counting and context
        from collections import defaultdict

        matches_by_file = defaultdict(list)
        for match in matches:
            matches_by_file[match["file"]].append(match)

        # Handle -l (files with matches only)
        if files_with_matches:
            for filename in sorted(matches_by_file.keys()):
                console.print(filename)
            return

        # Handle -c (count only)
        if count:
            for filename in sorted(matches_by_file.keys()):
                count_val = len(matches_by_file[filename])
                console.print(f"{filename}:{count_val}")
            return

        # Regular output with optional enhancements
        console.print(f"[green]Found {len(matches)} matches[/green] for [cyan]{pattern}[/cyan]")
        if search_mode != "auto":
            console.print(f"[dim]Search mode: {search_mode}[/dim]")
        console.print()

        for filename in sorted(matches_by_file.keys()):
            file_matches = matches_by_file[filename]
            console.print(f"[bold cyan]{filename}[/bold cyan]")

            for match in file_matches:
                # Format line number if requested
                line_num_str = f"{match['line']}:" if line_number else ""

                console.print(f"  [yellow]{line_num_str}[/yellow] {match['content']}")

            console.print()

    except Exception as e:
        handle_error(e)


@click.command(name="find-duplicates")
@click.option("-p", "--path", default="/", help="Base path to search from")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@add_backend_options
def find_duplicates(path: str, json_output: bool, backend_config: BackendConfig) -> None:
    """Find duplicate files using content hashes.

    Uses batch_get_content_ids() for efficient deduplication detection.
    Groups files by their content hash to find duplicates.

    Examples:
        nexus find-duplicates
        nexus find-duplicates --path /workspace
        nexus find-duplicates --json
    """
    try:
        nx = get_filesystem(backend_config)

        # Only Embedded mode supports batch_get_content_ids
        if not isinstance(nx, NexusFS):
            console.print("[red]Error:[/red] find-duplicates is only available in embedded mode")
            nx.close()
            sys.exit(1)

        # Get all files under path
        with console.status(f"[yellow]Scanning files in {path}...[/yellow]", spinner="dots"):
            all_files_raw = nx.list(path, recursive=True)

            # Check if we got detailed results (list of dicts) or simple paths (list of strings)
            if all_files_raw and isinstance(all_files_raw[0], dict):
                # details=True was used
                all_files_detailed = cast(list[dict[str, Any]], all_files_raw)
                file_paths = [f["path"] for f in all_files_detailed]
            else:
                # Simple list of paths
                file_paths = cast(list[str], all_files_raw)

        if not file_paths:
            console.print(f"[yellow]No files found in {path}[/yellow]")
            nx.close()
            return

        # Get content hashes in batch (single query)
        with console.status(
            f"[yellow]Analyzing {len(file_paths)} files for duplicates...[/yellow]",
            spinner="dots",
        ):
            content_ids = nx.batch_get_content_ids(file_paths)

            # Group by hash
            from collections import defaultdict

            by_hash = defaultdict(list)
            for file_path, content_hash in content_ids.items():
                if content_hash:
                    by_hash[content_hash].append(file_path)

            # Find duplicate groups (hash with >1 file)
            duplicates = {h: paths for h, paths in by_hash.items() if len(paths) > 1}

        nx.close()

        # Calculate statistics
        total_files = len(file_paths)
        unique_hashes = len(by_hash)
        duplicate_groups = len(duplicates)
        duplicate_files = sum(len(paths) for paths in duplicates.values())

        if json_output:
            import json

            result = {
                "total_files": total_files,
                "unique_hashes": unique_hashes,
                "duplicate_groups": duplicate_groups,
                "duplicate_files": duplicate_files,
                "duplicates": [
                    {"content_hash": h, "paths": paths} for h, paths in duplicates.items()
                ],
            }
            console.print(json.dumps(result, indent=2))
        else:
            # Display summary
            console.print("\n[bold cyan]Duplicate File Analysis[/bold cyan]")
            console.print(f"Total files scanned: [green]{total_files}[/green]")
            console.print(f"Unique content hashes: [green]{unique_hashes}[/green]")
            console.print(f"Duplicate groups: [yellow]{duplicate_groups}[/yellow]")
            console.print(f"Duplicate files: [yellow]{duplicate_files}[/yellow]")

            if not duplicates:
                console.print("\n[green]✓ No duplicate files found![/green]")
                return

            # Display duplicate groups
            console.print("\n[bold yellow]Duplicate Groups:[/bold yellow]\n")

            for i, (content_hash, paths) in enumerate(duplicates.items(), 1):
                console.print(f"[bold]Group {i}[/bold] (hash: [dim]{content_hash[:16]}...[/dim])")
                console.print(f"  [yellow]{len(paths)} files with identical content:[/yellow]")
                for pth in sorted(paths):
                    console.print(f"    • {pth}")
                console.print()

            # Calculate potential space savings
            # Each duplicate group can save (n-1) copies
            console.print("[bold cyan]Storage Impact:[/bold cyan]")
            console.print(
                f"  Files that could be deduplicated: [yellow]{duplicate_files - duplicate_groups}[/yellow]"
            )
            console.print("  (CAS automatically deduplicates - no action needed!)")

    except Exception as e:
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
def search_init(
    provider: str | None,
    model: str | None,
    api_key: str | None,
    chunk_size: int,
    chunk_strategy: str,
    backend_config: BackendConfig,
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
    import asyncio

    try:
        nx = get_filesystem(backend_config)

        with console.status("[yellow]Initializing search engine...[/yellow]", spinner="dots"):

            async def init_search() -> None:
                await nx.initialize_semantic_search(  # type: ignore[attr-defined]
                    embedding_provider=provider,
                    embedding_model=model,
                    api_key=api_key,
                    chunk_size=chunk_size,
                    chunk_strategy=chunk_strategy,
                )

            asyncio.run(init_search())

        console.print("[green]✓ Search engine initialized successfully![/green]")
        if isinstance(nx, NexusFS):
            console.print(f"  Database: [cyan]{nx.metadata.engine.dialect.name}[/cyan]")
        else:
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
def search_index(
    path: str,
    recursive: bool,
    backend_config: BackendConfig,
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
    import asyncio

    try:
        nx = get_filesystem(backend_config)

        with console.status(f"[yellow]Indexing {path}...[/yellow]", spinner="dots"):

            async def do_index() -> dict[str, int]:
                # Auto-initialize semantic search if not already initialized (embedded mode)
                if isinstance(nx, NexusFS) and (
                    not hasattr(nx, "_semantic_search") or nx._semantic_search is None
                ):
                    await nx.initialize_semantic_search()
                result: dict[str, int] = await nx.semantic_search_index(path, recursive=recursive)  # type: ignore[attr-defined]
                return result

            results = asyncio.run(do_index())

        # Display results
        total_chunks = sum(v for v in results.values() if v > 0)
        successful = sum(1 for v in results.values() if v > 0)
        failed = sum(1 for v in results.values() if v < 0)

        console.print("\n[green]✓ Indexing complete![/green]")
        console.print(f"  Files indexed: [cyan]{successful}[/cyan]")
        console.print(f"  Total chunks: [cyan]{total_chunks}[/cyan]")
        if failed > 0:
            console.print(f"  Failed: [yellow]{failed}[/yellow]")

        # Show stats
        async def get_stats() -> dict[str, Any]:
            result: dict[str, Any] = await nx.semantic_search_stats()  # type: ignore[attr-defined]
            return result

        stats = asyncio.run(get_stats())
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
@click.option(
    "--provider",
    type=click.Choice(["openai", "voyage"]),
    default=None,
    help="Embedding provider (for semantic/hybrid mode)",
)
@click.option("--api-key", default=None, help="API key for embedding provider")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@add_backend_options
def search_query(
    query: str,
    path: str,
    limit: int,
    mode: str,
    provider: str | None,
    api_key: str | None,
    json_output: bool,
    backend_config: BackendConfig,
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
    import asyncio

    try:
        nx = get_filesystem(backend_config)

        with console.status(f"[yellow]Searching for: {query}[/yellow]", spinner="dots"):

            async def do_search() -> list[dict[str, Any]]:
                # Auto-initialize semantic search if not already initialized (embedded mode)
                if isinstance(nx, NexusFS) and (
                    not hasattr(nx, "_semantic_search") or nx._semantic_search is None
                ):
                    await nx.initialize_semantic_search(
                        embedding_provider=provider, api_key=api_key
                    )
                result: list[dict[str, Any]] = await nx.semantic_search(  # type: ignore[attr-defined]
                    query, path=path, limit=limit, search_mode=mode
                )
                return result

            results = asyncio.run(do_search())

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
def search_stats(backend_config: BackendConfig) -> None:
    """Show semantic search statistics.

    Examples:
        nexus search stats
    """
    import asyncio

    try:
        nx = get_filesystem(backend_config)

        async def get_stats() -> dict[str, Any]:
            # Auto-initialize semantic search if not already initialized (embedded mode)
            if isinstance(nx, NexusFS) and (
                not hasattr(nx, "_semantic_search") or nx._semantic_search is None
            ):
                await nx.initialize_semantic_search()
            result: dict[str, Any] = await nx.semantic_search_stats()  # type: ignore[attr-defined]
            return result

        stats = asyncio.run(get_stats())

        console.print("\n[bold cyan]Semantic Search Statistics[/bold cyan]")
        console.print(f"  Database type: [green]{stats['database_type']}[/green]")
        console.print(f"  Indexed files: [green]{stats['indexed_files']}[/green]")
        console.print(f"  Total chunks: [green]{stats['total_chunks']}[/green]")
        console.print(f"  Embedding model: [cyan]{stats.get('embedding_model', 'None')}[/cyan]")
        console.print(f"  Chunk size: [cyan]{stats['chunk_size']}[/cyan] tokens")
        console.print(f"  Chunk strategy: [cyan]{stats['chunk_strategy']}[/cyan]")

        nx.close()
    except Exception as e:
        handle_error(e)
