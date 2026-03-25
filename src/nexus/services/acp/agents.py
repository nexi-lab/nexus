"""Agent config schema for VFS-persisted agent definitions.

Agent configs live as files at ``/{zone}/agents/{id}/agent.json``.
Users write them via ``sys_write``; AcpService reads them on each call.
No built-in defaults — VFS is the single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Configuration for a coding agent CLI (parsed from VFS JSON)."""

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

    @classmethod
    def from_dict(cls, data: dict) -> AgentConfig:
        """Deserialize from a JSON-compatible dict (VFS agent.json)."""
        return cls(
            agent_id=data["agent_id"],
            name=data["name"],
            command=data["command"],
            prompt_flag=data.get("prompt_flag", "-p"),
            default_system_prompt=data.get("default_system_prompt"),
            extra_args=tuple(data.get("extra_args", ())),
            env=dict(data.get("env", {})),
            npx_package=data.get("npx_package"),
            acp_args=tuple(data.get("acp_args", ("--experimental-acp",))),
            enabled=data.get("enabled", True),
        )
