"""Agent self-observability brick — mounts /.activity/ and owns ReBAC grants."""

from nexus.bricks.agent_log.brick import AgentLogService

__all__ = ["AgentLogService"]
