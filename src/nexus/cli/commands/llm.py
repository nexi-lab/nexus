"""LLM-powered document reading commands."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import click

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)


def register_commands(cli: click.Group) -> None:
    """Register LLM commands."""
    cli.add_command(llm)


@click.group()
def llm() -> None:
    """LLM-powered document operations.

    Use AI to read, analyze, and answer questions about documents.
    """
    pass


@llm.command()
@click.argument("path", type=str)
@click.argument("prompt", type=str)
@click.option(
    "--model",
    default="claude-sonnet-4",
    help="LLM model to use (default: claude-sonnet-4)",
    show_default=True,
)
@click.option(
    "--max-tokens",
    default=1000,
    type=int,
    help="Maximum tokens in response",
    show_default=True,
)
@click.option(
    "--api-key",
    envvar="API_KEY",  # Will check multiple env vars below
    help="API key for LLM provider (or set ANTHROPIC_API_KEY/OPENAI_API_KEY/OPENROUTER_API_KEY)",
)
@click.option(
    "--no-search",
    is_flag=True,
    help="Disable semantic search (read entire document)",
)
@click.option(
    "--search-mode",
    type=click.Choice(["semantic", "keyword", "hybrid"]),
    default="semantic",
    help="Search mode for context retrieval",
    show_default=True,
)
@click.option(
    "--stream",
    is_flag=True,
    help="Stream the response (show output as it's generated)",
)
@click.option(
    "--detailed",
    is_flag=True,
    help="Show detailed output with citations and metadata",
)
@add_backend_options
def read(
    path: str,
    prompt: str,
    model: str,
    max_tokens: int,
    api_key: str | None,
    no_search: bool,
    search_mode: str,
    stream: bool,
    detailed: bool,
    backend_config: BackendConfig,
) -> None:
    """Read and analyze documents with LLM.

    Ask questions about documents and get AI-powered answers with citations.

    PATH: Document path or glob pattern (e.g., /report.pdf or /docs/**/*.md)

    PROMPT: Your question or instruction

    Examples:

        # Ask a question about a single document
        nexus llm read /reports/q4.pdf "What were the top 3 challenges?"

        # Query multiple documents
        nexus llm read "/docs/**/*.md" "How does authentication work?"

        # Use a different model
        nexus llm read /report.pdf "Summarize this" --model gpt-4o

        # Stream the response
        nexus llm read /long-report.pdf "Analyze trends" --stream

        # Show detailed output with citations
        nexus llm read /docs/**/*.md "Explain the API" --detailed

        # Disable semantic search (read full document)
        nexus llm read /report.txt "Summarize" --no-search

        # Use keyword search instead of semantic
        nexus llm read /docs/**/*.md "API endpoints" --search-mode keyword
    """
    try:
        nx = get_filesystem(backend_config)

        # Auto-detect API key from environment if not provided
        if not api_key:
            import os

            api_key = (
                os.getenv("OPENROUTER_API_KEY")
                or os.getenv("ANTHROPIC_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )

        # Run async function
        if stream:
            asyncio.run(
                _read_stream(
                    nx=nx,
                    path=path,
                    prompt=prompt,
                    model=model,
                    max_tokens=max_tokens,
                    api_key=api_key,
                    use_search=not no_search,
                    search_mode=search_mode,
                )
            )
        elif detailed:
            asyncio.run(
                _read_detailed(
                    nx=nx,
                    path=path,
                    prompt=prompt,
                    model=model,
                    max_tokens=max_tokens,
                    api_key=api_key,
                    use_search=not no_search,
                    search_mode=search_mode,
                )
            )
        else:
            asyncio.run(
                _read_simple(
                    nx=nx,
                    path=path,
                    prompt=prompt,
                    model=model,
                    max_tokens=max_tokens,
                    api_key=api_key,
                    use_search=not no_search,
                    search_mode=search_mode,
                )
            )

        nx.close()
    except Exception as e:
        handle_error(e)


async def _read_simple(
    nx: Any,
    path: str,
    prompt: str,
    model: str,
    max_tokens: int,
    api_key: str | None,
    use_search: bool,
    search_mode: str,
) -> None:
    """Simple read - just print the answer."""
    console.print(f"[cyan]Reading:[/cyan] {path}")
    console.print(f"[cyan]Question:[/cyan] {prompt}")
    console.print()

    answer = await nx.llm_read(
        path=path,
        prompt=prompt,
        model=model,
        max_tokens=max_tokens,
        api_key=api_key,
        use_search=use_search,
        search_mode=search_mode,
    )

    console.print("[green]Answer:[/green]")
    console.print(answer)


async def _read_detailed(
    nx: Any,
    path: str,
    prompt: str,
    model: str,
    max_tokens: int,
    api_key: str | None,
    use_search: bool,
    search_mode: str,
) -> None:
    """Detailed read - show answer with citations and metadata."""
    console.print(f"[cyan]Reading:[/cyan] {path}")
    console.print(f"[cyan]Question:[/cyan] {prompt}")
    console.print()

    with console.status("[bold cyan]Processing..."):
        result = await nx.llm_read_detailed(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            api_key=api_key,
            use_search=use_search,
            search_mode=search_mode,
        )

    # Print answer
    console.print("[green]Answer:[/green]")
    console.print(result.answer)
    console.print()

    # Print sources
    console.print(f"[yellow]Sources ({len(result.sources)}):[/yellow]")
    for source in result.sources:
        console.print(f"  • {source}")
    console.print()

    # Print citations if available
    if result.citations:
        console.print(f"[yellow]Citations ({len(result.citations)}):[/yellow]")
        for i, citation in enumerate(result.citations[:10], start=1):  # Show first 10
            score_str = f" (score: {citation.score:.2f})" if citation.score else ""
            chunk_str = (
                f" [chunk {citation.chunk_index}]" if citation.chunk_index is not None else ""
            )
            console.print(f"  {i}. {citation.path}{chunk_str}{score_str}")
        if len(result.citations) > 10:
            console.print(f"  ... and {len(result.citations) - 10} more")
        console.print()

    # Print metadata
    console.print("[yellow]Metadata:[/yellow]")
    if result.tokens_used:
        console.print(f"  Tokens: {result.tokens_used:,}")
    if result.cost:
        console.print(f"  Cost: ${result.cost:.4f}")
    if result.cached:
        console.print("  [green]Cached response[/green]")
        if result.cache_savings:
            console.print(f"  Cache savings: ${result.cache_savings:.4f}")


async def _read_stream(
    nx: Any,
    path: str,
    prompt: str,
    model: str,
    max_tokens: int,
    api_key: str | None,
    use_search: bool,
    search_mode: str,
) -> None:
    """Stream read - show response as it's generated."""
    console.print(f"[cyan]Reading:[/cyan] {path}")
    console.print(f"[cyan]Question:[/cyan] {prompt}")
    console.print()
    console.print("[green]Answer:[/green]")

    async for chunk in nx.llm_read_stream(
        path=path,
        prompt=prompt,
        model=model,
        max_tokens=max_tokens,
        api_key=api_key,
        use_search=use_search,
        search_mode=search_mode,
    ):
        # Print chunk without newline
        sys.stdout.write(chunk)
        sys.stdout.flush()

    # Final newline
    print()
