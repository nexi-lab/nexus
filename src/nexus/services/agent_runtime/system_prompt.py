"""System prompt assembly for ManagedAgentLoop (nexus-agent-plan §4.2).

Assembles the system prompt from multiple VFS sources, matching Claude Code's
19-section system prompt structure:

    Static sections (1-4):   {agent_path}/SYSTEM.md
    Dynamic env (5-8):       Generated at runtime (platform, model, git status)
    Prompt fragments (10+):  {agent_path}/prompts/*.md (optional)
    Project context (12):    {cwd}/.nexus/agent.md (equiv to CLAUDE.md)

Tool descriptions (section 9) go in the API ``tools`` parameter, not in
system prompt text — handled by ToolRegistry.schemas() in ManagedAgentLoop.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
from collections.abc import Awaitable, Callable

from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)

# Type alias for injected kernel callable
SysReadFn = Callable[[str], Awaitable[bytes]]

# Optional prompt fragment names (loaded from {agent_path}/prompts/{name}.md)
_PROMPT_FRAGMENTS = (
    "output_efficiency",
    "json_formatting",
    "tool_batching",
)


async def assemble_system_prompt(
    *,
    sys_read: SysReadFn,
    zone_id: str = ROOT_ZONE_ID,
    agent_id: str = "",
    cwd: str = "",
    model: str | None = None,
) -> str:
    """Assemble multi-section system prompt from VFS sources.

    Uses ``vfs_paths.agent`` for all path construction (no hardcoded paths).

    Args:
        sys_read: Kernel sys_read callable for VFS file access.
        zone_id: Zone ID for VFS path construction.
        agent_id: Agent ID for VFS path construction.
        cwd: Working directory for project context (.nexus/agent.md).
        model: Model name for environment block.

    Returns:
        Assembled system prompt string. Empty string if no sources found.
    """
    from nexus.contracts.vfs_paths import agent as agent_paths

    parts: list[str] = []

    # Sections 1-4: Static identity from SYSTEM.md
    system_md = await _read_vfs_text(sys_read, agent_paths.system_prompt(zone_id, agent_id))
    if system_md:
        parts.append(system_md)

    # Sections 5-8: Dynamic environment block
    parts.append(_generate_env_block(model=model, cwd=cwd))

    # Sections 10, 14, 15: Optional prompt fragments
    for name in _PROMPT_FRAGMENTS:
        fragment = await _read_vfs_text(
            sys_read, agent_paths.prompt_fragment(zone_id, agent_id, name)
        )
        if fragment:
            parts.append(fragment)

    # Section 12: Project context (.nexus/agent.md — equiv to CLAUDE.md)
    if cwd:
        project_ctx = await _read_vfs_text(sys_read, f"{cwd}/.nexus/agent.md")
        if project_ctx:
            parts.append(project_ctx)

    return "\n\n".join(parts)


def _generate_env_block(*, model: str | None = None, cwd: str = "") -> str:
    """Generate dynamic environment info (sections 5-8).

    Includes platform, shell, model identity, date, and git status.
    """
    lines = [
        "# Environment",
        f"- Platform: {platform.system()} {platform.machine()}",
        f"- Shell: {os.environ.get('SHELL', 'unknown')}",
        f"- Python: {platform.python_version()}",
    ]

    if model:
        lines.append(f"- Model: {model}")

    if cwd:
        lines.append(f"- Working directory: {cwd}")
        git_info = _get_git_status(cwd)
        if git_info:
            lines.append(f"- Git: {git_info}")

    return "\n".join(lines)


def _get_git_status(cwd: str) -> str:
    """Get brief git status for environment block."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        if branch.returncode != 0:
            return ""

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        branch_name = branch.stdout.strip()
        changed = len(status.stdout.strip().splitlines()) if status.stdout.strip() else 0
        if changed:
            return f"branch={branch_name}, {changed} changed files"
        return f"branch={branch_name}, clean"
    except Exception:
        return ""


async def _read_vfs_text(sys_read: SysReadFn, path: str) -> str:
    """Read a VFS file as UTF-8 text, return empty string on failure."""
    try:
        data = await sys_read(path)
        return data.decode("utf-8").strip()
    except Exception:
        return ""
