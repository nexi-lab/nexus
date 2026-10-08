"""Search and discovery commands - glob, grep, semantic search."""

import asyncio
import inspect
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

import click

from nexus.cli.clients.search import SearchClient
from nexus.cli.config import resolve_connection
from nexus.cli.exit_codes import ExitCode
from nexus.cli.output import OutputOptions, add_output_options, render_error, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import (
    ZONE_ID_OPTION,
    add_backend_options,
    console,
    handle_error,
    open_filesystem,
)


def register_commands(cli: click.Group) -> None:
    """Register all search and discovery commands."""
    cli.add_command(glob)
    cli.add_command(grep)
    cli.add_command(semantic_search_group)


def _resolve_files_arg(
    files: tuple[str, ...],
    files_from: str | None,
) -> list[str] | None:
    """Merge ``--files`` and ``--files-from`` into a single list (#3701).

    Semantics:
    * Neither flag set → returns ``None`` so the server-side default
      (walk the tree) applies.
    * ``--files a --files b`` → returns ``["a", "b"]``.
    * ``--files-from path.txt`` → reads newline-separated paths.
    * ``--files-from -`` → reads from stdin (pipe pattern).
    * Both flags set → explicit values come first, then file contents,
      in order. The Search host validates and deduplicates the paths.

    Blank lines and lines beginning with ``#`` in the files-from source
    are skipped so agents can pipe JSON-dumped lists through
    ``jq -r '.files[]'`` or through a human-readable listing without
    needing an intermediate clean-up step.
    """
    if not files and files_from is None:
        return None

    merged: list[str] = list(files)

    if files_from is not None:
        source = sys.stdin.read() if files_from == "-" else Path(files_from).read_text()
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            merged.append(stripped)

    return merged


async def _await_service_result(value: Any) -> Any:
    """Await local async service results while preserving sync remote proxy results."""
    if inspect.isawaitable(value):
        return await value
    return value


@click.command()
@click.argument("pattern", type=str)
@click.argument("path", type=str, default="/", required=False)
@click.option("-l", "--long", is_flag=True, help="Show detailed listing with size and date")
@click.option(
    "-t", "--type", type=click.Choice(["f", "d"]), help="Filter by type: f=files, d=directories"
)
@click.option(
    "--plain",
    is_flag=True,
    help=(
        "Pipe-friendly output: one path per line, no decoration or markup. "
        "Use to pipe into ``nexus grep --files-from=-`` without jq."
    ),
)
@click.option(
    "--files",
    "files",
    multiple=True,
    help=(
        "Stateless narrowing (#3701): restrict the glob to this working set "
        "of paths instead of walking the tree. Repeatable."
    ),
)
@click.option(
    "--files-from",
    type=str,
    default=None,
    help=(
        "Read the narrowing working set from a file (one path per line). "
        "Use ``-`` for stdin so you can pipe: "
        "``nexus grep -l X | nexus glob '**/*.py' --files-from=-``."
    ),
)
@add_output_options
@add_backend_options
def glob(
    pattern: str,
    path: str,
    long: bool,
    type: str | None,
    plain: bool,
    files: tuple[str, ...],
    files_from: str | None,
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
        nexus glob "*.py" --files /src/a.py --files /src/b.py
        nexus grep "TODO" -l | nexus glob "**/*.py" --files-from=-
    """

    async def _impl() -> None:
        timing = CommandTiming()
        files_list = _resolve_files_arg(files, files_from)

        try:
            async with open_filesystem(remote_url, remote_api_key) as nx:
                with timing.phase("connect"):
                    pass  # connection already established by async with

                with timing.phase("server"):
                    glob_kwargs: dict[str, Any] = {}
                    if files_list is not None:
                        glob_kwargs["files"] = files_list
                    result = await _await_service_result(
                        nx.service("search").glob(pattern, path, **glob_kwargs)
                    )
                    matches = (
                        result["matches"]
                        if isinstance(result, dict) and "matches" in result
                        else result
                    )

                if not matches:
                    if plain and not output_opts.json_output_explicit:
                        # --plain with no matches: emit empty output for
                        # safe piping into ``--files-from=-``.
                        return
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
                            nx.is_directory(match)
                            if hasattr(nx, "is_directory")
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
                        all_details = nx.sys_readdir(parent_path, recursive=True, details=True)
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

            # #3701: --plain mode is a pipe-first output format. Bypass
            # render_output (and its auto-JSON-when-piped fallback) so
            # the stdout is unadorned one-path-per-line, ready for
            # ``nexus grep --files-from=-`` consumption.
            if plain and not output_opts.json_output_explicit:
                for entry in match_data:
                    print(entry["path"])  # noqa: T201
                return

            def _print_human(entries: list[dict[str, Any]]) -> None:
                if plain:
                    # Pipe-friendly: one path per line, no decoration,
                    # via plain print() so stdout is unadorned (#3701).
                    for entry in entries:
                        print(entry["path"])  # noqa: T201
                    return

                console.print(
                    f"[nexus.success]Found {len(entries)} files matching[/nexus.success] [nexus.value]{pattern}[/nexus.value]:"
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
                data=match_data,
                output_opts=output_opts,
                timing=timing,
                human_formatter=_print_human,
            )
        except Exception as e:
            if output_opts.json_output:
                from nexus.cli.exit_codes import ExitCode

                render_error(
                    error=e,
                    output_opts=output_opts,
                    exit_code=ExitCode.GENERAL_ERROR,
                    timing=timing,
                )
            else:
                handle_error(e)

    asyncio.run(_impl())


@click.command()
@click.argument("pattern", type=str)
@click.argument("path", type=str, default="/", required=False)
@click.option("-f", "--file-pattern", help="Filter files by glob pattern (e.g., *.py)")
@click.option("-i", "--ignore-case", is_flag=True, help="Case-insensitive search")
@click.option("-n", "--line-number", is_flag=True, help="Show line numbers (like grep -n)")
@click.option("-l", "--files-with-matches", is_flag=True, help="Show only filenames with matches")
@click.option("-c", "--count", is_flag=True, help="Show count of matches per file")
@click.option("--invert-match", is_flag=True, help="Invert match (return non-matching lines)")
@click.option(
    "-A",
    "--after-context",
    type=int,
    default=0,
    help="Show N lines after each match",
)
@click.option(
    "-B",
    "--before-context",
    type=int,
    default=0,
    help="Show N lines before each match",
)
@click.option(
    "-C",
    "--context",
    type=int,
    default=0,
    help="Show N lines before and after each match (sets both -A and -B)",
)
@click.option("-m", "--max-results", default=100, help="Maximum results to show")
@click.option(
    "--search-mode",
    type=click.Choice(["auto", "raw"]),
    default="auto",
    help="Search current file bytes (auto and raw use the same discovery service)",
    show_default=True,
)
@click.option(
    "--files",
    "files",
    multiple=True,
    help=(
        "Stateless narrowing (#3701): restrict grep to this working set "
        "of paths instead of walking the tree. Repeatable."
    ),
)
@click.option(
    "--files-from",
    type=str,
    default=None,
    help=(
        "Read the narrowing working set from a file (one path per line). "
        "Use ``-`` for stdin so you can pipe: "
        "``nexus grep 'auth' -l | nexus grep 'JWT' --files-from=-``."
    ),
)
@click.option(
    "--block-type",
    type=click.Choice(
        ["code", "table", "frontmatter", "paragraph", "blockquote", "list", "heading"]
    ),
    default=None,
    help=(
        "Restrict matches to a markdown block type (#3720). "
        "Non-markdown files pass through unfiltered."
    ),
)
@click.option(
    "--in-section",
    "section",
    type=str,
    default=None,
    help=(
        "Restrict matches to a markdown/parsed-content section heading "
        "(#4186), e.g. 'API' or '## API'."
    ),
)
@add_output_options
@add_backend_options
def grep(
    pattern: str,
    path: str,
    file_pattern: str | None,
    ignore_case: bool,
    line_number: bool,
    files_with_matches: bool,
    count: bool,
    invert_match: bool,
    after_context: int,
    before_context: int,
    context: int,
    max_results: int,
    search_mode: str,
    files: tuple[str, ...],
    files_from: str | None,
    block_type: str | None,
    section: str | None,
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
        nexus grep "pool" -B 2 -A 2   # with 2 lines of surrounding context
        nexus grep "TODO" --invert-match   # show non-matching lines
        nexus grep "JWT" --files /src/auth.py --files /src/user.py
        nexus grep "auth" -l | nexus grep "JWT" --files-from=-

    \b
        nexus grep "revenue" -f "**/*.pdf" --search-mode=parsed
        nexus grep "status" /workspace/spec.md --in-section "## API"
    """

    async def _impl() -> None:
        timing = CommandTiming()

        # Resolve --files / --files-from into the working set (or None).
        files_list = _resolve_files_arg(files, files_from)

        # -C / --context is shorthand for setting both -A and -B to the
        # same value, matching POSIX grep semantics.
        effective_before = before_context or context
        effective_after = after_context or context

        try:
            async with open_filesystem(remote_url, remote_api_key) as nx:
                with timing.phase("connect"):
                    pass  # connection already established by async with

                with timing.phase("server"):
                    grep_kwargs: dict[str, Any] = {
                        "path": path,
                        "file_pattern": file_pattern,
                        "ignore_case": ignore_case,
                        "max_results": max_results,
                        "search_mode": search_mode,
                    }
                    # Only forward the new flags when non-default so the
                    # RPC payload stays lean and older servers without
                    # the #3701 fields still accept the request.
                    if effective_before:
                        grep_kwargs["before_context"] = effective_before
                    if effective_after:
                        grep_kwargs["after_context"] = effective_after
                    if invert_match:
                        grep_kwargs["invert_match"] = True
                    if files_list is not None:
                        grep_kwargs["files"] = files_list
                    if block_type is not None:
                        grep_kwargs["block_type"] = block_type
                    if section is not None:
                        grep_kwargs["section"] = section

                    result = await _await_service_result(
                        nx.service("search").grep(pattern, **grep_kwargs)
                    )

            # Normalize result format
            if isinstance(result, dict) and "results" in result:
                matches = result["results"]
            elif isinstance(result, dict) and "matches" in result:
                matches = result["matches"]
            else:
                matches = result

            if not matches:
                if files_with_matches and not output_opts.json_output_explicit:
                    # -l with no matches: produce an empty pipe output so
                    # downstream ``--files-from=-`` sees an empty list, not
                    # a JSON envelope that fails to parse.
                    return
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

            # #3701: ``-l`` mode is a pipe-first output format. Bypass
            # render_output entirely — including the auto-JSON-when-piped
            # fallback in add_output_options — and write plain filenames
            # to stdout so downstream ``--files-from=-`` can consume them.
            # Users who want JSON for -l mode can still pass --json
            # explicitly; we only bypass the auto-detection.
            if files_with_matches and not output_opts.json_output_explicit:
                for filename in sorted(matches_by_file.keys()):
                    print(filename)  # noqa: T201
                return

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
                    # -l mode: print one filename per line, unadorned, via
                    # plain print() so the output is pipeable to
                    # ``--files-from=-`` without ANSI escape codes or
                    # Rich markup interpretation (#3701).
                    for filename in sorted(by_file.keys()):
                        print(filename)  # noqa: T201
                    return

                if count:
                    for filename in sorted(by_file.keys()):
                        console.print(f"{filename}:{len(by_file[filename])}")
                    return

                console.print(
                    f"[nexus.success]Found {len(m_list)} matches[/nexus.success] for [nexus.value]{pattern}[/nexus.value]"
                )
                if search_mode != "auto":
                    console.print(f"[nexus.muted]Search mode: {search_mode}[/nexus.muted]")
                console.print()

                has_context = bool(effective_before or effective_after)

                for filename in sorted(by_file.keys()):
                    console.print(f"[bold nexus.value]{filename}[/bold nexus.value]")
                    for m in by_file[filename]:
                        ln = f"{m['line']}:" if line_number else ""
                        # Render before-context lines (#3701): dim, with a
                        # ``-`` line-separator marking so the output matches
                        # classic ``grep -B N`` formatting.
                        if has_context:
                            for b in m.get("before_context") or []:
                                b_ln = f"{b['line']}-" if line_number else ""
                                console.print(f"  [nexus.muted]{b_ln} {b['content']}[/nexus.muted]")
                        console.print(f"  [nexus.warning]{ln}[/nexus.warning] {m['content']}")
                        if has_context:
                            for a in m.get("after_context") or []:
                                a_ln = f"{a['line']}-" if line_number else ""
                                console.print(f"  [nexus.muted]{a_ln} {a['content']}[/nexus.muted]")
                            # Separator between context blocks, like grep.
                            if by_file[filename][-1] is not m:
                                console.print("  --")
                    console.print()

            render_output(
                data=data, output_opts=output_opts, timing=timing, human_formatter=_print_human
            )
        except Exception as e:
            if output_opts.json_output:
                from nexus.cli.exit_codes import ExitCode

                render_error(
                    error=e,
                    output_opts=output_opts,
                    exit_code=ExitCode.GENERAL_ERROR,
                    timing=timing,
                )
            else:
                handle_error(e)

    asyncio.run(_impl())


# Semantic Search Commands (v0.4.0)


def _search_client(
    remote_url: str | None, remote_api_key: str | None, zone_id: str | None
) -> SearchClient:
    ctx = click.get_current_context(silent=True)
    profile_name = ctx.obj.get("profile") if ctx and ctx.obj else None
    connection = resolve_connection(remote_url, remote_api_key, profile_name, zone_id)
    if not connection.url:
        raise click.ClickException(
            "Server URL required. Set NEXUS_URL, use --remote-url, or select a Nexus profile."
        )
    return SearchClient(connection.url, connection.api_key, zone_id=connection.zone_id)


def _print_search_stats(stats: dict[str, Any]) -> None:
    console.print("\n[bold nexus.value]Search Statistics[/bold nexus.value]")
    for label, field in (
        ("Engine", "backend"),
        ("Indexed files", "fts_path_count"),
        ("Keyword chunks", "fts_doc_count"),
        ("Vector chunks", "ann_chunk_count"),
        ("Pending documents", "pending"),
    ):
        console.print(f"  {label}: [nexus.value]{stats[field]}[/nexus.value]")
    if stats.get("embedding_model"):
        console.print(f"  Embedding model: [nexus.value]{stats['embedding_model']}[/nexus.value]")


@click.group(name="search")
def semantic_search_group() -> None:
    """Search and index documents through the cluster HTTP API."""


@semantic_search_group.command(name="index")
@click.argument("path", default="/")
@click.option("--recursive/--no-recursive", default=True, help="Index directory recursively")
@ZONE_ID_OPTION
@add_backend_options
def search_index(
    path: str,
    recursive: bool,
    zone_id: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Index documents from the cluster's VFS.

    Examples:
        nexus search index /docs --remote-url http://localhost:2027
        nexus search index /docs --zone-id sharedzone
    """
    try:
        with _search_client(remote_url, remote_api_key, zone_id) as client:
            with console.status(
                f"[nexus.warning]Indexing {path}...[/nexus.warning]", spinner="dots"
            ):
                result = client.index(path, recursive=recursive)
            console.print("\n[nexus.success]? Indexing complete![/nexus.success]")
            console.print(f"  Files indexed: [nexus.value]{result['indexed_count']}[/nexus.value]")
            console.print(f"  Files skipped: [nexus.value]{result['skipped_count']}[/nexus.value]")
            _print_search_stats(client.stats())
    except click.ClickException as exc:
        render_error(error=exc, exit_code=ExitCode.CONFIG_ERROR)
    except Exception as exc:
        render_error(error=exc)


@semantic_search_group.command(name="query")
@click.argument("query", type=str)
@click.option("-p", "--path", default="/", help="Root path to search")
@click.option(
    "-n", "--limit", type=click.IntRange(min=1), default=10, help="Maximum number of results"
)
@click.option(
    "-m",
    "--mode",
    type=click.Choice(["keyword", "semantic", "hybrid"]),
    default="semantic",
    help="Search mode (default: semantic)",
)
@click.option("--json", "json_output", is_flag=True, help="Output results as a JSON array")
@ZONE_ID_OPTION
@add_backend_options
def search_query(
    query: str,
    path: str,
    limit: int,
    mode: str,
    json_output: bool,
    zone_id: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Search indexed documents through the cluster HTTP API.

    Examples:
        nexus search query "authentication" --mode keyword --json
        nexus search query "database migration" --path /docs --zone-id sharedzone
    """
    try:
        with (
            _search_client(remote_url, remote_api_key, zone_id) as client,
            console.status(
                f"[nexus.warning]Searching for: {query}[/nexus.warning]", spinner="dots"
            ),
        ):
            results = client.query(query, path_filter=path, limit=limit, query_type=mode)["results"]
        if json_output:
            import json

            click.echo(json.dumps(results, indent=2, default=str))
        elif not results:
            console.print(f"[nexus.warning]No results found for:[/nexus.warning] {query}")
        else:
            console.print(
                f"\n[nexus.success]Found {len(results)} results for:[/nexus.success] "
                f"[nexus.value]{query}[/nexus.value]\n"
            )
            for i, result in enumerate(results, 1):
                chunk_text = result["chunk_text"]
                if len(chunk_text) > 200:
                    chunk_text = chunk_text[:200] + "..."
                console.print(f"[bold]{i}. {result['path']}[/bold]")
                console.print(f"   Score: [nexus.success]{result['score']:.3f}[/nexus.success]")
                console.print(f"   [nexus.muted]{chunk_text}[/nexus.muted]")
                console.print()
    except click.ClickException as exc:
        render_error(error=exc, exit_code=ExitCode.CONFIG_ERROR)
    except Exception as exc:
        render_error(error=exc)


@semantic_search_group.command(name="stats")
@ZONE_ID_OPTION
@add_backend_options
def search_stats(zone_id: str | None, remote_url: str | None, remote_api_key: str | None) -> None:
    """Show the cluster's search index statistics."""
    try:
        with _search_client(remote_url, remote_api_key, zone_id) as client:
            _print_search_stats(client.stats())
    except click.ClickException as exc:
        render_error(error=exc, exit_code=ExitCode.CONFIG_ERROR)
    except Exception as exc:
        render_error(error=exc)
