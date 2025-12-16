"""Anthropic plugin for uploading/downloading skills to/from Claude Skills API."""

import os
from pathlib import Path
from typing import Callable, Optional

import anthropic
import requests
from nexus.plugins import NexusPlugin, PluginMetadata
from rich.console import Console
from rich.table import Table

console = Console()


class AnthropicPlugin(NexusPlugin):
    """Plugin for Anthropic Claude Skills API integration.

    Provides commands for uploading and downloading skills to/from Claude Skills API.
    Uses the official /beta/skills endpoints announced in October 2025.
    """

    # API version headers
    SKILLS_BETA_VERSION = "skills-2025-10-02"
    CODE_EXECUTION_BETA_VERSION = "code-execution-2025-08-25"

    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="anthropic",
            version="0.2.0",
            description="Anthropic Claude Skills API integration for skills management",
            author="Nexus Team",
            homepage="https://github.com/nexi-lab/nexus-plugin-anthropic",
            requires=[],
        )

    def commands(self) -> dict[str, Callable]:
        """Return plugin commands."""
        return {
            "upload-skill": self.upload_skill,
            "download-skill": self.download_skill,
            "list-skills": self.list_skills,
            "delete-skill": self.delete_skill,
            "browse-github": self.browse_github_skills,
            "import-github": self.import_github_skill,
        }

    def _filter_skill_md_for_claude(self, skill_content: str) -> str:
        """Filter SKILL.md frontmatter to only include Claude API allowed fields.

        Claude API only allows: name, description, license, allowed-tools, metadata

        Args:
            skill_content: Original SKILL.md content

        Returns:
            Filtered SKILL.md content
        """
        import yaml

        try:
            # Split frontmatter and content
            parts = skill_content.split("---")
            if len(parts) < 3:
                return skill_content

            # Parse frontmatter
            frontmatter = yaml.safe_load(parts[1])

            # Filter to allowed fields
            allowed_fields = {"name", "description", "license", "allowed-tools", "metadata"}
            filtered_frontmatter = {k: v for k, v in frontmatter.items() if k in allowed_fields}

            # Ensure required fields exist
            if "name" not in filtered_frontmatter and "name" in frontmatter:
                filtered_frontmatter["name"] = frontmatter["name"]
            if "description" not in filtered_frontmatter and "description" in frontmatter:
                filtered_frontmatter["description"] = frontmatter["description"]

            # Reconstruct SKILL.md
            filtered_yaml = yaml.dump(
                filtered_frontmatter, default_flow_style=False, sort_keys=False
            )
            reconstructed = f"---\n{filtered_yaml}---\n"

            # Add content (everything after second ---)
            if len(parts) > 2:
                reconstructed += "---".join(parts[2:])

            return reconstructed

        except Exception as e:
            console.print(f"[yellow]Warning: Could not filter SKILL.md frontmatter: {e}[/yellow]")
            return skill_content

    def _get_client(self, api_key: Optional[str] = None) -> anthropic.Anthropic:
        """Get Anthropic client with API key.

        Args:
            api_key: Optional API key override

        Returns:
            Configured Anthropic client
        """
        api_key = api_key or self.get_config("api_key") or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("Anthropic API key not found")

        return anthropic.Anthropic(api_key=api_key)

    async def upload_skill(
        self,
        skill_name: str,
        api_key: Optional[str] = None,
        format: str = "claude",
        display_title: Optional[str] = None,
    ) -> None:
        """Upload a skill to Claude Skills API.

        Args:
            skill_name: Name of the skill to upload
            api_key: Anthropic API key (optional, uses config or env)
            format: Export format (default: claude)
            display_title: Display title for the skill (defaults to skill_name)
        """
        if not self.nx:
            console.print("[red]Error: NexusFS not available[/red]")
            return

        try:
            client = self._get_client(api_key)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            console.print("Set API key in config or ANTHROPIC_API_KEY environment variable")
            return

        try:
            from nexus.skills import SkillExporter, SkillRegistry

            # Export skill using core Nexus
            registry = SkillRegistry(self.nx)
            await registry.discover()

            exporter = SkillExporter(registry)

            # Create temporary export file
            export_path = Path(f"/tmp/{skill_name}.zip")

            console.print(f"Exporting skill '{skill_name}'...")
            await exporter.export_skill(
                skill_name, str(export_path), format=format, include_dependencies=True
            )

            # Upload to Claude Skills API
            console.print("Uploading to Claude Skills API...")

            # Read and restructure the zip file for Claude API requirements
            # Claude API expects: skill-name/SKILL.md and skill-name/manifest.json
            import io
            import zipfile

            # Read the exported zip
            with zipfile.ZipFile(export_path, "r") as src_zip:
                # Create a new zip with the correct structure
                new_zip_buffer = io.BytesIO()
                with zipfile.ZipFile(new_zip_buffer, "w", zipfile.ZIP_DEFLATED) as dst_zip:
                    # Ensure all files are inside a top-level folder
                    for file_info in src_zip.filelist:
                        content = src_zip.read(file_info.filename)
                        filename = file_info.filename

                        # Filter SKILL.md frontmatter for Claude API compatibility
                        if filename.endswith("/SKILL.md") or filename == "SKILL.md":
                            content = self._filter_skill_md_for_claude(
                                content.decode("utf-8")
                            ).encode("utf-8")

                        # If manifest.json is at root, move it inside the skill folder
                        if filename == "manifest.json":
                            filename = f"{skill_name}/{filename}"

                        # Write to new zip
                        if filename and not filename.endswith("/"):
                            dst_zip.writestr(filename, content)

                zip_content = new_zip_buffer.getvalue()

            # Upload to Skills API
            response = client.beta.skills.create(
                display_title=display_title or skill_name,
                files=[("skill.zip", zip_content)],
                extra_headers={
                    "anthropic-beta": f"{self.SKILLS_BETA_VERSION},{self.CODE_EXECUTION_BETA_VERSION}"
                },
            )

            console.print(f"[green]✓ Successfully uploaded skill '{skill_name}'[/green]")
            console.print(f"Skill ID: {response.id}")
            console.print(f"Version: {response.latest_version}")

            # Cleanup
            export_path.unlink()

        except Exception as e:
            console.print(f"[red]Failed to upload skill: {e}[/red]")
            import traceback

            traceback.print_exc()

    async def download_skill(
        self,
        skill_id: str,
        api_key: Optional[str] = None,
        tier: str = "agent",
        version: str = "latest",
    ) -> None:
        """Download a skill from Claude Skills API.

        Args:
            skill_id: Anthropic skill ID (e.g., skill_01AbCdEfGhIjKlMnOpQrStUv)
            api_key: Anthropic API key (optional, uses config or env)
            tier: Target tier (agent, tenant, system)
            version: Skill version (default: latest)
        """
        if not self.nx:
            console.print("[red]Error: NexusFS not available[/red]")
            return

        try:
            client = self._get_client(api_key)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return

        try:
            console.print(f"Downloading skill {skill_id} (version: {version})...")

            # Get skill details
            skill_info = client.beta.skills.retrieve(
                skill_id=skill_id,
                extra_headers={
                    "anthropic-beta": f"{self.SKILLS_BETA_VERSION},{self.CODE_EXECUTION_BETA_VERSION}"
                },
            )

            console.print(f"Found: {skill_info.display_title}")

            # Display skill information
            console.print(
                "[yellow]Note: Skill download implementation pending API documentation[/yellow]"
            )
            console.print(f"Skill ID: {skill_id}")
            console.print(f"Display Title: {skill_info.display_title}")
            console.print(f"Latest Version: {skill_info.latest_version}")

        except Exception as e:
            console.print(f"[red]Failed to download skill: {e}[/red]")
            import traceback

            traceback.print_exc()

    async def list_skills(
        self, api_key: Optional[str] = None, source: Optional[str] = None
    ) -> None:
        """List skills uploaded to Claude Skills API.

        Args:
            api_key: Anthropic API key (optional, uses config or env)
            source: Filter by source ("custom" or "anthropic")
        """
        try:
            client = self._get_client(api_key)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return

        try:
            console.print("Fetching skills from Claude Skills API...")

            # GET /beta/skills
            if source:
                skills = client.beta.skills.list(
                    extra_headers={
                        "anthropic-beta": f"{self.SKILLS_BETA_VERSION},{self.CODE_EXECUTION_BETA_VERSION}"
                    },
                    source=source,
                )
            else:
                skills = client.beta.skills.list(
                    extra_headers={
                        "anthropic-beta": f"{self.SKILLS_BETA_VERSION},{self.CODE_EXECUTION_BETA_VERSION}"
                    },
                )

            if not skills.data:
                console.print("[yellow]No skills found in Claude Skills API[/yellow]")
                return

            table = Table(title="Claude Skills")
            table.add_column("ID", style="cyan")
            table.add_column("Display Title", style="green")
            table.add_column("Latest Version")
            table.add_column("Source", style="yellow")
            table.add_column("Created At")

            for skill in skills.data:
                created = getattr(skill, "created_at", "N/A")
                if hasattr(created, "strftime"):
                    created = created.strftime("%Y-%m-%d %H:%M:%S")

                source_type = getattr(skill, "source", "custom")

                table.add_row(
                    skill.id,
                    skill.display_title,
                    str(skill.latest_version),
                    source_type,
                    str(created),
                )

            console.print(table)

        except Exception as e:
            console.print(f"[red]Failed to list skills: {e}[/red]")
            import traceback

            traceback.print_exc()

    async def delete_skill(
        self, skill_id: str, api_key: Optional[str] = None, confirm: bool = False
    ) -> None:
        """Delete a skill from Claude Skills API.

        Args:
            skill_id: Anthropic skill ID to delete
            api_key: Anthropic API key (optional, uses config or env)
            confirm: Skip confirmation prompt
        """
        try:
            client = self._get_client(api_key)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return

        try:
            if not confirm:
                from rich.prompt import Confirm

                if not Confirm.ask(f"Delete skill {skill_id}?"):
                    console.print("Cancelled")
                    return

            console.print(f"Deleting skill {skill_id}...")

            # Delete all versions before deleting the skill
            console.print("Deleting all versions first...")

            versions = client.beta.skills.versions.list(
                skill_id=skill_id,
                extra_headers={
                    "anthropic-beta": f"{self.SKILLS_BETA_VERSION},{self.CODE_EXECUTION_BETA_VERSION}"
                },
            )

            for version in versions.data:
                client.beta.skills.versions.delete(
                    skill_id=skill_id,
                    version=version.version,
                    extra_headers={
                        "anthropic-beta": f"{self.SKILLS_BETA_VERSION},{self.CODE_EXECUTION_BETA_VERSION}"
                    },
                )
                console.print(f"  Deleted version: {version.version}")

            # Now delete the skill
            client.beta.skills.delete(
                skill_id=skill_id,
                extra_headers={
                    "anthropic-beta": f"{self.SKILLS_BETA_VERSION},{self.CODE_EXECUTION_BETA_VERSION}"
                },
            )

            console.print(f"[green]✓ Deleted skill {skill_id}[/green]")

        except Exception as e:
            console.print(f"[red]Failed to delete skill: {e}[/red]")
            import traceback

            traceback.print_exc()

    async def browse_github_skills(self, category: Optional[str] = None) -> None:
        """Browse skills from the Anthropic skills GitHub repository.

        Args:
            category: Filter by category (e.g., creative, development, enterprise)
        """
        try:
            console.print("Fetching skills from GitHub (anthropics/skills)...")

            # Fetch the repository structure
            api_url = "https://api.github.com/repos/anthropics/skills/contents"

            response = requests.get(api_url, timeout=10)
            response.raise_for_status()

            items = response.json()

            # Filter for directories (skills)
            skills = [
                item for item in items if item["type"] == "dir" and not item["name"].startswith(".")
            ]

            if not skills:
                console.print("[yellow]No skills found in repository[/yellow]")
                return

            # Filter by category if specified
            if category:
                skills = [s for s in skills if category.lower() in s["name"].lower()]

            table = Table(title="GitHub Skills (anthropics/skills)")
            table.add_column("Name", style="cyan")
            table.add_column("Path", style="dim")

            for skill in skills:
                table.add_row(skill["name"], skill["path"])

            console.print(table)
            console.print(f"\n[dim]Total: {len(skills)} skills[/dim]")
            console.print(
                "\nUse [cyan]nexus anthropic import-github <skill-name>[/cyan] to import a skill"
            )

        except Exception as e:
            console.print(f"[red]Failed to browse GitHub skills: {e}[/red]")
            import traceback

            traceback.print_exc()

    async def import_github_skill(self, skill_name: str, tier: str = "agent") -> None:
        """Import a skill from the Anthropic skills GitHub repository.

        Args:
            skill_name: Name of the skill directory in the GitHub repo
            tier: Target tier (agent, tenant, system)
        """
        if not self.nx:
            console.print("[red]Error: NexusFS not available[/red]")
            return

        try:
            console.print(f"Importing skill '{skill_name}' from GitHub...")

            # Fetch SKILL.md from GitHub
            raw_url = (
                f"https://raw.githubusercontent.com/anthropics/skills/main/{skill_name}/SKILL.md"
            )

            response = requests.get(raw_url, timeout=10)
            response.raise_for_status()

            skill_content = response.text

            # Determine tier path
            tier_paths = {
                "agent": "/workspace/.nexus/skills/",
                "tenant": "/shared/skills/",
                "system": "/system/skills/",
            }

            # Write to Nexus
            skill_path = f"{tier_paths[tier]}{skill_name}/SKILL.md"

            # Create directory structure
            dir_path = f"{tier_paths[tier]}{skill_name}"
            self.nx.mkdir(dir_path, parents=True, exist_ok=True)

            self.nx.write(skill_path, skill_content.encode("utf-8"))

            console.print(f"[green]✓ Imported '{skill_name}' to {skill_path}[/green]")
            console.print(f"Source: https://github.com/anthropics/skills/tree/main/{skill_name}")

            # Show skill info
            import yaml

            try:
                parts = skill_content.split("---")
                if len(parts) >= 2:
                    metadata = yaml.safe_load(parts[1])
                    console.print("\n[bold]Skill Info:[/bold]")
                    console.print(f"  Name: {metadata.get('name', 'N/A')}")
                    console.print(f"  Description: {metadata.get('description', 'N/A')}")
            except Exception:
                pass

        except requests.HTTPError as e:
            if e.response.status_code == 404:
                console.print(f"[red]Skill '{skill_name}' not found in GitHub repository[/red]")
                console.print(
                    "Use [cyan]nexus anthropic browse-github[/cyan] to see available skills"
                )
            else:
                console.print(f"[red]Failed to fetch skill: {e}[/red]")
        except Exception as e:
            console.print(f"[red]Failed to import skill: {e}[/red]")
            import traceback

            traceback.print_exc()
