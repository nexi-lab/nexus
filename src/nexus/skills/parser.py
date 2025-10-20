"""Parser for SKILL.md files with YAML frontmatter."""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from nexus.core.exceptions import ValidationError
from nexus.skills.models import Skill, SkillMetadata

logger = logging.getLogger(__name__)


class SkillParseError(ValidationError):
    """Raised when parsing a SKILL.md file fails."""

    pass


class SkillParser:
    """Parser for SKILL.md files with YAML frontmatter.

    SKILL.md format:
        ---
        name: skill-name
        description: Skill description
        version: 1.0.0
        author: Author Name
        requires:
          - dependency-skill-1
          - dependency-skill-2
        ---

        # Skill Content

        Markdown content describing the skill...

    Example:
        >>> parser = SkillParser()
        >>> skill = parser.parse_file("/path/to/SKILL.md")
        >>> print(skill.metadata.name)
        'skill-name'
        >>> print(skill.content[:50])
        '# Skill Content\\n\\nMarkdown content...'
    """

    # Regex to match YAML frontmatter
    FRONTMATTER_PATTERN = re.compile(
        r"^---\s*\n(.*?)\n---\s*\n(.*)$",
        re.DOTALL,
    )

    def parse_file(self, file_path: str | Path, tier: str | None = None) -> Skill:
        """Parse a SKILL.md file.

        Args:
            file_path: Path to the SKILL.md file
            tier: Optional tier (agent, tenant, system) for this skill

        Returns:
            Parsed Skill object

        Raises:
            SkillParseError: If parsing fails
        """
        file_path = Path(file_path)

        if not file_path.exists():
            raise SkillParseError(f"Skill file not found: {file_path}")

        if not file_path.is_file():
            raise SkillParseError(f"Skill path is not a file: {file_path}")

        try:
            content = file_path.read_text(encoding="utf-8")
            skill = self.parse_content(content, str(file_path), tier)
            return skill
        except Exception as e:
            if isinstance(e, SkillParseError):
                raise
            raise SkillParseError(f"Failed to parse skill file: {e}", path=str(file_path)) from e

    def parse_content(
        self, content: str, file_path: str | None = None, tier: str | None = None
    ) -> Skill:
        """Parse SKILL.md content.

        Args:
            content: Raw SKILL.md content
            file_path: Optional file path (for error messages)
            tier: Optional tier (agent, tenant, system) for this skill

        Returns:
            Parsed Skill object

        Raises:
            SkillParseError: If parsing fails
        """
        # Match frontmatter
        match = self.FRONTMATTER_PATTERN.match(content)

        if not match:
            raise SkillParseError(
                "SKILL.md must have YAML frontmatter (---\\n...\\n---)", path=file_path
            )

        frontmatter_yaml, markdown_content = match.groups()

        # Parse YAML frontmatter
        try:
            frontmatter = yaml.safe_load(frontmatter_yaml) or {}
        except yaml.YAMLError as e:
            raise SkillParseError(f"Invalid YAML frontmatter: {e}", path=file_path) from e

        if not isinstance(frontmatter, dict):
            raise SkillParseError(
                f"Frontmatter must be a YAML dict, got {type(frontmatter).__name__}",
                path=file_path,
            )

        # Extract metadata
        metadata = self._parse_metadata(frontmatter, file_path, tier)

        # Create Skill object
        skill = Skill(metadata=metadata, content=markdown_content.strip())

        # Validate (but only if content is not empty - empty content is allowed)
        if skill.content:
            skill.validate()
        else:
            # Just validate metadata
            metadata.validate()

        return skill

    def parse_metadata_from_content(
        self, content: str, file_path: str | None = None, tier: str | None = None
    ) -> SkillMetadata:
        """Parse metadata from SKILL.md content string.

        Args:
            content: Raw SKILL.md content
            file_path: Optional file path (for metadata)
            tier: Optional tier (agent, tenant, system) for this skill

        Returns:
            SkillMetadata object (without full content)

        Raises:
            SkillParseError: If parsing fails
        """
        # Extract just the frontmatter
        match = self.FRONTMATTER_PATTERN.match(content)
        if not match:
            raise SkillParseError(
                "SKILL.md must have YAML frontmatter (---\\n...\\n---)",
                path=file_path,
            )

        frontmatter_yaml = match.group(1)

        # Parse YAML
        try:
            frontmatter = yaml.safe_load(frontmatter_yaml) or {}
        except yaml.YAMLError as e:
            raise SkillParseError(f"Invalid YAML frontmatter: {e}", path=file_path) from e

        # Extract metadata
        metadata = self._parse_metadata(frontmatter, file_path, tier)
        metadata.validate()

        return metadata

    def parse_metadata_only(self, file_path: str | Path, tier: str | None = None) -> SkillMetadata:
        """Parse only the metadata (frontmatter) from a SKILL.md file.

        This is for progressive disclosure - load metadata during discovery,
        load full content on-demand.

        Args:
            file_path: Path to the SKILL.md file
            tier: Optional tier (agent, tenant, system) for this skill

        Returns:
            SkillMetadata object (without full content)

        Raises:
            SkillParseError: If parsing fails
        """
        file_path = Path(file_path)

        if not file_path.exists():
            raise SkillParseError(f"Skill file not found: {file_path}")

        try:
            content = file_path.read_text(encoding="utf-8")

            # Extract just the frontmatter
            match = self.FRONTMATTER_PATTERN.match(content)
            if not match:
                raise SkillParseError(
                    "SKILL.md must have YAML frontmatter (---\\n...\\n---)",
                    path=str(file_path),
                )

            frontmatter_yaml = match.group(1)

            # Parse YAML
            try:
                frontmatter = yaml.safe_load(frontmatter_yaml) or {}
            except yaml.YAMLError as e:
                raise SkillParseError(f"Invalid YAML frontmatter: {e}", path=str(file_path)) from e

            # Extract metadata
            metadata = self._parse_metadata(frontmatter, str(file_path), tier)
            metadata.validate()

            return metadata

        except Exception as e:
            if isinstance(e, SkillParseError):
                raise
            raise SkillParseError(
                f"Failed to parse skill metadata: {e}", path=str(file_path)
            ) from e

    def _parse_metadata(
        self, frontmatter: dict[str, Any], file_path: str | None, tier: str | None
    ) -> SkillMetadata:
        """Parse metadata from frontmatter dict.

        Args:
            frontmatter: Parsed YAML frontmatter
            file_path: Optional file path (for metadata)
            tier: Optional tier (agent, tenant, system)

        Returns:
            SkillMetadata object

        Raises:
            SkillParseError: If required fields are missing
        """
        # Required fields
        name = frontmatter.get("name")
        description = frontmatter.get("description")

        if not name:
            raise SkillParseError("Skill 'name' is required in frontmatter", path=file_path)

        if not description:
            raise SkillParseError(
                f"Skill 'description' is required in frontmatter for '{name}'",
                path=file_path,
            )

        # Optional fields
        version = frontmatter.get("version")
        author = frontmatter.get("author")
        requires = frontmatter.get("requires", [])

        # Parse dates if present
        created_at = self._parse_datetime(frontmatter.get("created_at"))
        modified_at = self._parse_datetime(frontmatter.get("modified_at"))

        # Ensure requires is a list
        if not isinstance(requires, list):
            if isinstance(requires, str):
                requires = [requires]
            else:
                raise SkillParseError(
                    f"'requires' must be a list or string, got {type(requires).__name__}",
                    path=file_path,
                )

        # Extract additional metadata (everything not in known fields)
        known_fields = {
            "name",
            "description",
            "version",
            "author",
            "requires",
            "created_at",
            "modified_at",
        }
        additional_metadata = {k: v for k, v in frontmatter.items() if k not in known_fields}

        return SkillMetadata(
            name=name,
            description=description,
            version=version,
            author=author,
            created_at=created_at,
            modified_at=modified_at,
            requires=requires,
            metadata=additional_metadata,
            file_path=file_path,
            tier=tier,
        )

    def _parse_datetime(self, value: Any) -> datetime | None:
        """Parse datetime from various formats.

        Args:
            value: Datetime value (str, datetime, or None)

        Returns:
            datetime object or None
        """
        if value is None:
            return None

        if isinstance(value, datetime):
            return value

        if isinstance(value, str):
            # Try ISO format
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                logger.warning(f"Failed to parse datetime: {value}")
                return None

        logger.warning(f"Unexpected datetime type: {type(value).__name__}")
        return None
