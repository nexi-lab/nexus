"""Skill document generation protocol and convenience re-export (Issue #2035).

Moved from nexus.skills.skill_generator to break cross-brick dependency.
mount_core_service.py and oauth_service.py import generate_skill_md.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SkillDocGenerator(Protocol):
    """Protocol for generating SKILL.md content."""

    def generate_skill_md(
        self,
        service_name: str,
        mount_path: str,
        mcp_tools: list[dict[str, Any]] | None = None,
        mcp_mount: Any | None = None,
        tool_defs: list[Any] | None = None,
    ) -> str:
        """Generate SKILL.md content for a service."""
        ...


def generate_skill_md(
    service_name: str,
    mount_path: str,
    mcp_tools: list[dict[str, Any]] | None = None,
    mcp_mount: Any | None = None,
    tool_defs: list[Any] | None = None,
) -> str:
    """Convenience function — delegates to nexus.skills.skill_generator.

    This re-export allows services outside the skills brick to import
    from the services layer without reaching into the brick.
    """
    from nexus.skills.skill_generator import generate_skill_md as _generate

    return _generate(
        service_name=service_name,
        mount_path=mount_path,
        mcp_tools=mcp_tools,
        mcp_mount=mcp_mount,
        tool_defs=tool_defs,
    )
