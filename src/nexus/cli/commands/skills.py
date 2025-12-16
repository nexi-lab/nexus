"""Nexus CLI Skills Commands - Manage reusable AI agent skills.

The Skills System provides vendor-neutral skill management with:
- SKILL.md format with YAML frontmatter
- Three-tier hierarchy (agent > tenant > system)
- Dependency resolution with DAG and cycle detection
- Vendor-neutral export to .zip packages
- Skill lifecycle management (create, fork, publish)
- Usage analytics and governance
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any
from urllib.parse import urlparse

import click
from rich.table import Table

from nexus.cli.utils import (
    BackendConfig,
    add_backend_options,
    console,
    get_filesystem,
    handle_error,
)


class SQLAlchemyDatabaseConnection:
    """Wrapper for SQLAlchemy session to match DatabaseConnection protocol."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def execute(self, query: str, params: dict | None = None) -> Any:
        """Execute a query."""
        from sqlalchemy import text

        return self._session.execute(text(query), params or {})

    def fetchall(self, query: str, params: dict | None = None) -> list[dict]:
        """Fetch all results from a query."""
        from sqlalchemy import text

        result = self._session.execute(text(query), params or {})
        return [dict(row._mapping) for row in result]

    def fetchone(self, query: str, params: dict | None = None) -> dict | None:
        """Fetch one result from a query."""
        from sqlalchemy import text

        result = self._session.execute(text(query), params or {})
        row = result.fetchone()
        return dict(row._mapping) if row else None

    def commit(self) -> None:
        """Commit the transaction."""
        self._session.commit()


def _get_database_connection() -> SQLAlchemyDatabaseConnection | None:
    """Get database connection for skill governance.

    Returns wrapped SQLAlchemy session using NEXUS_DATABASE_URL environment variable.
    Returns None if not configured (falls back to in-memory storage).
    """
    import os

    db_url = os.getenv("NEXUS_DATABASE_URL")
    if not db_url:
        return None

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    try:
        engine = create_engine(db_url, echo=False)
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        return SQLAlchemyDatabaseConnection(session)
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Could not connect to database: {e}")
        console.print("[dim]Falling back to in-memory governance storage[/dim]")
        return None


def register_commands(cli: click.Group) -> None:
    """Register skills commands with the main CLI group.

    Args:
        cli: The main Click group to register commands with
    """
    cli.add_command(skills)


@click.group(name="skills")
def skills() -> None:
    """Skills System - Manage reusable AI agent skills.

    The Skills System provides vendor-neutral skill management with:
    - SKILL.md format with YAML frontmatter
    - Three-tier hierarchy (agent > tenant > system)
    - Dependency resolution with DAG and cycle detection
    - Vendor-neutral export to .zip packages
    - Skill lifecycle management (create, fork, publish)
    - Usage analytics and governance

    Examples:
        nexus skills list
        nexus skills create my-skill --description "My custom skill"
        nexus skills fork analyze-code my-analyzer
        nexus skills publish my-skill
        nexus skills export my-skill --output ./my-skill.zip --format claude
    """
    pass


@skills.command(name="list")
@click.option("--user", is_flag=True, help="Show user-level skills")
@click.option("--tenant", is_flag=True, help="Show tenant-wide skills")
@click.option("--system", is_flag=True, help="Show system skills")
@click.option(
    "--tier", type=click.Choice(["agent", "user", "tenant", "system"]), help="Filter by tier"
)
@add_backend_options
def skills_list(
    user: bool,
    tenant: bool,
    system: bool,
    tier: str | None,
    backend_config: BackendConfig,
) -> None:
    """List all skills.

    Examples:
        nexus skills list
        nexus skills list --user
        nexus skills list --tenant
        nexus skills list --system
        nexus skills list --tier agent
    """
    try:
        nx = get_filesystem(backend_config, enforce_permissions=False)

        # Determine tier filter
        if tier:
            tier_filter = tier
        elif user:
            tier_filter = "user"
        elif tenant:
            tier_filter = "tenant"
        elif system:
            tier_filter = "system"
        else:
            tier_filter = None

        # Use RPC endpoint directly
        result = nx.skills_list(tier=tier_filter, include_metadata=True)  # type: ignore[attr-defined]

        skills_data = result.get("skills", [])

        if not skills_data:
            console.print("[yellow]No skills found[/yellow]")
            nx.close()
            return

        # Display skills in table
        table = Table(title=f"Skills ({result['count']} found)")
        table.add_column("Name", style="cyan", no_wrap=False)
        table.add_column("Description", style="green")
        table.add_column("Version", style="yellow")
        table.add_column("Tier", style="magenta")

        for skill in skills_data:
            if isinstance(skill, dict):
                table.add_row(
                    skill.get("name", "N/A"),
                    skill.get("description", "N/A"),
                    skill.get("version", "N/A"),
                    skill.get("tier", "N/A"),
                )

        console.print(table)
        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="create")
@click.argument("name", type=str)
@click.option("--description", required=True, help="Skill description")
@click.option("--template", default="basic", help="Template to use (basic, data-analysis, etc.)")
@click.option(
    "--tier",
    type=click.Choice(["agent", "user", "tenant", "system"]),
    default="user",
    help="Target tier",
)
@click.option("--author", help="Author name")
@add_backend_options
def skills_create(
    name: str,
    description: str,
    template: str,
    tier: str,
    author: str | None,
    backend_config: BackendConfig,
) -> None:
    """Create a new skill from template.

    Examples:
        nexus skills create my-skill --description "My custom skill"
        nexus skills create data-viz --description "Data visualization" --template data-analysis
        nexus skills create analyzer --description "Code analyzer" --author Alice
    """
    try:
        # Get filesystem with permission enforcement disabled for skills operations
        nx = get_filesystem(backend_config, enforce_permissions=False)

        # Use RPC endpoint directly
        result = nx.skills_create(  # type: ignore[attr-defined]
            name=name,
            description=description,
            template=template,
            tier=tier,
            author=author,
        )

        console.print(f"[green]✓[/green] Created skill [cyan]{name}[/cyan]")
        console.print(f"  Path: [dim]{result['skill_path']}[/dim]")
        console.print(f"  Tier: [yellow]{tier}[/yellow]")
        console.print(f"  Template: [yellow]{template}[/yellow]")

        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="create-from-web")
@click.option("--name", help="Skill name (auto-generated from URL/title if not provided)")
@click.option(
    "--tier",
    type=click.Choice(["agent", "user", "tenant", "system"]),
    default="user",
    help="Target tier",
)
@click.option("--stdin", is_flag=True, help="Read JSON input from stdin (for piping)")
@click.option("--json", "json_output", is_flag=True, help="Output JSON for piping to next command")
@click.option("--author", help="Author name")
@add_backend_options
def skills_create_from_web(
    name: str | None,
    tier: str,
    stdin: bool,
    json_output: bool,
    author: str | None,
    backend_config: BackendConfig,
) -> None:
    """Create skill from web content (supports Unix piping).

    This command accepts JSON input from stdin (typically from a web scraper)
    and creates a SKILL.md file from the content.

    Expected JSON format:
        {
            "type": "scraped_content",
            "url": "https://example.com",
            "content": "markdown content...",
            "title": "Page Title",
            "metadata": {...}
        }

    Examples:
        # With pipe from firecrawl
        nexus firecrawl scrape https://docs.stripe.com/api --json | \\
            nexus skills create-from-web --stdin --name stripe-api

        # Auto-generate name from URL
        nexus firecrawl scrape https://docs.example.com --json | \\
            nexus skills create-from-web --stdin

        # Full pipeline with JSON output
        nexus firecrawl scrape https://docs.example.com --json | \\
            nexus skills create-from-web --stdin --json | \\
            nexus anthropic upload-skill --stdin
    """
    try:
        import asyncio

        from nexus.skills import SkillManager, SkillRegistry

        # Read from stdin if piped or --stdin flag
        if stdin or not sys.stdin.isatty():
            try:
                input_data = json.load(sys.stdin)
            except json.JSONDecodeError:
                console.print("[red]Error: Invalid JSON from stdin[/red]")
                console.print("[yellow]Expected format from web scraper:[/yellow]")
                console.print('  {"type": "scraped_content", "url": "...", "content": "..."}')
                sys.exit(1)

            # Extract data from input
            url = input_data.get("url", "")
            content = input_data.get("content", "")
            title = input_data.get("title", "")
            input_metadata = input_data.get("metadata", {})

            # Auto-generate skill name if not provided
            if not name:
                name = _generate_skill_name_from_url_or_title(url, title)

            # Generate description from title or URL
            description = title if title else f"Skill generated from {url}"

            # Get filesystem
            nx = get_filesystem(backend_config, enforce_permissions=False)
            registry = SkillRegistry(nx)
            manager = SkillManager(nx, registry)

            async def create_skill_from_web_async() -> None:
                # Create the skill with the scraped content
                skill_path = await manager.create_skill_from_content(
                    name=name,
                    description=description,
                    content=content,
                    tier=tier,
                    author=author,
                    source_url=url,
                    metadata=input_metadata,
                )

                # Output mode: JSON for piping or human-readable
                if json_output or not sys.stdout.isatty():
                    # JSON output for next command in pipeline
                    output = {
                        "type": "skill",
                        "name": name,
                        "path": skill_path,
                        "tier": tier,
                        "source_url": url,
                    }
                    print(json.dumps(output))
                else:
                    # Human-readable output
                    console.print(
                        f"[green]✓[/green] Created skill [cyan]{name}[/cyan] from web content"
                    )
                    console.print(f"  Path: [dim]{skill_path}[/dim]")
                    console.print(f"  Tier: [yellow]{tier}[/yellow]")
                    console.print(f"  Source: [cyan]{url}[/cyan]")

            asyncio.run(create_skill_from_web_async())
            nx.close()

        else:
            console.print("[red]Error: No input provided[/red]")
            console.print("[yellow]This command requires piped JSON input from stdin.[/yellow]")
            console.print("\nUsage:")
            console.print(
                "  nexus firecrawl scrape <url> --json | nexus skills create-from-web --stdin"
            )
            sys.exit(1)

    except Exception as e:
        handle_error(e)


@skills.command(name="create-from-file")
@click.argument("source", type=str)
@click.option("--name", help="Skill name (auto-generated from source if not provided)")
@click.option(
    "--tier",
    type=click.Choice(["agent", "user", "tenant", "system"]),
    default="user",
    help="Target tier",
)
@click.option("--description", help="Skill description")
@click.option("--ai", is_flag=True, help="Enable AI enhancement (requires API key)")
@click.option("--no-tables", is_flag=True, help="Disable table extraction (enabled by default)")
@click.option("--no-images", is_flag=True, help="Disable image extraction (enabled by default)")
@click.option(
    "--no-ocr", is_flag=True, help="Disable OCR for scanned PDFs (auto-detected by default)"
)
@click.option("--author", help="Author name")
@add_backend_options
def skills_create_from_file(
    source: str,
    name: str | None,
    tier: str,
    description: str | None,
    ai: bool,
    no_tables: bool,
    no_images: bool,
    no_ocr: bool,
    author: str | None,
    backend_config: BackendConfig,
) -> None:
    """Create skill from file or URL (auto-detects type).

    Smart defaults: tables, images, and OCR are enabled automatically.
    The tool intelligently extracts everything useful from your documents.

    Automatically detects the source type and uses the appropriate extractor:
    - PDF files (.pdf) - extracts text, tables, images
    - URLs (http://, https://) - scrapes web content
    - Markdown files (.md) - parses markdown
    - Text files (.txt) - plain text

    Examples:
        # Simple - extract everything automatically
        nexus skills create-from-file requirement.pdf

        # From URL
        nexus skills create-from-file https://docs.stripe.com/api

        # With AI enhancement
        export ANTHROPIC_API_KEY="sk-ant-..."
        nexus skills create-from-file manual.pdf --ai

        # Disable specific features if needed
        nexus skills create-from-file doc.pdf --no-tables --no-images
    """
    try:
        from pathlib import Path
        from urllib.parse import urlparse

        # Detect source type
        is_url = source.startswith(("http://", "https://"))
        is_pdf = source.lower().endswith(".pdf")

        # Auto-generate skill name if not provided
        if not name:
            if is_url:
                # Extract from URL
                parsed = urlparse(source)
                name = parsed.path.strip("/").split("/")[-1] or parsed.netloc
                name = name.lower().replace(".", "-").replace("_", "-")
            else:
                # Extract from filename
                name = Path(source).stem.lower().replace(" ", "-").replace("_", "-")

        # Get filesystem
        nx = get_filesystem(backend_config, enforce_permissions=False)

        console.print(f"[cyan]Generating skill from:[/cyan] {source}")
        console.print(
            f"  Type: [yellow]{'URL' if is_url else 'PDF' if is_pdf else 'File'}[/yellow]"
        )
        console.print(f"  Name: [yellow]{name}[/yellow]")
        console.print(f"  Tier: [yellow]{tier}[/yellow]")
        console.print()

        # Handle file upload for PDF
        file_data = None
        if is_pdf and Path(source).exists():
            import base64

            with open(source, "rb") as f:
                file_data = base64.b64encode(f.read()).decode("utf-8")
        elif is_pdf:
            console.print(f"[red]Error: File not found: {source}[/red]")
            sys.exit(1)

        # Use RPC endpoint with smart defaults
        try:
            result = nx.skills_create_from_file(  # type: ignore[attr-defined]
                source=source,
                file_data=file_data,
                name=name,
                description=description,
                tier=tier,
                use_ai=ai,
                use_ocr=not no_ocr,  # Smart default: enabled unless --no-ocr
                extract_tables=not no_tables,  # Smart default: enabled unless --no-tables
                extract_images=not no_images,  # Smart default: enabled unless --no-images
                _author=author,
            )

            skill_path = result.get("skill_path")
            if skill_path:
                console.print()
                console.print("[green]✓[/green] Skill generated successfully")
                console.print(f"  Path: [cyan]{skill_path}[/cyan]")
                console.print()
                console.print("View with:")
                console.print(f"  nexus cat {skill_path}")
                console.print(f"  nexus skills info {name}")
            else:
                console.print("[red]✗ Failed to generate skill[/red]")
                sys.exit(1)
        except Exception as e:
            console.print(f"[red]✗ Error: {e}[/red]")
            sys.exit(1)
        finally:
            nx.close()

    except Exception as e:
        handle_error(e)


def _generate_skill_name_from_url_or_title(url: str, title: str) -> str:
    """Generate a skill name from URL or title.

    Args:
        url: Source URL
        title: Page title

    Returns:
        Generated skill name (lowercase, hyphenated)
    """
    if title:
        # Use title: convert to lowercase, replace spaces/special chars with hyphens
        name = re.sub(r"[^a-z0-9]+", "-", title.lower())
        name = name.strip("-")
    elif url:
        # Use URL path: extract meaningful part
        parsed = urlparse(url)
        path = parsed.path.strip("/")

        if path:
            # Use last segment of path
            segments = path.split("/")
            last_segment = segments[-1]

            # Remove file extensions
            name = re.sub(r"\.(html|md|txt|php|asp)$", "", last_segment)
            name = re.sub(r"[^a-z0-9]+", "-", name.lower())
            name = name.strip("-")
        else:
            # Use domain name
            domain = parsed.netloc.replace("www.", "")
            name = re.sub(r"[^a-z0-9]+", "-", domain.lower())
            name = name.strip("-")
    else:
        # Fallback: generate timestamp-based name
        import datetime

        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"skill-{timestamp}"

    return name or "unnamed-skill"


@skills.command(name="fork")
@click.argument("source_skill", type=str)
@click.argument("target_skill", type=str)
@click.option(
    "--tier",
    type=click.Choice(["agent", "user", "tenant", "system"]),
    default="user",
    help="Target tier",
)
@click.option("--author", help="Author name for the fork")
@add_backend_options
def skills_fork(
    source_skill: str,
    target_skill: str,
    tier: str,
    author: str | None,
    backend_config: BackendConfig,
) -> None:
    """Fork an existing skill.

    Examples:
        nexus skills fork analyze-code my-analyzer
        nexus skills fork data-analysis custom-analysis --author Bob
    """
    try:
        nx = get_filesystem(backend_config, enforce_permissions=False)

        # Use RPC endpoint directly
        result = nx.skills_fork(  # type: ignore[attr-defined]
            source_name=source_skill,
            target_name=target_skill,
            tier=tier,
            author=author,
        )

        console.print(
            f"[green]✓[/green] Forked skill [cyan]{source_skill}[/cyan] → [cyan]{target_skill}[/cyan]"
        )
        console.print(f"  Path: [dim]{result['forked_path']}[/dim]")
        console.print(f"  Tier: [yellow]{tier}[/yellow]")

        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="publish")
@click.argument("skill_name", type=str)
@click.option(
    "--from-tier",
    type=click.Choice(["agent", "tenant", "system"]),
    default="agent",
    help="Source tier",
)
@click.option(
    "--to-tier",
    type=click.Choice(["agent", "tenant", "system"]),
    default="tenant",
    help="Target tier",
)
@add_backend_options
def skills_publish(
    skill_name: str,
    from_tier: str,
    to_tier: str,
    backend_config: BackendConfig,
) -> None:
    """Publish skill to tenant or system library.

    Examples:
        nexus skills publish my-skill
        nexus skills publish shared-skill --from-tier tenant --to-tier system
    """
    try:
        nx = get_filesystem(backend_config, enforce_permissions=False)

        # Use RPC endpoint directly
        result = nx.skills_publish(  # type: ignore[attr-defined]
            skill_name=skill_name,
            source_tier=from_tier,
            target_tier=to_tier,
        )

        console.print(f"[green]✓[/green] Published skill [cyan]{skill_name}[/cyan]")
        console.print(f"  From: [yellow]{from_tier}[/yellow] → To: [yellow]{to_tier}[/yellow]")
        console.print(f"  Path: [dim]{result['published_path']}[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="search")
@click.argument("query", type=str)
@click.option(
    "--tier", type=click.Choice(["agent", "user", "tenant", "system"]), help="Filter by tier"
)
@click.option("--limit", default=10, type=int, help="Maximum results")
@add_backend_options
def skills_search(
    query: str,
    tier: str | None,
    limit: int,
    backend_config: BackendConfig,
) -> None:
    """Search skills by description.

    Examples:
        nexus skills search "data analysis"
        nexus skills search "code" --tier tenant --limit 5
    """
    try:
        nx = get_filesystem(backend_config, enforce_permissions=False)

        # Use RPC endpoint directly
        result = nx.skills_search(query=query, tier=tier, limit=limit)  # type: ignore[attr-defined]

        results_data = result.get("results", [])

        if not results_data:
            console.print(f"[yellow]No skills match query:[/yellow] {query}")
            nx.close()
            return

        console.print(
            f"[green]Found {result['count']} skills matching[/green] [cyan]{query}[/cyan]\n"
        )

        table = Table(title=f"Search Results for '{query}'")
        table.add_column("Skill Name", style="cyan")
        table.add_column("Relevance Score", justify="right", style="yellow")

        for item in results_data:
            table.add_row(item["skill_name"], f"{item['score']:.2f}")

        console.print(table)
        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="info")
@click.argument("skill_name", type=str)
@add_backend_options
def skills_info(
    skill_name: str,
    backend_config: BackendConfig,
) -> None:
    """Show detailed skill information.

    Examples:
        nexus skills info analyze-code
        nexus skills info data-analysis
    """
    try:
        nx = get_filesystem(backend_config, enforce_permissions=False)

        # Use RPC endpoint directly
        skill_info = nx.skills_info(skill_name=skill_name)  # type: ignore[attr-defined]

        # Display skill information
        table = Table(title=f"Skill Information: {skill_name}")
        table.add_column("Property", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Name", skill_info.get("name", "N/A"))
        table.add_row("Description", skill_info.get("description", "N/A"))
        table.add_row("Version", skill_info.get("version", "N/A"))
        table.add_row("Author", skill_info.get("author", "N/A"))
        table.add_row("Tier", skill_info.get("tier", "N/A"))
        table.add_row("File Path", skill_info.get("file_path", "N/A"))

        if skill_info.get("created_at"):
            from datetime import datetime

            created = datetime.fromisoformat(skill_info["created_at"])
            table.add_row("Created", created.strftime("%Y-%m-%d %H:%M:%S"))
        if skill_info.get("modified_at"):
            from datetime import datetime

            modified = datetime.fromisoformat(skill_info["modified_at"])
            table.add_row("Modified", modified.strftime("%Y-%m-%d %H:%M:%S"))

        # Show dependencies
        if skill_info.get("requires"):
            deps_str = ", ".join(skill_info["requires"])
            table.add_row("Dependencies", deps_str)

        console.print(table)

        # Show dependencies resolved
        if skill_info.get("resolved_dependencies"):
            console.print("\n[bold]Dependency Resolution:[/bold]")
            resolved = skill_info["resolved_dependencies"]
            console.print(f"  Resolved order: [cyan]{' → '.join(resolved)}[/cyan]")

        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="export")
@click.argument("skill_name", type=str)
@click.option("--output", "-o", type=click.Path(), required=True, help="Output .zip file path")
@click.option(
    "--format",
    type=click.Choice(["generic", "claude", "openai"]),
    default="generic",
    help="Export format",
)
@click.option("--no-deps", is_flag=True, help="Exclude dependencies from export")
@add_backend_options
def skills_export(
    skill_name: str,
    output: str,
    format: str,
    no_deps: bool,
    backend_config: BackendConfig,
) -> None:
    """Export skill to .zip package.

    Examples:
        nexus skills export my-skill --output ./my-skill.zip
        nexus skills export analyze-code --output ./export.zip --format claude
        nexus skills export my-skill --output ./export.zip --no-deps
    """
    try:
        import asyncio

        from nexus.skills import SkillExporter, SkillRegistry

        nx = get_filesystem(backend_config, enforce_permissions=False)
        registry = SkillRegistry(nx)
        exporter = SkillExporter(registry)

        async def export_skill_async() -> None:
            await registry.discover()

            include_deps = not no_deps

            with console.status(
                f"[yellow]Exporting skill {skill_name}...[/yellow]", spinner="dots"
            ):
                await exporter.export_skill(
                    name=skill_name,
                    output_path=output,
                    include_dependencies=include_deps,
                )

            console.print(f"[green]✓[/green] Exported skill [cyan]{skill_name}[/cyan]")
            console.print(f"  Output: [cyan]{output}[/cyan]")
            console.print(f"  Format: [yellow]{format}[/yellow]")
            console.print(
                f"  Dependencies: [yellow]{'Included' if include_deps else 'Excluded'}[/yellow]"
            )

        asyncio.run(export_skill_async())
        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="validate")
@click.argument("skill_name", type=str)
@click.option(
    "--format",
    type=click.Choice(["generic", "claude", "openai"]),
    default="generic",
    help="Validation format",
)
@add_backend_options
def skills_validate(
    skill_name: str,
    format: str,
    backend_config: BackendConfig,
) -> None:
    """Validate skill format and size limits.

    Examples:
        nexus skills validate my-skill
        nexus skills validate analyze-code --format claude
    """
    try:
        import asyncio

        from nexus.skills import SkillExporter, SkillRegistry

        nx = get_filesystem(backend_config, enforce_permissions=False)
        registry = SkillRegistry(nx)
        exporter = SkillExporter(registry)

        async def validate_skill_async() -> None:
            await registry.discover()

            valid, message, size_bytes = await exporter.validate_export(
                name=skill_name,
                include_dependencies=True,
            )

            def format_size(size: int) -> str:
                """Format size in human-readable format."""
                size_float = float(size)
                for unit in ["B", "KB", "MB", "GB"]:
                    if size_float < 1024.0:
                        return f"{size_float:.2f} {unit}"
                    size_float /= 1024.0
                return f"{size_float:.2f} TB"

            if valid:
                console.print(
                    f"[green]✓[/green] Skill [cyan]{skill_name}[/cyan] is valid for export"
                )
                console.print(f"  Format: [yellow]{format}[/yellow]")
                console.print(f"  Total size: [cyan]{format_size(size_bytes)}[/cyan]")
                console.print(f"  Message: [dim]{message}[/dim]")
            else:
                console.print(f"[red]✗[/red] Skill [cyan]{skill_name}[/cyan] validation failed")
                console.print(f"  Format: [yellow]{format}[/yellow]")
                console.print(f"  Total size: [cyan]{format_size(size_bytes)}[/cyan]")
                console.print(f"  Error: [red]{message}[/red]")
                sys.exit(1)

        asyncio.run(validate_skill_async())
        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="size")
@click.argument("skill_name", type=str)
@click.option("--human", "-h", is_flag=True, help="Human-readable output")
@add_backend_options
def skills_size(
    skill_name: str,
    human: bool,
    backend_config: BackendConfig,
) -> None:
    """Calculate total size of skill and dependencies.

    Examples:
        nexus skills size my-skill
        nexus skills size analyze-code --human
    """
    try:
        import asyncio

        from nexus.skills import SkillExporter, SkillRegistry

        nx = get_filesystem(backend_config, enforce_permissions=False)
        registry = SkillRegistry(nx)
        exporter = SkillExporter(registry)

        async def calculate_size_async() -> None:
            await registry.discover()

            _, _, size_bytes = await exporter.validate_export(
                name=skill_name,
                include_dependencies=True,
            )

            def format_size(size: int) -> str:
                """Format size in human-readable format."""
                if not human:
                    return f"{size:,} bytes"

                size_float = float(size)
                for unit in ["B", "KB", "MB", "GB"]:
                    if size_float < 1024.0:
                        return f"{size_float:.2f} {unit}"
                    size_float /= 1024.0
                return f"{size_float:.2f} TB"

            console.print(f"[bold cyan]Size of {skill_name} (with dependencies):[/bold cyan]")
            console.print(f"  Total size: [green]{format_size(size_bytes)}[/green]")

        asyncio.run(calculate_size_async())
        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="deps")
@click.argument("skill_name", type=str)
@click.option("--visual/--no-visual", default=True, help="Show visual tree (default: True)")
@add_backend_options
def skills_deps(
    skill_name: str,
    visual: bool,
    backend_config: BackendConfig,
) -> None:
    """Show skill dependencies as a visual tree.

    Examples:
        nexus skills deps my-skill
        nexus skills deps analyze-code --no-visual
    """
    try:
        import asyncio

        from nexus.skills import SkillRegistry

        nx = get_filesystem(backend_config, enforce_permissions=False)
        registry = SkillRegistry(nx)

        async def show_deps_async() -> None:
            await registry.discover()

            # Get the skill to verify it exists
            skill = await registry.get_skill(skill_name)

            if visual:
                # Build visual dependency tree
                from rich.tree import Tree

                tree = Tree(f"[bold cyan]{skill_name}[/bold cyan]", guide_style="dim")

                async def add_dependencies(
                    parent_tree: Tree, skill_name: str, visited: set[str]
                ) -> None:
                    """Recursively add dependencies to tree."""
                    if skill_name in visited:
                        parent_tree.add(f"[dim]{skill_name} (circular reference)[/dim]")
                        return

                    visited.add(skill_name)

                    try:
                        skill_obj = await registry.get_skill(skill_name)
                        deps = skill_obj.metadata.requires or []

                        for dep in deps:
                            dep_metadata = registry.get_metadata(dep)
                            dep_desc = dep_metadata.description or "No description"

                            # Truncate description
                            if len(dep_desc) > 50:
                                dep_desc = dep_desc[:47] + "..."

                            dep_node = parent_tree.add(
                                f"[green]{dep}[/green] - [dim]{dep_desc}[/dim]"
                            )

                            # Recursively add dependencies
                            await add_dependencies(dep_node, dep, visited.copy())
                    except Exception as e:
                        parent_tree.add(f"[red]{skill_name} (error: {e})[/red]")

                # Add dependencies to the tree
                visited: set[str] = set()
                deps = skill.metadata.requires or []

                if not deps:
                    tree.add("[yellow]No dependencies[/yellow]")
                else:
                    for dep in deps:
                        dep_metadata = registry.get_metadata(dep)
                        dep_desc = dep_metadata.description or "No description"

                        if len(dep_desc) > 50:
                            dep_desc = dep_desc[:47] + "..."

                        dep_node = tree.add(f"[green]{dep}[/green] - [dim]{dep_desc}[/dim]")

                        # Recursively add sub-dependencies
                        await add_dependencies(dep_node, dep, visited.copy())

                console.print()
                console.print(tree)
                console.print()

                # Show total dependency count
                all_deps = await registry.resolve_dependencies(skill_name)
                total_deps = len(all_deps) - 1  # Exclude the skill itself
                console.print(f"[dim]Total dependencies: {total_deps}[/dim]")

            else:
                # Simple list format
                deps = await registry.resolve_dependencies(skill_name)

                console.print(f"\n[bold cyan]Dependencies for {skill_name}:[/bold cyan]")

                if len(deps) == 1:
                    console.print("  [yellow]No dependencies[/yellow]")
                else:
                    console.print("  [dim]Resolution order:[/dim]")
                    for i, dep in enumerate(deps):
                        if dep == skill_name:
                            console.print(f"  {i + 1}. [bold cyan]{dep}[/bold cyan] (self)")
                        else:
                            dep_metadata = registry.get_metadata(dep)
                            console.print(f"  {i + 1}. [green]{dep}[/green]")
                            if dep_metadata.description:
                                console.print(f"      [dim]{dep_metadata.description}[/dim]")

        asyncio.run(show_deps_async())
        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="submit-approval")
@click.argument("skill_name", type=str)
@click.option("--submitted-by", required=True, help="Submitter ID (user or agent)")
@click.option("--reviewers", help="Comma-separated list of reviewer IDs")
@click.option("--comments", help="Optional submission comments")
@add_backend_options
def skills_submit_approval(
    skill_name: str,
    submitted_by: str,
    reviewers: str | None,
    comments: str | None,
    backend_config: BackendConfig,  # noqa: ARG001
) -> None:
    """Submit a skill for approval to publish to tenant library.

    Examples:
        nexus skills submit-approval my-analyzer --submitted-by alice
        nexus skills submit-approval code-review --submitted-by alice --reviewers bob,charlie
        nexus skills submit-approval my-skill --submitted-by alice --comments "Ready for team use"
    """
    try:
        nx = get_filesystem(backend_config, enforce_permissions=False)

        # Parse reviewers list
        reviewer_list = [r.strip() for r in reviewers.split(",")] if reviewers else None

        # Use RPC endpoint directly
        result = nx.skills_submit_approval(  # type: ignore[attr-defined]
            skill_name=skill_name,
            submitted_by=submitted_by,
            reviewers=reviewer_list,
            comments=comments,
        )

        console.print(f"[green]✓[/green] Submitted skill [cyan]{skill_name}[/cyan] for approval")
        console.print(f"  Approval ID: [yellow]{result['approval_id']}[/yellow]")
        console.print(f"  Submitted by: [cyan]{submitted_by}[/cyan]")
        if reviewer_list:
            console.print(f"  Reviewers: [cyan]{', '.join(reviewer_list)}[/cyan]")
        if comments:
            console.print(f"  Comments: [dim]{comments}[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="approve")
@click.argument("approval_id", type=str)
@click.option("--reviewed-by", required=True, help="Reviewer ID")
@click.option(
    "--reviewer-type", default="user", type=click.Choice(["user", "agent"]), help="Reviewer type"
)
@click.option("--comments", help="Optional review comments")
@click.option("--tenant-id", help="Tenant ID for scoping")
@add_backend_options
def skills_approve(
    approval_id: str,
    reviewed_by: str,
    reviewer_type: str,
    comments: str | None,
    tenant_id: str | None,
    backend_config: BackendConfig,  # noqa: ARG001
) -> None:
    """Approve a skill for publication.

    Examples:
        nexus skills approve <approval-id> --reviewed-by bob
        nexus skills approve <approval-id> --reviewed-by bob --comments "Code quality excellent!"
        nexus skills approve <approval-id> --reviewed-by manager-id --reviewer-type user --tenant-id acme-corp
    """
    try:
        nx = get_filesystem(backend_config, enforce_permissions=False)

        # Use RPC endpoint directly
        nx.skills_approve(  # type: ignore[attr-defined]
            approval_id=approval_id,
            reviewed_by=reviewed_by,
            reviewer_type=reviewer_type,
            comments=comments,
            tenant_id=tenant_id,
        )

        console.print(f"[green]✓[/green] Approved skill (Approval ID: [cyan]{approval_id}[/cyan])")
        console.print(f"  Reviewed by: [cyan]{reviewed_by}[/cyan] ({reviewer_type})")
        if comments:
            console.print(f"  Comments: [dim]{comments}[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="reject")
@click.argument("approval_id", type=str)
@click.option("--reviewed-by", required=True, help="Reviewer ID")
@click.option(
    "--reviewer-type", default="user", type=click.Choice(["user", "agent"]), help="Reviewer type"
)
@click.option("--comments", help="Optional rejection reason")
@click.option("--tenant-id", help="Tenant ID for scoping")
@add_backend_options
def skills_reject(
    approval_id: str,
    reviewed_by: str,
    reviewer_type: str,
    comments: str | None,
    tenant_id: str | None,
    backend_config: BackendConfig,  # noqa: ARG001
) -> None:
    """Reject a skill for publication.

    Examples:
        nexus skills reject <approval-id> --reviewed-by bob --comments "Security concerns"
        nexus skills reject <approval-id> --reviewed-by manager-id --reviewer-type user --tenant-id acme-corp
    """
    try:
        nx = get_filesystem(backend_config, enforce_permissions=False)

        # Use RPC endpoint directly
        nx.skills_reject(  # type: ignore[attr-defined]
            approval_id=approval_id,
            reviewed_by=reviewed_by,
            reviewer_type=reviewer_type,
            comments=comments,
            tenant_id=tenant_id,
        )

        console.print(f"[red]✗[/red] Rejected skill (Approval ID: [cyan]{approval_id}[/cyan])")
        console.print(f"  Reviewed by: [cyan]{reviewed_by}[/cyan] ({reviewer_type})")
        if comments:
            console.print(f"  Reason: [dim]{comments}[/dim]")

        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="list-approvals")
@click.option(
    "--status", type=click.Choice(["pending", "approved", "rejected"]), help="Filter by status"
)
@click.option("--skill", help="Filter by skill name")
@add_backend_options
def skills_list_approvals(
    status: str | None,
    skill: str | None,
    backend_config: BackendConfig,  # noqa: ARG001
) -> None:
    """List skill approval requests.

    Examples:
        nexus skills list-approvals
        nexus skills list-approvals --status pending
        nexus skills list-approvals --skill my-analyzer
        nexus skills list-approvals --status approved --skill my-skill
    """
    try:
        nx = get_filesystem(backend_config, enforce_permissions=False)

        # Use RPC endpoint directly
        result = nx.skills_list_approvals(status=status, skill_name=skill)  # type: ignore[attr-defined]

        approvals_data = result.get("approvals", [])

        if not approvals_data:
            console.print("[yellow]No approval requests found[/yellow]")
            nx.close()
            return

        # Display approvals in table
        table = Table(title=f"Skill Approvals ({result['count']} found)")
        table.add_column("Approval ID", style="cyan")
        table.add_column("Skill Name", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Submitted By", style="magenta")
        table.add_column("Submitted At", style="dim")

        for approval in approvals_data:
            status_value = approval.get("status", "unknown")
            status_color = {
                "pending": "yellow",
                "approved": "green",
                "rejected": "red",
            }.get(status_value, "white")

            submitted_at_str = approval.get("submitted_at", "N/A")
            if submitted_at_str != "N/A":
                from datetime import datetime

                submitted = datetime.fromisoformat(submitted_at_str)
                submitted_at_str = submitted.strftime("%Y-%m-%d %H:%M")

            approval_id = approval.get("approval_id", "")
            approval_id_display = approval_id[:16] + "..." if len(approval_id) > 16 else approval_id

            table.add_row(
                approval_id_display,
                approval.get("skill_name", "N/A"),
                f"[{status_color}]{status_value}[/{status_color}]",
                approval.get("submitted_by", "N/A"),
                submitted_at_str,
            )

        console.print(table)
        nx.close()

    except Exception as e:
        handle_error(e)


@skills.command(name="diff")
@click.argument("skill1", type=str)
@click.argument("skill2", type=str)
@click.option("--context", "-c", default=3, type=int, help="Context lines (default: 3)")
@add_backend_options
def skills_diff(
    skill1: str,
    skill2: str,
    context: int,
    backend_config: BackendConfig,
) -> None:
    """Show differences between two skills.

    Examples:
        nexus skills diff my-skill-v1 my-skill-v2
        nexus skills diff analyze-code my-analyzer --context 5
    """
    try:
        import asyncio
        import difflib

        from rich.syntax import Syntax

        from nexus.skills import SkillRegistry

        nx = get_filesystem(backend_config, enforce_permissions=False)
        registry = SkillRegistry(nx)

        async def show_diff_async() -> None:
            await registry.discover()

            # Load both skills
            skill_obj1 = await registry.get_skill(skill1)
            skill_obj2 = await registry.get_skill(skill2)

            # Reconstruct SKILL.md content for both
            from nexus.skills.exporter import SkillExporter

            exporter = SkillExporter(registry)

            content1 = exporter._reconstruct_skill_md(skill_obj1)
            content2 = exporter._reconstruct_skill_md(skill_obj2)

            # Generate unified diff
            diff = difflib.unified_diff(
                content1.splitlines(keepends=True),
                content2.splitlines(keepends=True),
                fromfile=f"{skill1}/SKILL.md",
                tofile=f"{skill2}/SKILL.md",
                n=context,
            )

            diff_text = "".join(diff)

            if not diff_text:
                console.print(f"[yellow]No differences between {skill1} and {skill2}[/yellow]")
                return

            # Display diff with syntax highlighting
            console.print(f"\n[bold]Diff: {skill1} vs {skill2}[/bold]\n")

            # Use Syntax for colored diff output
            syntax = Syntax(
                diff_text,
                "diff",
                theme="monokai",
                line_numbers=True,
                word_wrap=False,
            )
            console.print(syntax)

            # Show summary statistics
            lines = diff_text.split("\n")
            additions = sum(
                1 for line in lines if line.startswith("+") and not line.startswith("+++")
            )
            deletions = sum(
                1 for line in lines if line.startswith("-") and not line.startswith("---")
            )

            console.print(
                f"\n[dim]Summary: [green]+{additions}[/green] additions, [red]-{deletions}[/red] deletions[/dim]"
            )

        asyncio.run(show_diff_async())
        nx.close()

    except Exception as e:
        handle_error(e)


# =============================================================================
# MCP TOOLS COMMANDS
# =============================================================================


@skills.group(name="mcp")
def skills_mcp() -> None:
    """MCP Tools - Manage MCP tool skills.

    Export, mount, and discover MCP tools as skills for dynamic tool discovery.

    Examples:
        nexus skills mcp export-tools
        nexus skills mcp list-tools
        nexus skills mcp mount github --command "npx -y @modelcontextprotocol/server-github"
        nexus skills mcp list-mounts
    """
    pass


@skills_mcp.command(name="export-tools")
@click.option("--output", "-o", help="Output directory (default: /skills/system/mcp-tools/nexus/)")
@click.option("--no-sandbox", is_flag=True, help="Exclude sandbox tools")
@add_backend_options
def skills_mcp_export_tools(
    output: str | None,
    no_sandbox: bool,
    backend_config: BackendConfig,
) -> None:
    """Export Nexus MCP tools to Skills format.

    Exports all built-in Nexus MCP tools (file operations, search, memory, etc.)
    to the /skills/system/mcp-tools/ directory as discoverable skills.

    Examples:
        nexus skills mcp export-tools
        nexus skills mcp export-tools --output /custom/path/
        nexus skills mcp export-tools --no-sandbox
    """
    try:
        import asyncio

        from nexus.skills.mcp_exporter import MCPToolExporter

        nx = get_filesystem(backend_config, enforce_permissions=False)
        exporter = MCPToolExporter(nx)

        async def export_async() -> None:
            with console.status("[yellow]Exporting MCP tools...[/yellow]", spinner="dots"):
                count = await exporter.export_nexus_tools(
                    output_path=output,
                    include_sandbox=not no_sandbox,
                )

            console.print(f"[green]✓[/green] Exported [cyan]{count}[/cyan] MCP tools")
            console.print(f"  Output: [dim]{output or exporter.OUTPUT_PATH}[/dim]")

            # Show categories
            categories = exporter.get_tool_categories()
            console.print("\n[bold]Tool Categories:[/bold]")
            for cat, tools in categories.items():
                if no_sandbox and cat == "sandbox":
                    continue
                console.print(f"  [cyan]{cat}[/cyan]: {len(tools)} tools")

        asyncio.run(export_async())
        nx.close()

    except Exception as e:
        handle_error(e)


@skills_mcp.command(name="list-tools")
@click.option("--category", "-c", help="Filter by category (file_operations, search, memory, etc.)")
def skills_mcp_list_tools(
    category: str | None,
) -> None:
    """List available MCP tools.

    Shows all Nexus MCP tools that can be exported as skills.

    Examples:
        nexus skills mcp list-tools
        nexus skills mcp list-tools --category search
    """
    try:
        from nexus.skills.mcp_exporter import NEXUS_TOOLS

        # Filter by category if specified
        tools = NEXUS_TOOLS
        if category:
            tools = [t for t in tools if t.get("category") == category]

        if not tools:
            console.print(f"[yellow]No tools found for category: {category}[/yellow]")
            return

        # Group by category
        categories: dict[str, list[dict]] = {}
        for tool in tools:
            cat = tool.get("category", "other")
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(tool)

        # Display tools
        for cat, cat_tools in sorted(categories.items()):
            console.print(f"\n[bold cyan]{cat.upper()}[/bold cyan]")
            for tool in cat_tools:
                console.print(f"  [green]{tool['name']}[/green]")
                console.print(f"    [dim]{tool['description']}[/dim]")

        console.print(f"\n[dim]Total: {len(tools)} tools[/dim]")

    except Exception as e:
        handle_error(e)


@skills_mcp.command(name="mount")
@click.argument("name", type=str)
@click.option("--description", "-d", help="Mount description")
@click.option("--command", "-c", help="Command to run MCP server (for local/stdio)")
@click.option("--url", "-u", help="URL of remote MCP server (auto-selects SSE transport)")
@click.option(
    "--env",
    "-e",
    multiple=True,
    help="Environment variables (KEY=VALUE). Can be used multiple times.",
)
@click.option(
    "--env-file", type=click.Path(exists=True), help="Load env vars from file (.env format)"
)
@click.option(
    "--header",
    "-H",
    multiple=True,
    help="HTTP headers for remote MCP (KEY: VALUE). Can be used multiple times.",
)
@click.option(
    "--oauth",
    type=str,
    help="Use OAuth credential from Nexus (format: provider:user_email, e.g., google:alice@example.com)",
)
@add_backend_options
def skills_mcp_mount(
    name: str,
    description: str | None,
    command: str | None,
    url: str | None,
    env: tuple[str, ...],
    env_file: str | None,
    header: tuple[str, ...],
    oauth: str | None,
    backend_config: BackendConfig,
) -> None:
    """Mount an MCP server (local or remote).

    Transport is auto-detected:
      - Use --command for local MCP servers (stdio)
      - Use --url for remote MCP servers (SSE)

    Authentication options:
      - Use --env for API tokens/keys
      - Use --env-file to load env vars from file
      - Use --header for HTTP headers (remote MCP only)
      - Use --oauth to use Nexus OAuth credentials (setup via 'nexus oauth')

    Examples:
        # Local filesystem MCP server
        nexus skills mcp mount fs \\
            --command "npx -y @modelcontextprotocol/server-filesystem /tmp"

        # Local Python MCP server
        nexus skills mcp mount my-tools --command "python my_mcp_server.py"

        # GitHub MCP server (with Personal Access Token)
        nexus skills mcp mount github \\
            --command "npx -y @modelcontextprotocol/server-github" \\
            --env GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxxxxxxxxxx

        # Slack MCP server (multiple env vars)
        nexus skills mcp mount slack \\
            --command "npx -y @modelcontextprotocol/server-slack" \\
            --env SLACK_BOT_TOKEN=xoxb-xxxxxxxxxxxx \\
            --env SLACK_TEAM_ID=T01234567

        # Load env vars from file
        nexus skills mcp mount github \\
            --command "npx -y @modelcontextprotocol/server-github" \\
            --env-file ~/.github-mcp.env

        # Use Nexus OAuth credential (will prompt to setup if not found)
        nexus skills mcp mount google-analytics \\
            --command "npx -y @anthropic/mcp-server-google-analytics" \\
            --oauth google:alice@example.com

        # Remote MCP server via URL
        nexus skills mcp mount remote-api --url http://localhost:8080/sse

        # Remote hosted MCP server with auth header (Klavis, etc.)
        nexus skills mcp mount klavis-github \\
            --url "https://strata.klavis.ai/mcp/" \\
            --header "Authorization: Bearer eyJhbGc..."
    """
    try:
        import asyncio

        from nexus.skills.mcp_models import MCPMount
        from nexus.skills.mcp_mount import MCPMountManager

        # Validate: need either command or url
        if not command and not url:
            console.print("[red]Error: Either --command or --url is required[/red]")
            console.print("  Use --command for local MCP servers")
            console.print("  Use --url for remote MCP servers")
            return

        if command and url:
            console.print("[red]Error: Cannot specify both --command and --url[/red]")
            return

        nx = get_filesystem(backend_config, enforce_permissions=False)
        manager = MCPMountManager(nx)

        # Parse environment variables from file first (if provided)
        env_dict: dict[str, str] = {}
        if env_file:
            try:
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        # Skip empty lines and comments
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            key, value = line.split("=", 1)
                            # Remove surrounding quotes if present
                            value = value.strip().strip("'\"")
                            env_dict[key.strip()] = value
                console.print(f"[dim]Loaded {len(env_dict)} env vars from {env_file}[/dim]")
            except Exception as e:
                console.print(f"[red]Error reading env file: {e}[/red]")
                nx.close()
                return

        # Parse command-line environment variables (override file)
        for env_var in env:
            if "=" in env_var:
                key, value = env_var.split("=", 1)
                env_dict[key] = value

        # Parse HTTP headers (for remote MCP servers)
        headers_dict: dict[str, str] = {}
        for h in header:
            if ":" in h:
                key, value = h.split(":", 1)
                headers_dict[key.strip()] = value.strip()
            else:
                console.print(
                    f"[yellow]Warning: Invalid header format '{h}', expected 'Key: Value'[/yellow]"
                )

        # Handle OAuth credential if specified
        oauth_provider = None
        oauth_user = None
        if oauth:
            if ":" not in oauth:
                console.print("[red]Error: --oauth format should be 'provider:user_email'[/red]")
                console.print("  Example: --oauth google:alice@example.com")
                nx.close()
                return

            oauth_provider, oauth_user = oauth.split(":", 1)

            # Get OAuth token from Nexus TokenManager
            try:
                import os

                from nexus.server.auth import TokenManager

                # Get TokenManager (same logic as oauth CLI)
                db_url = os.getenv("NEXUS_DATABASE_URL")
                if db_url:
                    token_manager = TokenManager(db_url=db_url)
                else:
                    home = os.path.expanduser("~")
                    db_path = os.path.join(home, ".nexus", "nexus.db")
                    token_manager = TokenManager(db_path=db_path)

                # Register OAuth provider for automatic token refresh
                if oauth_provider == "google":
                    from nexus.server.auth import GoogleOAuthProvider
                    from nexus.server.auth.oauth_provider import OAuthProvider

                    client_id = os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID")
                    client_secret = os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET")
                    if client_id and client_secret:
                        provider_instance: OAuthProvider = GoogleOAuthProvider(
                            client_id=client_id,
                            client_secret=client_secret,
                            redirect_uri="http://localhost",
                            scopes=[
                                "https://www.googleapis.com/auth/drive",
                                "https://www.googleapis.com/auth/drive.file",
                            ],
                            provider_name="google",
                        )
                        token_manager.register_provider("google", provider_instance)

                # First check if credential exists
                async def check_credential() -> bool:
                    cred = await token_manager.get_credential(oauth_provider, oauth_user, "default")
                    return cred is not None

                credential_exists = asyncio.run(check_credential())

                if not credential_exists:
                    console.print(
                        f"[yellow]No OAuth credential found for {oauth_provider}:{oauth_user}[/yellow]"
                    )
                    console.print()

                    # Ask if user wants to set up OAuth inline
                    if not click.confirm("Would you like to set up OAuth now?", default=True):
                        token_manager.close()
                        nx.close()
                        return

                    # Inline OAuth flow
                    console.print()
                    console.print(f"[bold green]OAuth Setup for {oauth_provider}[/bold green]")

                    # Get OAuth provider credentials from environment
                    if oauth_provider == "google":
                        from nexus.server.auth import GoogleOAuthProvider

                        client_id = os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID")
                        client_secret = os.getenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET")

                        if not client_id or not client_secret:
                            console.print("[red]Error: Google OAuth credentials not found[/red]")
                            console.print("[yellow]Set these environment variables:[/yellow]")
                            console.print("  export NEXUS_OAUTH_GOOGLE_CLIENT_ID='your-client-id'")
                            console.print("  export NEXUS_OAUTH_GOOGLE_CLIENT_SECRET='your-secret'")
                            token_manager.close()
                            nx.close()
                            return

                        provider_instance = GoogleOAuthProvider(
                            client_id=client_id,
                            client_secret=client_secret,
                            redirect_uri="http://localhost",
                            scopes=[
                                "https://www.googleapis.com/auth/drive",
                                "https://www.googleapis.com/auth/drive.file",
                            ],
                            provider_name="google",
                        )
                    elif oauth_provider in ("twitter", "x"):
                        from nexus.server.auth.x_oauth import XOAuthProvider

                        client_id = os.getenv("NEXUS_OAUTH_X_CLIENT_ID")
                        client_secret = os.getenv("NEXUS_OAUTH_X_CLIENT_SECRET")

                        if not client_id or not client_secret:
                            console.print("[red]Error: X/Twitter OAuth credentials not found[/red]")
                            console.print("[yellow]Set these environment variables:[/yellow]")
                            console.print("  export NEXUS_OAUTH_X_CLIENT_ID='your-client-id'")
                            console.print("  export NEXUS_OAUTH_X_CLIENT_SECRET='your-secret'")
                            token_manager.close()
                            nx.close()
                            return

                        provider_instance = XOAuthProvider(
                            client_id=client_id,
                            redirect_uri="http://localhost",
                            scopes=[
                                "tweet.read",
                                "tweet.write",
                                "users.read",
                                "offline.access",
                            ],
                            provider_name="x",
                            client_secret=client_secret,
                        )
                    else:
                        console.print(
                            f"[red]Inline OAuth setup not supported for provider: {oauth_provider}[/red]"
                        )
                        console.print(
                            f"[yellow]Run: nexus oauth init {oauth_provider} ...[/yellow]"
                        )
                        token_manager.close()
                        nx.close()
                        return

                    # Generate auth URL and prompt user
                    auth_url = provider_instance.get_authorization_url()
                    console.print(f"\n[bold]User:[/bold] {oauth_user}")
                    console.print(
                        "\n[bold yellow]Step 1:[/bold yellow] Visit this URL to authorize:"
                    )
                    console.print(f"\n{auth_url}\n")
                    console.print(
                        "[bold yellow]Step 2:[/bold yellow] After granting permission, copy the 'code' from the redirect URL"
                    )
                    console.print("[dim]Example: http://localhost/?code=4/0AdLI...[/dim]")

                    # Get auth code from user
                    auth_code = click.prompt("\nEnter authorization code")

                    # Exchange code for tokens
                    console.print("\n[dim]Exchanging code for tokens...[/dim]")

                    async def _exchange_and_store() -> None:
                        credential = await provider_instance.exchange_code(auth_code)
                        await token_manager.store_credential(
                            provider=oauth_provider if oauth_provider != "x" else "twitter",
                            user_email=oauth_user,
                            credential=credential,
                            tenant_id="default",
                            created_by=oauth_user,
                        )

                    try:
                        asyncio.run(_exchange_and_store())
                        console.print(f"[green]✓[/green] OAuth credentials stored for {oauth_user}")
                    except Exception as e:
                        console.print(f"[red]Error storing credentials: {e}[/red]")
                        token_manager.close()
                        nx.close()
                        return

                async def get_oauth_token() -> str:
                    return await token_manager.get_valid_token(
                        oauth_provider, oauth_user, "default"
                    )

                # Get the token
                access_token = asyncio.run(get_oauth_token())
                token_manager.close()

                # Set appropriate env var based on provider
                # Common patterns for OAuth-based MCP servers
                if oauth_provider == "google":
                    env_dict["GOOGLE_ACCESS_TOKEN"] = access_token
                    env_dict["OAUTH_ACCESS_TOKEN"] = access_token
                elif oauth_provider == "microsoft":
                    env_dict["MICROSOFT_ACCESS_TOKEN"] = access_token
                    env_dict["OAUTH_ACCESS_TOKEN"] = access_token
                else:
                    # Generic OAuth token
                    env_dict["OAUTH_ACCESS_TOKEN"] = access_token
                    env_dict[f"{oauth_provider.upper()}_ACCESS_TOKEN"] = access_token

                console.print(f"[dim]Using OAuth credential: {oauth_provider}:{oauth_user}[/dim]")

            except Exception as e:
                console.print(f"[red]Error getting OAuth token: {e}[/red]")
                console.print("[yellow]Make sure you've set up OAuth via 'nexus oauth'[/yellow]")
                if oauth_provider == "google":
                    console.print(f"  Example: nexus oauth setup-gdrive --user-email {oauth_user}")
                elif oauth_provider in ("twitter", "x"):
                    console.print(f"  Example: nexus oauth setup-x --user-email {oauth_user}")
                nx.close()
                return

        # Auto-detect transport based on command vs url
        if command:
            transport = "stdio"
            # Parse command and args
            parts = command.split()
            cmd = parts[0]
            args = parts[1:] if len(parts) > 1 else []
        else:
            transport = "sse"
            cmd = None
            args = []

        # Create mount configuration
        mount_config = MCPMount(
            name=name,
            description=description or f"MCP server: {name}",
            transport=transport,
            command=cmd,
            url=url,
            args=args,
            env=env_dict,
            headers=headers_dict,
        )

        async def mount_async() -> None:
            with console.status(f"[yellow]Mounting {name}...[/yellow]", spinner="dots"):
                await manager.mount(mount_config)

            console.print(f"[green]✓[/green] Mounted MCP server: [cyan]{name}[/cyan]")
            console.print(f"  Transport: [yellow]{transport}[/yellow]")
            if cmd:
                console.print(f"  Command: [dim]{command}[/dim]")
            if url:
                console.print(f"  URL: [dim]{url}[/dim]")
            if oauth_provider and oauth_user:
                console.print(f"  OAuth: [dim]{oauth_provider}:{oauth_user}[/dim]")
            if env_dict:
                # Show env var names (not values for security)
                env_keys = ", ".join(env_dict.keys())
                console.print(f"  Env vars: [dim]{env_keys}[/dim]")
            if headers_dict:
                # Show header names (not values for security)
                header_keys = ", ".join(headers_dict.keys())
                console.print(f"  Headers: [dim]{header_keys}[/dim]")

        asyncio.run(mount_async())
        nx.close()

    except Exception as e:
        handle_error(e)


@skills_mcp.command(name="unmount")
@click.argument("name", type=str)
@add_backend_options
def skills_mcp_unmount(
    name: str,
    backend_config: BackendConfig,
) -> None:
    """Unmount an MCP server.

    Disconnects from a mounted MCP server.

    Examples:
        nexus skills mcp unmount github
    """
    try:
        import asyncio

        from nexus.skills.mcp_mount import MCPMountManager

        nx = get_filesystem(backend_config, enforce_permissions=False)
        manager = MCPMountManager(nx)

        async def unmount_async() -> None:
            await manager.unmount(name)
            console.print(f"[green]✓[/green] Unmounted MCP server: [cyan]{name}[/cyan]")

        asyncio.run(unmount_async())
        nx.close()

    except Exception as e:
        handle_error(e)


@skills_mcp.command(name="list-mounts")
@click.option("--all", "show_all", is_flag=True, help="Show all mounts including unmounted")
@add_backend_options
def skills_mcp_list_mounts(
    show_all: bool,
    backend_config: BackendConfig,
) -> None:
    """List MCP server mounts.

    Shows all configured MCP server connections.

    Examples:
        nexus skills mcp list-mounts
        nexus skills mcp list-mounts --all
    """
    try:
        from nexus.skills.mcp_mount import MCPMountManager

        nx = get_filesystem(backend_config, enforce_permissions=False)
        manager = MCPMountManager(nx)

        mounts = manager.list_mounts(include_unmounted=show_all)

        if not mounts:
            console.print("[yellow]No MCP mounts configured[/yellow]")
            console.print("[dim]Use 'nexus skills mcp mount <name> --command ...' to add one[/dim]")
            nx.close()
            return

        # Display mounts in table
        table = Table(title="MCP Server Mounts")
        table.add_column("Name", style="cyan")
        table.add_column("Status", style="green")
        table.add_column("Transport", style="yellow")
        table.add_column("Tools", justify="right")
        table.add_column("Last Sync", style="dim")

        for mount in mounts:
            status = "[green]mounted[/green]" if mount.mounted else "[red]not mounted[/red]"
            last_sync = mount.last_sync.strftime("%Y-%m-%d %H:%M") if mount.last_sync else "Never"

            table.add_row(
                mount.name,
                status,
                mount.transport,
                str(mount.tool_count),
                last_sync,
            )

        console.print(table)
        nx.close()

    except Exception as e:
        handle_error(e)


@skills_mcp.command(name="tools")
@click.argument("name", type=str)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@add_backend_options
def skills_mcp_tools(
    name: str,
    json_output: bool,
    backend_config: BackendConfig,
) -> None:
    """List tools from a specific MCP mount.

    Shows all tools discovered from a mounted MCP server.

    Examples:
        nexus skills mcp tools github
        nexus skills mcp tools github --json
    """
    try:
        from nexus.skills.mcp_mount import MCPMountManager

        nx = get_filesystem(backend_config, enforce_permissions=False)
        manager = MCPMountManager(nx)

        mount = manager.get_mount(name)
        if not mount:
            console.print(f"[red]Mount not found: {name}[/red]")
            console.print(
                "[dim]Use 'nexus skills mcp list-mounts --all' to see available mounts[/dim]"
            )
            nx.close()
            return

        # Get tools from mount config or read from filesystem
        tools = mount.tools or []

        # If no tools in config, try to read from filesystem
        if not tools and mount.tools_path:
            try:
                items = nx.list(mount.tools_path, recursive=False)
                # Filter for .json files (tool definitions)
                tools = [
                    str(item).split("/")[-1].replace(".json", "")
                    for item in items
                    if isinstance(item, str)
                    and item.endswith(".json")
                    and not item.endswith(".mounts.json")
                ]
            except Exception:
                pass

        if not tools:
            console.print(f"[yellow]No tools found for mount: {name}[/yellow]")
            console.print("[dim]The mount may not have been synced yet[/dim]")
            nx.close()
            return

        if json_output:
            # JSON output
            import json as json_module

            tool_data = []
            for tool_name in sorted(tools):
                # New flat structure: /skills/system/mcp-tools/github/search_repositories.json
                tool_path = f"{mount.tools_path}{tool_name}.json"
                try:
                    content = nx.read(tool_path)
                    if isinstance(content, bytes):
                        content_str = content.decode()
                        tool_info = json_module.loads(content_str)
                    elif isinstance(content, dict):
                        tool_info = content
                    else:
                        tool_info = {"name": tool_name}
                    tool_data.append(tool_info)
                except Exception:
                    tool_data.append({"name": tool_name})
            print(json_module.dumps(tool_data, indent=2))
        else:
            # Table output
            table = Table(title=f"Tools from '{name}' ({len(tools)} tools)")
            table.add_column("Tool Name", style="cyan")
            table.add_column("Description", style="green")
            table.add_column("Endpoint", style="dim")

            for tool_name in sorted(tools):
                # Try to read tool.json for description
                description = ""
                endpoint = f"mcp://{name}/{tool_name}"
                # New flat structure: /skills/system/mcp-tools/github/search_repositories.json
                tool_path = f"{mount.tools_path}{tool_name}.json"
                try:
                    content = nx.read(tool_path)
                    import json as json_module

                    if isinstance(content, bytes):
                        content_str = content.decode()
                        tool_info = json_module.loads(content_str)
                    elif isinstance(content, dict):
                        tool_info = content
                    else:
                        tool_info = {}
                    description = tool_info.get("description", "")
                    if tool_info.get("mcp_config", {}).get("endpoint"):
                        endpoint = tool_info["mcp_config"]["endpoint"]
                except Exception:
                    pass

                # Truncate long descriptions
                if len(description) > 50:
                    description = description[:47] + "..."

                table.add_row(tool_name, description, endpoint)

            console.print(table)

        nx.close()

    except Exception as e:
        handle_error(e)


@skills_mcp.command(name="remove")
@click.argument("name", type=str)
@click.option("--force", "-f", is_flag=True, help="Force remove even if mounted")
@add_backend_options
def skills_mcp_remove(
    name: str,
    force: bool,
    backend_config: BackendConfig,
) -> None:
    """Remove an MCP mount configuration.

    Removes the mount configuration and optionally its discovered tools.

    Examples:
        nexus skills mcp remove github
        nexus skills mcp remove github --force
    """
    try:
        import asyncio

        from nexus.skills.mcp_mount import MCPMountManager

        nx = get_filesystem(backend_config, enforce_permissions=False)
        manager = MCPMountManager(nx)

        mount = manager.get_mount(name)
        if not mount:
            console.print(f"[red]Mount not found: {name}[/red]")
            nx.close()
            return

        async def remove_async() -> None:
            # Unmount if needed
            if mount.mounted:
                if not force:
                    console.print(f"[red]Mount {name} is active. Use --force to remove.[/red]")
                    return
                await manager.unmount(name)

            manager.remove_mount(name)
            console.print(f"[green]✓[/green] Removed mount configuration: [cyan]{name}[/cyan]")

        asyncio.run(remove_async())
        nx.close()

    except Exception as e:
        handle_error(e)


# =============================================================================
# UNIFIED MCP CONNECTION COMMANDS
# =============================================================================


@skills_mcp.command(name="connect")
@click.argument("provider", type=str)
@click.option("--user", "-u", required=True, help="User ID for this connection")
@click.option(
    "--scopes", "-s", multiple=True, help="OAuth scopes (optional, uses provider defaults)"
)
@click.option("--port", default=3000, type=int, help="Local callback port for OAuth")
@click.option("--no-browser", is_flag=True, help="Don't open browser (print URL instead)")
@add_backend_options
def skills_mcp_connect(
    provider: str,
    user: str,
    scopes: tuple[str, ...],
    port: int,
    no_browser: bool,
    backend_config: BackendConfig,
) -> None:
    """Connect to an MCP provider (unified Klavis + local).

    This command works with both Klavis-hosted and local providers transparently.
    The same command works for GitHub (Klavis), Google Drive (local), etc.

    Examples:
        # Connect to GitHub via Klavis
        nexus skills mcp connect github --user alice

        # Connect to Google Drive via local OAuth
        nexus skills mcp connect gdrive --user alice@gmail.com

        # Connect with custom scopes
        nexus skills mcp connect github --user alice --scopes repo --scopes read:user

        # Don't open browser (manual OAuth)
        nexus skills mcp connect github --user alice --no-browser
    """
    try:
        import asyncio

        from nexus.mcp import MCPConnectionManager

        nx = get_filesystem(backend_config, enforce_permissions=False)
        manager = MCPConnectionManager(filesystem=nx)

        async def connect_async() -> None:
            console.print(
                f"Connecting to [cyan]{provider}[/cyan] for user [green]{user}[/green]..."
            )

            connection = await manager.connect(
                provider=provider,
                user_id=user,
                scopes=list(scopes) if scopes else None,
                callback_port=port,
                open_browser=not no_browser,
            )

            console.print(f"\n[green]✓[/green] Connected to {provider}")
            console.print(f"  User: {connection.user_id}")
            console.print(f"  Type: {connection.provider_type.value}")

            if connection.mcp_url:
                console.print(f"  MCP URL: {connection.mcp_url}")

            console.print(f"\n[dim]Tools available at: /skills/system/mcp-tools/{provider}/[/dim]")

        asyncio.run(connect_async())
        nx.close()

    except Exception as e:
        handle_error(e)


@skills_mcp.command(name="disconnect")
@click.argument("provider", type=str)
@click.option("--user", "-u", required=True, help="User ID")
@add_backend_options
def skills_mcp_disconnect(
    provider: str,
    user: str,
    backend_config: BackendConfig,
) -> None:
    """Disconnect from an MCP provider.

    Examples:
        nexus skills mcp disconnect github --user alice
        nexus skills mcp disconnect gdrive --user alice@gmail.com
    """
    try:
        import asyncio

        from nexus.mcp import MCPConnectionManager

        nx = get_filesystem(backend_config, enforce_permissions=False)
        manager = MCPConnectionManager(filesystem=nx)

        async def disconnect_async() -> None:
            success = await manager.disconnect(provider, user)
            if success:
                console.print(f"[green]✓[/green] Disconnected from {provider}")
            else:
                console.print(f"[yellow]Connection not found: {provider}:{user}[/yellow]")

        asyncio.run(disconnect_async())
        nx.close()

    except Exception as e:
        handle_error(e)


@skills_mcp.command(name="connections")
@click.option("--user", "-u", help="Filter by user")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@add_backend_options
def skills_mcp_connections(
    user: str | None,
    output_json: bool,
    backend_config: BackendConfig,
) -> None:
    """List all MCP connections.

    Examples:
        nexus skills mcp connections
        nexus skills mcp connections --user alice
        nexus skills mcp connections --json
    """
    try:
        import json as json_module

        from nexus.mcp import MCPConnectionManager

        nx = get_filesystem(backend_config, enforce_permissions=False)
        manager = MCPConnectionManager(filesystem=nx)

        connections = manager.list_connections(user_id=user)

        if not connections:
            console.print("[yellow]No connections found[/yellow]")
            nx.close()
            return

        if output_json:
            data = [c.to_dict() for c in connections]
            print(json_module.dumps(data, indent=2))
        else:
            table = Table(title="MCP Connections")
            table.add_column("Provider", style="cyan")
            table.add_column("User", style="green")
            table.add_column("Type", style="blue")
            table.add_column("Connected At", style="dim")

            for conn in connections:
                table.add_row(
                    conn.provider,
                    conn.user_id,
                    conn.provider_type.value,
                    conn.connected_at.strftime("%Y-%m-%d %H:%M"),
                )

            console.print(table)

        nx.close()

    except Exception as e:
        handle_error(e)


@skills_mcp.command(name="providers")
@click.option(
    "--type", "provider_type", type=click.Choice(["all", "klavis", "local"]), default="all"
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def skills_mcp_providers(
    provider_type: str,
    output_json: bool,
) -> None:
    """List available MCP providers.

    Shows all providers that can be connected via 'nexus skills mcp connect'.

    Examples:
        nexus skills mcp providers
        nexus skills mcp providers --type klavis
        nexus skills mcp providers --type local
        nexus skills mcp providers --json
    """
    try:
        import json as json_module

        from nexus.mcp import MCPProviderRegistry

        registry = MCPProviderRegistry.load_default()

        if provider_type == "klavis":
            providers = registry.list_klavis_providers()
        elif provider_type == "local":
            providers = registry.list_local_providers()
        else:
            providers = registry.list_providers()

        if not providers:
            console.print("[yellow]No providers found[/yellow]")
            return

        if output_json:
            data = {name: config.to_dict() for name, config in providers}
            print(json_module.dumps(data, indent=2))
        else:
            table = Table(title="Available MCP Providers")
            table.add_column("Name", style="cyan")
            table.add_column("Type", style="blue")
            table.add_column("Display Name", style="green")
            table.add_column("Description", style="dim")

            for name, config in providers:
                table.add_row(
                    name,
                    config.type.value,
                    config.display_name,
                    config.description[:50] + "..."
                    if len(config.description) > 50
                    else config.description,
                )

            console.print(table)
            console.print(
                "\n[dim]Use 'nexus skills mcp connect <provider> --user <user>' to connect[/dim]"
            )

    except Exception as e:
        handle_error(e)
