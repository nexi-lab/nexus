"""Agent CLI configurations for the ACP system service.

Each ``AgentConfig`` maps an ``agent_id`` key to a CLI binary and its
invocation flags.  Multiple configs can target the same binary with
different models (e.g., ``claude-opus``, ``claude-sonnet``).

Agent catalog sourced from AionUi (``src/types/acpTypes.ts``).

VFS convention: runtime agent configs are persisted as files at
``/{zone}/agents/{id}/agent.json`` via ``sys_write``.  ``BUILTIN_AGENTS``
are compile-time defaults; VFS-persisted configs override them on load.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Configuration for a coding agent CLI."""

    agent_id: str  # 'claude', 'codex', 'gemini', etc.
    name: str  # Display name
    command: str  # CLI binary ('claude', 'gemini', 'codex')
    prompt_flag: str = "-p"  # Flag for one-shot prompt text
    default_system_prompt: str | None = None  # Static system prompt (VFS overrides this)
    extra_args: tuple[str, ...] = ()  # Additional flags
    env: dict[str, str] = field(default_factory=dict)
    npx_package: str | None = None  # NPX fallback package
    acp_args: tuple[str, ...] = ("--experimental-acp",)  # ACP session flags
    enabled: bool = True


# ---------------------------------------------------------------------------
# Built-in agents — mirrors AionUi ACP_BACKENDS_ALL
# ---------------------------------------------------------------------------

BUILTIN_AGENTS: dict[str, AgentConfig] = {
    # --- Claude Code ---
    "claude": AgentConfig(
        agent_id="claude",
        name="Claude Code",
        command="claude",
        prompt_flag="-p",
        extra_args=("--output-format", "json"),
        npx_package="@zed-industries/claude-agent-acp@0.20.2",
        acp_args=("--experimental-acp", "--dangerously-skip-permissions"),
    ),
    # --- Codex CLI (OpenAI) ---
    "codex": AgentConfig(
        agent_id="codex",
        name="Codex CLI",
        command="codex",
        prompt_flag="-q",
        env={"CODEX_NO_INTERACTIVE": "1", "CODEX_AUTO_CONTINUE": "1"},
        npx_package="@zed-industries/codex-acp@latest",
        acp_args=("-c", 'approval_policy="never"'),
    ),
    # --- Gemini CLI ---
    "gemini": AgentConfig(
        agent_id="gemini",
        name="Gemini CLI",
        command="gemini",
        prompt_flag="--prompt",
        acp_args=("--experimental-acp", "--yolo"),
        enabled=True,
    ),
    # --- Qwen Code ---
    "qwen": AgentConfig(
        agent_id="qwen",
        name="Qwen Code",
        command="qwen",
        prompt_flag="-p",
        npx_package="@qwen-code/qwen-code",
        acp_args=("--acp",),
    ),
    # --- CodeBuddy (Tencent) ---
    "codebuddy": AgentConfig(
        agent_id="codebuddy",
        name="CodeBuddy",
        command="codebuddy",
        prompt_flag="-p",
        npx_package="@tencent-ai/codebuddy-code",
        acp_args=("--acp",),
    ),
    # --- Goose (Block) ---
    "goose": AgentConfig(
        agent_id="goose",
        name="Goose",
        command="goose",
        prompt_flag="-p",
        acp_args=("acp",),  # subcommand
    ),
    # --- GitHub Copilot ---
    "copilot": AgentConfig(
        agent_id="copilot",
        name="GitHub Copilot",
        command="copilot",
        prompt_flag="-p",
        acp_args=("--acp", "--stdio"),
    ),
    # --- Augment Code (Auggie) ---
    "auggie": AgentConfig(
        agent_id="auggie",
        name="Augment Code",
        command="auggie",
        prompt_flag="-p",
        acp_args=("--acp",),
    ),
    # --- Kimi CLI (Moonshot) ---
    "kimi": AgentConfig(
        agent_id="kimi",
        name="Kimi CLI",
        command="kimi",
        prompt_flag="-p",
        acp_args=("acp",),  # subcommand
    ),
    # --- OpenCode ---
    "opencode": AgentConfig(
        agent_id="opencode",
        name="OpenCode",
        command="opencode",
        prompt_flag="-p",
        acp_args=("acp",),  # subcommand
    ),
    # --- Factory Droid ---
    "droid": AgentConfig(
        agent_id="droid",
        name="Factory Droid",
        command="droid",
        prompt_flag="-p",
        acp_args=("exec", "--output-format", "acp"),
    ),
    # --- Qoder CLI ---
    "qoder": AgentConfig(
        agent_id="qoder",
        name="Qoder CLI",
        command="qodercli",
        prompt_flag="-p",
        acp_args=("--acp",),
    ),
    # --- Mistral Vibe ---
    "vibe": AgentConfig(
        agent_id="vibe",
        name="Mistral Vibe",
        command="vibe-acp",
        prompt_flag="-p",
        acp_args=(),  # ACP by default
    ),
    # --- iFlow CLI ---
    "iflow": AgentConfig(
        agent_id="iflow",
        name="iFlow CLI",
        command="iflow",
        prompt_flag="-p",
        acp_args=("--experimental-acp",),
    ),
    # --- Nano Bot ---
    "nanobot": AgentConfig(
        agent_id="nanobot",
        name="Nano Bot",
        command="nanobot",
        prompt_flag="-p",
        acp_args=("--experimental-acp",),
    ),
}
