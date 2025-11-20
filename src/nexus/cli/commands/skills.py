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
@click.option("--tenant", is_flag=True, help="Show tenant-wide skills")
@click.option("--system", is_flag=True, help="Show system skills")
@click.option("--tier", type=click.Choice(["agent", "tenant", "system"]), help="Filter by tier")
@add_backend_options
def skills_list(
    tenant: bool,
    system: bool,
    tier: str | None,
    backend_config: BackendConfig,
) -> None:
    """List all skills.

    Examples:
        nexus skills list
        nexus skills list --tenant
        nexus skills list --system
        nexus skills list --tier agent
    """
    try:
        nx = get_filesystem(backend_config, enforce_permissions=False)

        # Determine tier filter
        if tier:
            tier_filter = tier
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
    "--tier", type=click.Choice(["agent", "tenant", "system"]), default="agent", help="Target tier"
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
    "--tier", type=click.Choice(["agent", "tenant", "system"]), default="agent", help="Target tier"
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
    "--tier", type=click.Choice(["agent", "tenant", "system"]), default="agent", help="Target tier"
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
@click.option("--tier", type=click.Choice(["agent", "tenant", "system"]), help="Filter by tier")
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
                    format=format,
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
                format=format,
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
                format="generic",
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
