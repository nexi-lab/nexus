"""Skill Seekers plugin for generating skills from documentation."""

import re
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests
from rich.console import Console
from rich.progress import track

# Try to import nexus components, but make them optional for standalone testing
try:
    from nexus.core.exceptions import PermissionDeniedError
    from nexus.llm import LiteLLMProvider, LLMConfig, Message, MessageRole
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

    class LLMProvider:
        def __init__(self, model: str = "claude-3-5-sonnet-20241022"):
            pass

        async def generate(self, prompt: str, **kwargs: Any) -> str:
            return "Mock LLM response"

    class PermissionDeniedError(Exception):  # type: ignore[no-redef]
        pass


console = Console()


class SkillSeekersPlugin(NexusPlugin):
    """Plugin for generating skills from documentation with AI enhancement.

    Features:
    - llms.txt detection for 10x faster scraping
    - Firecrawl integration for multi-page crawling
    - AI-powered skill generation with Claude
    - ReBAC permission integration
    - Approval workflow support
    """

    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="skill-seekers",
            version="0.3.0",
            description="Generate skills from docs, PDFs, and GitHub using skill-seekers integration",
            author="Nexus Team",
            homepage="https://github.com/nexi-lab/nexus-plugin-skill-seekers",
            requires=["nexus-plugin-firecrawl", "skill-seekers>=2.1.0"],
        )

    def commands(self) -> dict[str, Callable]:
        """Return plugin commands."""
        return {
            "generate": self.generate_skill,
            "generate-from-pdf": self.generate_skill_from_pdf,
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
        creator_id: Optional[str] = None,
        creator_type: str = "agent",
        tenant_id: Optional[str] = None,
        use_ai: bool = True,
    ) -> Optional[str]:
        """Generate a skill from documentation URL.

        Args:
            url: Documentation URL to scrape
            name: Name for the skill (auto-generated if not provided)
            tier: Target tier (agent, tenant, system). Default: agent
            output_dir: Output directory for generated SKILL.md (optional)
            creator_id: ID of the creator (for ReBAC)
            creator_type: Type of creator (agent, user). Default: agent
            tenant_id: Tenant ID for scoping (for ReBAC)
            use_ai: Use AI enhancement (default: True)
        """
        try:
            # Check permissions based on tier
            await self._check_tier_permissions(tier, creator_id, creator_type, tenant_id)

            console.print(f"[cyan]Fetching documentation from:[/cyan] {url}")

            # Fetch documentation (tiered approach)
            content = await self._fetch_documentation(url)
            if not content:
                console.print("[red]Failed to fetch documentation[/red]")
                return None

            # Auto-generate name if not provided
            if not name:
                name = self._generate_skill_name(url)

            console.print(f"[cyan]Generating skill:[/cyan] {name}")

            # Generate SKILL.md content with optional AI enhancement
            if use_ai:
                console.print("[cyan]Enhancing with AI...[/cyan]")
                skill_content = await self._generate_skill_md_with_ai(name, url, content, tier)
            else:
                skill_content = self._generate_skill_md_basic(name, url, content, tier)

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
                skill_dir = f"{tier_paths[tier]}{name}/"
                skill_path = f"{skill_dir}SKILL.md"

                # Create directory
                try:
                    self.nx.mkdir(skill_dir, parents=True)
                except Exception:
                    pass  # Directory might already exist

                # Write skill file
                self.nx.write(skill_path, skill_content.encode("utf-8"))
                console.print(f"[green]✓ Imported to Nexus:[/green] {skill_path}")

                # Create ReBAC tuples
                await self._create_rebac_tuples(name, tier, creator_id, creator_type, tenant_id)

                # Handle approval workflow for tenant tier
                if tier == "tenant":
                    await self._submit_for_approval(name, creator_id, url)

                # Return the skill path
                return skill_path

            else:
                console.print("[yellow]Note: NexusFS not available, saved to file only[/yellow]")
                # Return the file path if output_dir was specified
                if output_dir:
                    return str(output_path)
                return None

        except PermissionDeniedError as e:
            console.print(f"[red]Permission denied: {e}[/red]")
            return None
        except Exception as e:
            console.print(f"[red]Failed to generate skill: {e}[/red]")
            import traceback

            traceback.print_exc()
            return None

    async def _check_tier_permissions(
        self,
        tier: str,
        creator_id: Optional[str],
        creator_type: str,
        tenant_id: Optional[str],
    ) -> None:
        """Check if creator has permission to create skill in this tier.

        Args:
            tier: Target tier
            creator_id: ID of the creator
            creator_type: Type of creator (agent, user)
            tenant_id: Tenant ID

        Raises:
            PermissionDeniedError: If creator lacks permission
        """
        if not creator_id:
            return  # No creator specified, skip permission checks

        # Get ReBAC manager if available
        try:
            rebac_manager = getattr(self.nx, "rebac_manager", None)
            if not rebac_manager:
                return  # No ReBAC manager, skip permission checks
        except Exception:
            return

        if tier == "system":
            # Check if creator is admin
            try:
                is_admin = await rebac_manager.rebac_check(
                    subject=(creator_type, creator_id),
                    permission="admin",
                    object=("system", "global"),
                    tenant_id=None,
                )
                if not is_admin:
                    raise PermissionDeniedError(
                        "Only system administrators can create system-tier skills"
                    )
            except PermissionDeniedError:
                raise
            except Exception:
                # If check fails, allow for backward compatibility
                pass

        elif tier == "tenant":
            # Check if creator is tenant member
            if not tenant_id:
                raise PermissionDeniedError("tenant_id required for tenant-tier skills")

            try:
                is_member = await rebac_manager.rebac_check(
                    subject=(creator_type, creator_id),
                    permission="member",
                    object=("tenant", tenant_id),
                    tenant_id=tenant_id,
                )
                if not is_member:
                    raise PermissionDeniedError(
                        f"Must be a member of tenant '{tenant_id}' to create tenant-tier skills"
                    )
            except PermissionDeniedError:
                raise
            except Exception:
                # If check fails, allow for backward compatibility
                pass

        # Agent tier: anyone can create in their own workspace

    async def _fetch_documentation(self, url: str) -> str:
        """Fetch documentation using tiered approach.

        Priority:
        1. Try llms.txt (10x faster)
        2. Use Firecrawl for multi-page crawling
        3. Fallback to basic scraping (deprecated)

        Args:
            url: Documentation URL

        Returns:
            Documentation content
        """
        # Step 1: Try llms.txt
        console.print("[cyan]→[/cyan] Checking for llms.txt...")
        content = await self._try_llms_txt(url)
        if content:
            console.print("[green]✓[/green] Found llms.txt (optimized)")
            return content

        # Step 2: Try Firecrawl
        console.print("[cyan]→[/cyan] Using Firecrawl for multi-page crawl...")
        try:
            content = await self._crawl_with_firecrawl(url)
            if content:
                console.print("[green]✓[/green] Crawled with Firecrawl")
                return content
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] Firecrawl failed: {e}")

        # Step 3: Fallback to basic scraping (deprecated)
        console.print("[yellow]→[/yellow] Falling back to basic scraping (limited)")
        return self._fetch_documentation_basic(url)

    async def _try_llms_txt(self, url: str) -> Optional[str]:
        """Try to fetch llms.txt for fast documentation access.

        Args:
            url: Documentation URL

        Returns:
            Content from llms.txt or None if not found
        """
        try:
            parsed = urlparse(url)
            llms_url = f"{parsed.scheme}://{parsed.netloc}/llms.txt"

            response = requests.get(llms_url, timeout=10)
            if response.status_code == 200:
                return response.text
        except Exception:
            pass

        return None

    async def _crawl_with_firecrawl(self, url: str) -> Optional[str]:
        """Scrape documentation with Firecrawl (single page).

        Note: Uses scrape() instead of crawl() since multi-page crawl
        requires paid Firecrawl API plan. Single page scraping works great!

        Args:
            url: Documentation URL

        Returns:
            Scraped content or None if failed
        """
        if not self.nx:
            return None

        try:
            # Use Firecrawl client directly for single-page scraping
            import os

            from nexus_firecrawl.client import FirecrawlClient

            api_key = os.environ.get("FIRECRAWL_API_KEY")
            if not api_key:
                console.print("[yellow]⚠[/yellow] FIRECRAWL_API_KEY not set")
                return None

            console.print("[cyan]→[/cyan] Scraping with Firecrawl...")

            async with FirecrawlClient(api_key=api_key) as client:
                result = await client.scrape(url=url, only_main_content=True)

                if result and result.markdown:
                    console.print(
                        f"[green]✓[/green] Scraped {len(result.markdown)} chars with Firecrawl"
                    )
                    return str(result.markdown)

                return None

        except Exception as e:
            # Firecrawl failed, will fall back to basic scraping
            console.print(f"[yellow]⚠[/yellow] Firecrawl error: {str(e)[:100]}")
            return None

    def _fetch_documentation_basic(self, url: str) -> str:
        """Fetch and extract text content from URL (basic method).

        DEPRECATED: Use llms.txt or Firecrawl instead.

        Args:
            url: Documentation URL

        Returns:
            Extracted text content
        """
        try:
            from bs4 import BeautifulSoup

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

    async def _generate_skill_md_with_ai(self, name: str, url: str, content: str, tier: str) -> str:
        """Generate SKILL.md using AI enhancement.

        Args:
            name: Skill name
            url: Source URL
            content: Documentation content
            tier: Target tier

        Returns:
            Generated SKILL.md content
        """
        try:
            import os

            # Get API key and model from environment
            api_key = (
                os.getenv("ANTHROPIC_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or os.getenv("OPENROUTER_API_KEY")
            )

            if not api_key:
                raise Exception("No LLM API key found in environment")

            # Determine model based on which API key is set

            if os.getenv("OPENROUTER_API_KEY"):
                # Use OpenRouter with a valid model (default to Claude 3.5 Sonnet)
                model = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
                # For OpenRouter, litellm automatically reads OPENROUTER_API_KEY from env
                # No need to pass api_key to config - litellm handles it
                config = LLMConfig(
                    model=f"openrouter/{model}",
                    max_output_tokens=4000,
                    temperature=0.7,
                )
            elif os.getenv("ANTHROPIC_API_KEY"):
                model = "claude-3-5-sonnet-20241022"
                # For Anthropic, litellm reads ANTHROPIC_API_KEY from env
                config = LLMConfig(
                    model=model,
                    max_output_tokens=4000,
                    temperature=0.7,
                )
            else:
                # For OpenAI, litellm reads OPENAI_API_KEY from env
                model = "gpt-4o"
                config = LLMConfig(
                    model=model,
                    max_output_tokens=4000,
                    temperature=0.7,
                )

            # Initialize LLM provider
            llm = LiteLLMProvider(config=config)

            # Create enhancement prompt
            prompt_text = f"""You are an expert at creating Claude AI skills from documentation.

Given this documentation content, create a comprehensive SKILL.md file.

**Source URL:** {url}
**Skill Name:** {name}

**Documentation Content:**
{content[:8000]}  # Limit to first 8000 chars

**Instructions:**
1. Create a clear, concise description (1-2 sentences)
2. Extract key concepts and capabilities
3. Provide practical usage examples
4. Identify important APIs, functions, or patterns
5. Add relevant tags/keywords

**Output Format:**
Return ONLY the markdown content (without the YAML frontmatter). I will add the frontmatter separately.

Start with a # heading for the skill name, then organize into these sections:
- ## Overview
- ## Key Concepts
- ## Usage Examples
- ## API Reference (if applicable)
- ## Best Practices

Be concise but comprehensive. Focus on what's most useful for an AI agent."""

            # Create messages
            messages = [Message(role=MessageRole.USER, content=prompt_text)]

            # Generate enhanced content
            response = await llm.complete_async(messages)
            enhanced_content = response.content or ""

            # Create frontmatter
            from datetime import datetime

            frontmatter = f"""---
name: {name}
version: 1.0.0
description: {self._extract_description_from_content(enhanced_content)}
author: Skill Seekers
created: {datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
source_url: {url}
tier: {tier}
tags: {self._extract_tags_from_content(enhanced_content)}
---

"""

            return frontmatter + enhanced_content

        except Exception as e:
            console.print(f"[yellow]⚠ AI enhancement failed: {e}[/yellow]")
            console.print("[yellow]→ Falling back to basic generation[/yellow]")
            return self._generate_skill_md_basic(name, url, content, tier)

    def _generate_skill_md_basic(self, name: str, url: str, content: str, tier: str) -> str:
        """Generate SKILL.md content without AI (fallback).

        Args:
            name: Skill name
            url: Source URL
            content: Documentation content
            tier: Target tier

        Returns:
            Generated SKILL.md content
        """
        # Truncate content for summary (use first 2000 chars)
        summary_content = content[:2000] + "..." if len(content) > 2000 else content

        skill_md = f"""---
name: {name}
version: 1.0.0
description: Skill generated from {url}
author: Skill Seekers
created: {self._get_timestamp()}
source_url: {url}
tier: {tier}
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

    def _extract_description_from_content(self, content: str) -> str:
        """Extract a concise description from AI-generated content.

        Args:
            content: AI-generated content

        Returns:
            Description (first meaningful sentence)
        """
        # Try to find first sentence in Overview section
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if "## Overview" in line and i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if next_line:
                    return next_line[:200]  # Limit to 200 chars

        # Fallback: first non-empty line
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                return line[:200]

        return "AI-generated skill from documentation"

    def _extract_tags_from_content(self, content: str) -> str:
        """Extract relevant tags from AI-generated content.

        Args:
            content: AI-generated content

        Returns:
            Comma-separated tags
        """
        # Extract keywords from headings and content
        keywords = self._extract_keywords(content, max_keywords=5)
        return ", ".join(keywords)

    async def _create_rebac_tuples(
        self,
        skill_name: str,
        tier: str,
        creator_id: Optional[str],
        creator_type: str,
        tenant_id: Optional[str],
    ) -> None:
        """Create ReBAC permission tuples for the skill.

        Args:
            skill_name: Name of the skill
            tier: Skill tier (agent, tenant, system)
            creator_id: ID of the creator
            creator_type: Type of creator (agent, user)
            tenant_id: Tenant ID
        """
        if not creator_id:
            return

        try:
            rebac_manager = getattr(self.nx, "rebac_manager", None)
            if not rebac_manager:
                return

            # Create ownership tuple
            await rebac_manager.rebac_write(
                subject=(creator_type, creator_id),
                relation="owner-of",
                object=("skill", skill_name),
                tenant_id=tenant_id,
            )
            console.print(f"[dim]→ Created ownership tuple for {creator_type}:{creator_id}[/dim]")

            # Create tier-specific tuples
            if tier == "system":
                # Make globally readable
                await rebac_manager.rebac_write(
                    subject=("*", "*"),
                    relation="public",
                    object=("skill", skill_name),
                    tenant_id=None,
                )
                console.print("[dim]→ Made skill publicly accessible (system tier)[/dim]")

            elif tier == "tenant" and tenant_id:
                # Associate with tenant
                await rebac_manager.rebac_write(
                    subject=("tenant", tenant_id),
                    relation="tenant",
                    object=("skill", skill_name),
                    tenant_id=tenant_id,
                )
                console.print(f"[dim]→ Associated with tenant {tenant_id}[/dim]")

        except Exception as e:
            console.print(f"[yellow]⚠ Failed to create ReBAC tuples: {e}[/yellow]")

    async def _submit_for_approval(
        self, skill_name: str, creator_id: Optional[str], url: str
    ) -> None:
        """Submit tenant-tier skill for approval.

        Args:
            skill_name: Name of the skill
            creator_id: ID of the creator
            url: Source URL
        """
        if not creator_id:
            console.print(
                "[yellow]⚠ Skill created but not submitted for approval (no creator_id)[/yellow]"
            )
            return

        try:
            from nexus.skills import SkillGovernance

            # Get database connection (simplified - in real implementation get from nx)
            db_conn = None  # TODO: Get from nx or create
            governance = SkillGovernance(db_connection=db_conn)

            approval_id = await governance.submit_for_approval(
                skill_name=skill_name,
                submitted_by=creator_id,
                comments=f"Auto-generated from {url}",
            )

            console.print(
                "[yellow]⚠[/yellow] Skill created but requires approval for tenant publication"
            )
            console.print(f"  [dim]Approval ID: {approval_id}[/dim]")
            console.print(f"  [dim]Approve with: nexus skills approve {approval_id}[/dim]")

        except Exception as e:
            console.print(f"[yellow]⚠ Failed to submit for approval: {e}[/yellow]")

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
            skill_dir = f"{tier_paths[tier]}{name}/"
            skill_path = f"{skill_dir}SKILL.md"

            # Create directory
            try:
                self.nx.mkdir(skill_dir, parents=True)
            except Exception:
                pass

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

    async def generate_skill_from_pdf(
        self,
        pdf_path: str,
        name: Optional[str] = None,
        tier: str = "agent",
        description: Optional[str] = None,
        creator_id: Optional[str] = None,
        creator_type: str = "agent",
        tenant_id: Optional[str] = None,
        use_ocr: bool = False,
        extract_images: bool = False,
        extract_tables: bool = False,
        use_ai: bool = True,
    ) -> Optional[str]:
        """Generate a skill from a PDF file using skill-seekers integration.

        Args:
            pdf_path: Path to PDF file
            name: Name for the skill (auto-generated if not provided)
            tier: Target tier (agent, tenant, system). Default: agent
            description: Skill description (optional)
            creator_id: ID of the creator (for ReBAC)
            creator_type: Type of creator (agent, user). Default: agent
            tenant_id: Tenant ID for scoping (for ReBAC)
            use_ocr: Use OCR for scanned PDFs (default: False)
            extract_images: Extract images from PDF (default: False)
            extract_tables: Extract tables from PDF (default: False)
            use_ai: Use AI enhancement (default: True)

        Returns:
            Path to generated skill or None on failure
        """
        try:
            # Import skill-seekers PDF extractor
            from skill_seekers.cli.pdf_extractor_poc import PDFExtractor

            # Check permissions based on tier
            await self._check_tier_permissions(tier, creator_id, creator_type, tenant_id)

            console.print(f"[cyan]Extracting from PDF:[/cyan] {pdf_path}")

            # Auto-generate name if not provided
            if not name:
                name = Path(pdf_path).stem.replace(" ", "-").lower()
                console.print(f"[dim]Auto-generated name: {name}[/dim]")

            # Extract PDF content using skill-seekers
            extractor = PDFExtractor(
                pdf_path=pdf_path,
                verbose=True,
                extract_images=extract_images,
                use_ocr=use_ocr,
                extract_tables=extract_tables,
                parallel=True,  # Enable parallel processing for faster extraction
                use_cache=True,  # Enable caching
            )

            # Perform extraction
            console.print("[cyan]→[/cyan] Extracting text, code, and metadata...")

            try:
                # Try using skill-seekers extractor
                extracted_data = extractor.extract_all()
            except (AssertionError, Exception) as e:
                # Fallback to simple extraction if skill-seekers fails (version incompatibility)
                console.print(f"[yellow]⚠ skill-seekers extraction failed: {e}[/yellow]")
                console.print("[cyan]→[/cyan] Falling back to simple PDF extraction...")
                extracted_data = self._extract_pdf_simple(pdf_path)

            console.print(
                f"[green]✓[/green] Extracted {len(extracted_data.get('pages', []))} pages"
            )

            # Generate skill content
            console.print(f"[cyan]Generating skill:[/cyan] {name}")

            # Build content from extracted data
            content = self._build_content_from_pdf_data(extracted_data)

            # Generate SKILL.md with optional AI enhancement
            if use_ai:
                console.print("[cyan]Enhancing with AI...[/cyan]")
                skill_content = await self._generate_skill_md_with_ai(
                    name, f"file://{pdf_path}", content, tier
                )
            else:
                skill_content = self._generate_skill_md_from_pdf(
                    name, pdf_path, content, tier, description, extracted_data
                )

            # Import to Nexus
            if self.nx:
                tier_paths = {
                    "agent": "/workspace/.nexus/skills/",
                    "tenant": "/shared/skills/",
                    "system": "/system/skills/",
                }
                skill_dir = f"{tier_paths[tier]}{name}/"
                skill_path = f"{skill_dir}SKILL.md"

                # Create directory
                try:
                    self.nx.mkdir(skill_dir, parents=True)
                except Exception:
                    pass

                # Write skill file
                self.nx.write(skill_path, skill_content.encode("utf-8"))
                console.print(f"[green]✓ Imported to Nexus:[/green] {skill_path}")

                # Create ReBAC tuples
                await self._create_rebac_tuples(name, tier, creator_id, creator_type, tenant_id)

                # Handle approval workflow for tenant tier
                if tier == "tenant":
                    await self._submit_for_approval(name, creator_id, f"file://{pdf_path}")

                return skill_path
            else:
                console.print("[yellow]Note: NexusFS not available[/yellow]")
                return None

        except ImportError as e:
            console.print(f"[red]skill-seekers package not installed: {e}[/red]")
            console.print("[yellow]Install with: pip install skill-seekers>=2.1.0[/yellow]")
            return None
        except PermissionDeniedError as e:
            console.print(f"[red]Permission denied: {e}[/red]")
            return None
        except Exception as e:
            console.print(f"[red]Failed to generate skill from PDF: {e}[/red]")
            import traceback

            traceback.print_exc()
            return None

    def _build_content_from_pdf_data(self, extracted_data: dict) -> str:
        """Build markdown content from extracted PDF data.

        Args:
            extracted_data: Extracted data from PDFExtractor

        Returns:
            Markdown content string
        """
        content_parts = []

        # Add text from pages
        for page in extracted_data.get("pages", []):
            page_num = page.get("page_num", 0)

            # Add tables first
            for table in page.get("tables", []):
                headers = table.get("headers", [])
                data = table.get("data", [])
                if data:
                    content_parts.append(f"### Table on Page {page_num}\n\n")
                    # Format as markdown table
                    if headers:
                        content_parts.append("| " + " | ".join(str(h) for h in headers) + " |")
                        content_parts.append("| " + " | ".join("---" for _ in headers) + " |")
                    for row in data:
                        content_parts.append("| " + " | ".join(str(cell) for cell in row) + " |")
                    content_parts.append("\n")

            text = page.get("text", "").strip()
            if text:
                content_parts.append(f"## Page {page_num}\n\n{text}\n")

            # Add code blocks
            for code_block in page.get("code_blocks", []):
                language = code_block.get("language", "")
                code = code_block.get("code", "")
                if code:
                    content_parts.append(f"```{language}\n{code}\n```\n")

        # Add chapter information if available
        chapters = extracted_data.get("chapters", [])
        if chapters:
            content_parts.insert(0, "## Table of Contents\n\n")
            for chapter in chapters:
                title = chapter.get("title", "Unknown")
                page_range = chapter.get("page_range", [])
                content_parts.insert(1, f"- {title} (Pages {page_range})\n")
            content_parts.insert(len(chapters) + 1, "\n")

        return "\n".join(content_parts)

    def _generate_skill_md_from_pdf(
        self,
        name: str,
        pdf_path: str,
        content: str,
        tier: str,
        description: Optional[str],
        extracted_data: dict,
    ) -> str:
        """Generate SKILL.md from PDF without AI enhancement.

        Args:
            name: Skill name
            pdf_path: PDF file path
            content: Extracted content
            tier: Target tier
            description: Optional description
            extracted_data: Full extracted data

        Returns:
            Generated SKILL.md content
        """
        from datetime import datetime

        # Generate description if not provided
        if not description:
            description = f"Skill generated from PDF: {Path(pdf_path).name}"

        # Get statistics
        stats = extracted_data.get("statistics", {})
        page_count = len(extracted_data.get("pages", []))
        code_blocks = sum(len(p.get("code_blocks", [])) for p in extracted_data.get("pages", []))

        skill_md = f"""---
name: {name}
version: 1.0.0
description: "{description}"
author: "Skill Seekers (PDF Extraction)"
created: {datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
source: "file://{pdf_path}"
tier: {tier}
metadata:
  pages: {page_count}
  code_blocks: {code_blocks}
  extraction_method: skill-seekers
---

# {name.replace("-", " ").title()}

## Overview

This skill was automatically generated from PDF documentation using the skill-seekers integration.

**Source:** `{Path(pdf_path).name}`
**Pages:** {page_count}
**Code Blocks Detected:** {code_blocks}

## Content

{content[:10000]}{"..." if len(content) > 10000 else ""}

## Extraction Statistics

- Total Pages: {page_count}
- Code Blocks: {code_blocks}
- Languages Detected: {", ".join(stats.get("languages", {}).keys()) if stats else "N/A"}

---

*Generated by Nexus Skill Seekers Plugin v0.3.0*
*Using skill-seekers PDF extraction engine*
"""
        return skill_md

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

    def _fix_numbered_lists(self, md_text: str) -> str:
        """Fix numbered lists where numbers are separated from descriptions.

        This handles PDFs with complex layouts where pymupdf4llm extracts numbers
        on separate lines from their descriptions.

        Args:
            md_text: Raw markdown text from pymupdf4llm

        Returns:
            Cleaned markdown with numbered items properly formatted
        """
        import re

        # Find sections with isolated numbers followed by paragraphs
        # Pattern: multiple lines of just "N)" followed by content
        lines = md_text.split("\n")

        # Check if we have the problematic pattern: isolated numbers
        isolated_numbers = []
        for i, line in enumerate(lines):
            if re.match(r"^\d+\)$", line.strip()):
                isolated_numbers.append(i)

        # If we found isolated numbers, reconstruct the list
        if len(isolated_numbers) >= 3:  # At least 3 items suggests it's a real list
            # Find where the descriptions start (after all the isolated numbers)
            desc_start = isolated_numbers[-1] + 1

            # Skip blank lines
            while desc_start < len(lines) and not lines[desc_start].strip():
                desc_start += 1

            # Collect all description paragraphs
            descriptions: list[str] = []
            current_para: list[str] = []

            for i in range(desc_start, len(lines)):
                line = lines[i].strip()
                if not line:
                    if current_para:
                        descriptions.append(" ".join(current_para))
                        current_para = []
                else:
                    current_para.append(line)

            if current_para:
                descriptions.append(" ".join(current_para))

            # Build the numbered list
            numbered_list = []
            for i, desc in enumerate(descriptions[: len(isolated_numbers)], 1):
                numbered_list.append(f"\n**{i}) {desc}**\n")

            # Reconstruct the markdown: keep everything before the numbers, replace with new list
            before_numbers = "\n".join(lines[: isolated_numbers[0]])
            after_list = "\n".join(lines[desc_start + len("\n".join(descriptions).split("\n")) :])

            return before_numbers + "\n\n" + "\n".join(numbered_list) + "\n\n" + after_list

        return md_text

    def _extract_pdf_simple(self, pdf_path: str) -> dict:
        """Simple PDF extraction fallback using PyMuPDF4LLM.

        Args:
            pdf_path: Path to PDF file

        Returns:
            Dictionary with extracted data in skill-seekers format
        """
        try:
            import pymupdf4llm

            # Use pymupdf4llm for better markdown extraction with structure preservation
            console.print("[cyan]→[/cyan] Using pymupdf4llm for enhanced extraction...")
            md_text = pymupdf4llm.to_markdown(pdf_path)

            # Post-process: Fix isolated numbered lists that got separated from descriptions
            # This handles PDFs with 2-column layouts where numbers and descriptions are separate
            md_text = self._fix_numbered_lists(md_text)

            # Parse markdown back into structured format
            pages = [
                {
                    "page_num": 1,
                    "text": md_text,
                    "code_blocks": [],
                    "tables": [],
                }
            ]

            return {
                "pages": pages,
                "chapters": [],
                "statistics": {
                    "total_pages": 1,
                    "total_chars": len(md_text),
                    "languages": {},
                },
            }

        except Exception as e:
            console.print(f"[yellow]⚠ pymupdf4llm extraction failed: {e}[/yellow]")
            console.print("[cyan]→[/cyan] Falling back to basic PyMuPDF extraction...")

            # Ultimate fallback: basic PyMuPDF
            import fitz

            doc = fitz.open(pdf_path)
            pages = []

            for page_num in range(len(doc)):
                page = doc[page_num]
                text = page.get_text("text")

                # Extract tables from this page
                tables = []
                try:
                    table_finder = page.find_tables()
                    for table in table_finder.tables:
                        # Convert table to pandas DataFrame then to dict
                        df = table.to_pandas()
                        tables.append(
                            {
                                "data": df.values.tolist(),
                                "headers": df.columns.tolist(),
                            }
                        )
                except Exception:
                    pass  # Silently skip table extraction errors in fallback

                page_data = {
                    "page_num": page_num + 1,
                    "text": text,
                    "code_blocks": [],
                    "tables": tables,
                }
                pages.append(page_data)

            doc.close()

            return {
                "pages": pages,
                "chapters": [],
                "statistics": {
                    "total_pages": len(pages),
                    "total_chars": sum(len(str(p.get("text", ""))) for p in pages),
                    "languages": {},
                },
            }
