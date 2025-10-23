"""Firecrawl plugin for production-grade web scraping integration."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from nexus_firecrawl.client import FirecrawlClient

# Try to import nexus components, but make them optional for standalone testing
try:
    from nexus.plugins import NexusPlugin, PluginMetadata
except ImportError:
    # Stub for development
    from abc import ABC
    from dataclasses import dataclass

    @dataclass
    class PluginMetadata:  # type: ignore[no-redef]
        name: str
        version: str
        description: str
        author: str
        homepage: Optional[str] = None
        requires: Optional[list[str]] = None

    class NexusPlugin(ABC):  # type: ignore[no-redef]
        def __init__(self, nexus_fs: Any = None) -> None:
            self._nexus_fs = nexus_fs
            self._config: dict[str, Any] = {}
            self._enabled = True

        @property
        def nx(self) -> Any:
            return self._nexus_fs

        def get_config(self, key: str, default: Any = None) -> Any:
            return self._config.get(key, default)

        def is_enabled(self) -> bool:
            return self._enabled


console = Console()
console_err = Console(stderr=True)


class FirecrawlPlugin(NexusPlugin):
    """Plugin for Firecrawl integration.

    Provides production-grade web scraping with:
    - JS rendering (Puppeteer/Playwright)
    - Anti-bot detection handling
    - Proxy rotation
    - PDF/complex document support
    - LLM-ready markdown output
    """

    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="firecrawl",
            version="0.1.0",
            description="Production-grade web scraping integration using Firecrawl",
            author="Nexus Team",
            homepage="https://github.com/nexi-lab/nexus-plugin-firecrawl",
            requires=[],
        )

    def commands(self) -> dict[str, Callable]:
        """Return plugin commands."""
        return {
            "scrape": self.scrape,
            "crawl": self.crawl,
            "map": self.map_url,
            "search": self.search,
            "extract": self.extract,
            "pipe": self.pipe,
        }

    def _get_client(self, api_key: Optional[str] = None) -> FirecrawlClient:
        """Get Firecrawl client with API key.

        Args:
            api_key: Optional API key override

        Returns:
            Configured Firecrawl client

        Raises:
            ValueError: If API key not found
        """
        api_key = api_key or self.get_config("api_key") or os.getenv("FIRECRAWL_API_KEY")
        if not api_key:
            raise ValueError(
                "Firecrawl API key not found. Set FIRECRAWL_API_KEY environment variable "
                "or configure in plugin settings."
            )

        base_url = self.get_config("base_url", "https://api.firecrawl.dev/v1")
        timeout = int(self.get_config("timeout", 60))
        max_retries = int(self.get_config("max_retries", 3))

        return FirecrawlClient(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    def _is_piped_output(self) -> bool:
        """Check if output is being piped (not a TTY)."""
        return not sys.stdout.isatty()

    def _url_to_path(self, url: str) -> str:
        """Convert URL to a safe file path.

        Args:
            url: URL to convert

        Returns:
            Safe file path
        """
        parsed = urlparse(url)
        domain = parsed.netloc.replace(".", "_")
        path = parsed.path.strip("/").replace("/", "_")

        if path:
            return f"{domain}/{path}.md"
        return f"{domain}/index.md"

    async def scrape(
        self,
        url: str,
        output: Optional[str] = None,
        json_output: bool = False,
        api_key: Optional[str] = None,
        only_main_content: bool = True,
        save_to_nexus: bool = True,
    ) -> None:
        """Scrape a single URL.

        Args:
            url: URL to scrape
            output: Output file path (optional)
            json_output: Output as JSON instead of markdown
            api_key: Firecrawl API key (optional, uses config or env)
            only_main_content: Extract only main content (default: True)
            save_to_nexus: Save to NexusFS if available (default: True)
        """
        try:
            client = self._get_client(api_key)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return

        try:
            if not self._is_piped_output():
                console.print(f"[cyan]Scraping:[/cyan] {url}")

            async with client:
                result = await client.scrape(url, only_main_content=only_main_content)

            if not result.success:
                console.print("[red]Scraping failed[/red]")
                return

            # Handle JSON output
            if json_output or self._is_piped_output():
                output_data = {
                    "url": url,
                    "markdown": result.markdown,
                    "metadata": result.metadata,
                }
                print(json.dumps(output_data, indent=2))
                return

            # Handle file output
            if output:
                output_path = Path(output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(result.markdown or "")
                console.print(f"[green]✓ Saved to:[/green] {output}")

            # Save to NexusFS
            if save_to_nexus and self.nx and result.markdown:
                file_path = output or f"scraped/{self._url_to_path(url)}"
                nexus_path = f"/workspace/{file_path}"

                # Create directory
                dir_path = str(Path(nexus_path).parent)
                self.nx.mkdir(dir_path, parents=True, exist_ok=True)

                # Write content
                self.nx.write(nexus_path, result.markdown.encode("utf-8"))
                console.print(f"[green]✓ Saved to NexusFS:[/green] {nexus_path}")

            # Display content if no output specified
            if not output and not json_output:
                console.print("\n[bold]Content:[/bold]")
                console.print(result.markdown or "No content")

        except Exception as e:
            console.print(f"[red]Failed to scrape: {e}[/red]")
            import traceback

            traceback.print_exc()

    async def crawl(
        self,
        url: str,
        max_pages: int = 100,
        max_depth: Optional[int] = None,
        include_paths: Optional[list[str]] = None,
        exclude_paths: Optional[list[str]] = None,
        api_key: Optional[str] = None,
        save_to_nexus: bool = True,
        output_dir: Optional[str] = None,
    ) -> None:
        """Crawl a website and scrape all pages.

        Args:
            url: Base URL to crawl
            max_pages: Maximum number of pages (default: 100)
            max_depth: Maximum crawl depth (optional)
            include_paths: URL patterns to include (e.g., ["/api/**"])
            exclude_paths: URL patterns to exclude (e.g., ["/blog/**"])
            api_key: Firecrawl API key (optional, uses config or env)
            save_to_nexus: Save to NexusFS if available (default: True)
            output_dir: Output directory for scraped files (optional)
        """
        try:
            client = self._get_client(api_key)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return

        try:
            console.print(f"[cyan]Starting crawl:[/cyan] {url}")
            console.print(f"Max pages: {max_pages}")

            async with client:
                # Start crawl
                job = await client.crawl(
                    url=url,
                    limit=max_pages,
                    max_depth=max_depth,
                    include_paths=include_paths,
                    exclude_paths=exclude_paths,
                )

                console.print(f"[cyan]Crawl job started:[/cyan] {job.id}")

                # Poll for completion
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                ) as progress:
                    task = progress.add_task("Crawling...", total=None)

                    while job.status not in ["completed", "failed"]:
                        await asyncio.sleep(2)
                        job = await client.get_crawl_status(job.id)

                        if job.total and job.completed:
                            progress.update(
                                task,
                                description=f"Crawling... ({job.completed}/{job.total} pages)",
                            )

                if job.status == "failed":
                    console.print("[red]Crawl failed[/red]")
                    return

                console.print(f"[green]✓ Crawl completed:[/green] {len(job.data)} pages")

                # Save results
                base_path = output_dir or f"crawled/{urlparse(url).netloc}"

                for i, page in enumerate(job.data):
                    page_url = page.get("url", "")
                    markdown = page.get("markdown", "")

                    if not markdown:
                        continue

                    # Generate file path
                    file_path = f"{base_path}/{self._url_to_path(page_url)}"

                    # Save to file if output_dir specified
                    if output_dir:
                        output_path = Path(file_path)
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_text(markdown)

                    # Save to NexusFS
                    if save_to_nexus and self.nx:
                        nexus_path = f"/workspace/{file_path}"
                        dir_path = str(Path(nexus_path).parent)
                        self.nx.mkdir(dir_path, parents=True, exist_ok=True)
                        self.nx.write(nexus_path, markdown.encode("utf-8"))

                    if (i + 1) % 10 == 0:
                        console.print(f"  Saved {i + 1}/{len(job.data)} pages...")

                console.print(f"[green]✓ Saved all pages to:[/green] {base_path}")

        except Exception as e:
            console.print(f"[red]Failed to crawl: {e}[/red]")
            import traceback

            traceback.print_exc()

    async def map_url(
        self,
        url: str,
        search: Optional[str] = None,
        limit: int = 5000,
        api_key: Optional[str] = None,
    ) -> None:
        """Map all URLs on a website.

        Args:
            url: Base URL to map
            search: Optional search query to filter URLs
            limit: Maximum number of URLs (default: 5000)
            api_key: Firecrawl API key (optional, uses config or env)
        """
        try:
            client = self._get_client(api_key)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return

        try:
            console.print(f"[cyan]Mapping URLs:[/cyan] {url}")

            async with client:
                result = await client.map_url(url, search=search, limit=limit)

            if not result.success:
                console.print("[red]URL mapping failed[/red]")
                return

            # Output for piping
            if self._is_piped_output():
                for link in result.links:
                    print(link)
                return

            # Pretty table output
            console.print(f"\n[green]Found {len(result.links)} URLs[/green]\n")

            table = Table(title="Discovered URLs")
            table.add_column("#", style="dim", width=6)
            table.add_column("URL", style="cyan")

            for i, link in enumerate(result.links[:100], 1):  # Show first 100
                table.add_row(str(i), link)

            console.print(table)

            if len(result.links) > 100:
                console.print(f"\n[dim]... and {len(result.links) - 100} more URLs[/dim]")

        except Exception as e:
            console.print(f"[red]Failed to map URLs: {e}[/red]")
            import traceback

            traceback.print_exc()

    async def search(
        self,
        query: str,
        limit: int = 10,
        scrape: bool = True,
        api_key: Optional[str] = None,
        save_to_nexus: bool = False,
    ) -> None:
        """Search the web and optionally scrape results.

        Args:
            query: Search query
            limit: Number of results (default: 10)
            scrape: Whether to scrape each result (default: True)
            api_key: Firecrawl API key (optional, uses config or env)
            save_to_nexus: Save results to NexusFS (default: False)
        """
        try:
            client = self._get_client(api_key)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return

        try:
            console.print(f"[cyan]Searching:[/cyan] {query}")

            async with client:
                results = await client.search(query, limit=limit, scrape_results=scrape)

            if not results:
                console.print("[yellow]No results found[/yellow]")
                return

            # Output for piping
            if self._is_piped_output():
                print(json.dumps(results, indent=2))
                return

            # Pretty output
            console.print(f"\n[green]Found {len(results)} results[/green]\n")

            for i, result in enumerate(results, 1):
                console.print(f"[bold cyan]{i}. {result.get('title', 'No title')}[/bold cyan]")
                console.print(f"   URL: {result.get('url', 'N/A')}")

                if scrape and result.get("markdown"):
                    snippet = (
                        result["markdown"][:200] + "..."
                        if len(result["markdown"]) > 200
                        else result["markdown"]
                    )
                    console.print(f"   {snippet}")

                console.print()

                # Save to NexusFS if requested
                if save_to_nexus and self.nx and result.get("markdown"):
                    url_path = self._url_to_path(result["url"])
                    nexus_path = f"/workspace/search/{query}/{url_path}"
                    dir_path = str(Path(nexus_path).parent)
                    self.nx.mkdir(dir_path, parents=True, exist_ok=True)
                    self.nx.write(nexus_path, result["markdown"].encode("utf-8"))

        except Exception as e:
            console.print(f"[red]Failed to search: {e}[/red]")
            import traceback

            traceback.print_exc()

    async def extract(
        self,
        url: str,
        schema: str,
        prompt: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        """Extract structured data from a URL using a schema.

        Args:
            url: URL to extract from
            schema: JSON schema file or inline JSON string
            prompt: Optional extraction prompt
            api_key: Firecrawl API key (optional, uses config or env)
        """
        try:
            client = self._get_client(api_key)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return

        try:
            # Load schema
            schema_dict: dict[str, Any]
            if Path(schema).exists():
                with open(schema) as f:
                    schema_dict = json.load(f)
            else:
                schema_dict = json.loads(schema)

            console.print(f"[cyan]Extracting from:[/cyan] {url}")

            async with client:
                result = await client.extract(url, schema_dict, prompt=prompt)

            # Output (always JSON for structured data)
            print(json.dumps(result, indent=2))

        except Exception as e:
            console.print(f"[red]Failed to extract: {e}[/red]")
            import traceback

            traceback.print_exc()

    async def pipe(
        self,
        url: str,
        api_key: Optional[str] = None,
    ) -> None:
        """Scrape URL and output JSON for piping to other commands.

        This is optimized for Unix pipelines:
        nexus firecrawl pipe <url> | nexus skills create-from-web

        Args:
            url: URL to scrape
            api_key: Firecrawl API key (optional, uses config or env)
        """
        try:
            client = self._get_client(api_key)
        except ValueError as e:
            # For piping, output error to stderr
            console_err.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

        try:
            async with client:
                result = await client.scrape(url, only_main_content=True)

            if not result.success:
                console_err.print("[red]Scraping failed[/red]")
                sys.exit(1)

            # Output clean JSON to stdout
            output_data = {
                "url": url,
                "content": result.markdown,
                "title": result.metadata.get("title", ""),
                "description": result.metadata.get("description", ""),
                "metadata": result.metadata,
            }
            print(json.dumps(output_data))

        except Exception as e:
            console_err.print(f"[red]Failed: {e}[/red]")
            sys.exit(1)
