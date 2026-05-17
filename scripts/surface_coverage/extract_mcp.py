"""Extract MCP tools from src/nexus/config/tool_profiles.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RawMcpTool:
    name: str
    profile: str
    source: str  # "path:line" (line is best-effort 1 since YAML doesn't track per-key lines)


def extract_mcp_tools(path: Path) -> list[RawMcpTool]:
    data = yaml.safe_load(path.read_text())
    seen: dict[str, RawMcpTool] = {}
    for profile_name, profile_data in (data.get("profiles") or {}).items():
        for tool_name in (profile_data or {}).get("tools", []):
            if tool_name in seen:
                continue
            seen[tool_name] = RawMcpTool(
                name=tool_name,
                profile=profile_name,
                source=f"{path}:1",
            )
    return sorted(seen.values(), key=lambda r: r.name)
