"""Skill Seekers plugin for generating skills from documentation."""

import re
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import track

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


class SkillSeekersPlugin(NexusPlugin):
    """Plugin for generating skills from documentation.

    Provides commands for scraping documentation and generating SKILL.md files.
    """

    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="skill-seekers",
            version="0.1.0",
            description="Generate skills from documentation using AI",
            author="Nexus Team",
            homepage="https://github.com/nexi-lab/nexus-plugin-skill-seekers",
            requires=[],
        )

    def commands(self) -> dict[str, Callable]:
        """Return plugin commands."""
        return {
            "generate": self.generate_skill,
            "import": self.import_skill,
            "batch": self.batch_generate,
            "list": self.list_skills,
        }

    async def generate_skill(
        self,
        url: str,
        name: Optional[str] = None,
        tier: str = "agent",
        output_dir: Optional[str] = None,
    ) -> None:
        """Generate a skill from documentation URL.

        Args:
            url: Documentation URL to scrape
            name: Name for the skill (auto-generated if not provided)
            tier: Target tier (agent, tenant, system). Default: agent
            output_dir: Output directory for generated SKILL.md (optional)
        """
        try:
            console.print(f"[cyan]Scraping documentation from:[/cyan] {url}")

            # Fetch and parse documentation
            content = self._fetch_documentation(url)
            if not content:
                console.print("[red]Failed to fetch documentation[/red]")
                return

            # Auto-generate name if not provided
            if not name:
                name = self._generate_skill_name(url)

            console.print(f"[cyan]Generating skill:[/cyan] {name}")

            # Generate SKILL.md content
            skill_content = self._generate_skill_md(name, url, content)

            # Determine output location
            if output_dir:
                # Save to file
                output_path = Path(output_dir) / f"{name}.md"
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "w") as f:
                    f.write(skill_content)
                console.print(f"[green]✓ Saved to:[/green] {output_path}")

            # Import to Nexus if available
            if self.nx:
                tier_paths = {
                    "agent": "/workspace/.nexus/skills/",
                    "tenant": "/shared/skills/",
                    "system": "/system/skills/",
                }
                skill_path = f"{tier_paths[tier]}{name}.md"
                self.nx.write(skill_path, skill_content.encode("utf-8"))
                console.print(f"[green]✓ Imported to Nexus:[/green] {skill_path}")
            else:
                console.print("[yellow]Note: NexusFS not available, saved to file only[/yellow]")

        except Exception as e:
            console.print(f"[red]Failed to generate skill: {e}[/red]")
            import traceback

            traceback.print_exc()

    async def import_skill(
        self, file_path: str, tier: str = "agent", name: Optional[str] = None
    ) -> None:
        """Import a SKILL.md file into Nexus.

        Args:
            file_path: Path to SKILL.md file
            tier: Target tier (agent, tenant, system). Default: agent
            name: Override skill name (uses filename if not provided)
        """
        if not self.nx:
            console.print("[red]Error: NexusFS not available[/red]")
            return

        try:
            # Read skill file
            with open(file_path, "r") as f:
                content = f.read()

            # Determine skill name
            if not name:
                name = Path(file_path).stem

            # Import to Nexus
            tier_paths = {
                "agent": "/workspace/.nexus/skills/",
                "tenant": "/shared/skills/",
                "system": "/system/skills/",
            }
            skill_path = f"{tier_paths[tier]}{name}.md"
            self.nx.write(skill_path, content.encode("utf-8"))

            console.print(f"[green]✓ Imported '{name}' to {skill_path}[/green]")

        except FileNotFoundError:
            console.print(f"[red]File not found: {file_path}[/red]")
        except Exception as e:
            console.print(f"[red]Failed to import skill: {e}[/red]")

    async def batch_generate(self, urls_file: str, tier: str = "agent") -> None:
        """Generate multiple skills from a URLs file.

        Args:
            urls_file: Path to file containing URLs (one per line: url name)
            tier: Target tier for all skills. Default: agent
        """
        try:
            with open(urls_file, "r") as f:
                lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]

            console.print(f"[cyan]Processing {len(lines)} URLs...[/cyan]")

            for line in track(lines, description="Generating skills..."):
                parts = line.split(maxsplit=1)
                url = parts[0]
                name = parts[1] if len(parts) > 1 else None

                await self.generate_skill(url, name=name, tier=tier)

            console.print(f"[green]✓ Generated {len(lines)} skills[/green]")

        except FileNotFoundError:
            console.print(f"[red]File not found: {urls_file}[/red]")
        except Exception as e:
            console.print(f"[red]Batch generation failed: {e}[/red]")

    async def list_skills(self) -> None:
        """List all generated skills."""
        if not self.nx:
            console.print("[yellow]NexusFS not available[/yellow]")
            return

        try:
            from rich.table import Table

            # List skills from all tiers
            tiers = {
                "Agent": "/workspace/.nexus/skills/",
                "Tenant": "/shared/skills/",
                "System": "/system/skills/",
            }

            table = Table(title="Nexus Skills")
            table.add_column("Tier", style="cyan")
            table.add_column("Name", style="green")
            table.add_column("Path")

            for tier_name, tier_path in tiers.items():
                try:
                    files = self.nx.list(tier_path)
                    for file in files:
                        if isinstance(file, str) and file.endswith(".md"):
                            name = file.replace(".md", "")
                            table.add_row(tier_name, name, f"{tier_path}{file}")
                except Exception:
                    # Tier directory might not exist
                    pass

            console.print(table)

        except Exception as e:
            console.print(f"[red]Failed to list skills: {e}[/red]")

    def _fetch_documentation(self, url: str) -> str:
        """Fetch and extract text content from URL.

        Args:
            url: Documentation URL

        Returns:
            Extracted text content
        """
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            # Parse HTML
            soup = BeautifulSoup(response.text, "html.parser")

            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()

            # Get text
            text = soup.get_text()

            # Clean up whitespace
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            cleaned_text = "\n".join(chunk for chunk in chunks if chunk)

            return cleaned_text

        except Exception as e:
            console.print(f"[red]Error fetching URL: {e}[/red]")
            return ""

    def _generate_skill_name(self, url: str) -> str:
        """Generate a skill name from URL.

        Args:
            url: Documentation URL

        Returns:
            Generated skill name
        """
        parsed = urlparse(url)

        # Use last path component or domain
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts:
            name = path_parts[-1]
        else:
            name = parsed.netloc.replace(".", "-")

        # Clean up name
        name = re.sub(r"[^a-zA-Z0-9_-]", "-", name)
        name = re.sub(r"-+", "-", name).strip("-")

        return name or "skill"

    def _generate_skill_md(self, name: str, url: str, content: str) -> str:
        """Generate SKILL.md content from documentation.

        Args:
            name: Skill name
            url: Source URL
            content: Documentation content

        Returns:
            Generated SKILL.md content
        """
        # Truncate content for summary (use first 2000 chars)
        summary_content = content[:2000] + "..." if len(content) > 2000 else content

        # Create basic SKILL.md structure
        # In a full implementation, you'd use an LLM here
        skill_md = f"""---
name: {name}
version: 1.0.0
description: Skill generated from {url}
author: Skill Seekers
created: {self._get_timestamp()}
source_url: {url}
tier: agent
---

# {name.replace("-", " ").title()}

## Overview

This skill was automatically generated from documentation at {url}.

## Description

{summary_content}

## Source

Documentation scraped from: {url}

## Usage

This skill can be used to understand and work with concepts from the source documentation.

## Keywords

{", ".join(self._extract_keywords(content))}

---

*Generated by Nexus Skill Seekers Plugin*
"""
        return skill_md

    def _get_timestamp(self) -> str:
        """Get current timestamp in ISO format."""
        from datetime import datetime

        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _extract_keywords(self, content: str, max_keywords: int = 10) -> list[str]:
        """Extract keywords from content.

        Args:
            content: Documentation content
            max_keywords: Maximum number of keywords

        Returns:
            List of keywords
        """
        # Simple keyword extraction: find common technical words
        # In a full implementation, use NLP techniques
        words = re.findall(r"\b[a-z]{4,}\b", content.lower())

        # Common programming/tech terms
        tech_terms = [
            "api",
            "data",
            "function",
            "class",
            "method",
            "object",
            "request",
            "response",
            "server",
            "client",
            "database",
            "query",
            "schema",
            "model",
            "service",
            "interface",
        ]

        # Filter for tech terms and get most common
        keywords = [word for word in words if word in tech_terms]

        # Return unique keywords
        seen = set()
        unique_keywords = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                unique_keywords.append(kw)
                if len(unique_keywords) >= max_keywords:
                    break

        return unique_keywords or ["documentation", "reference"]
