"""Sandbox subsystem for Nexus code execution environments.

Provides sandbox lifecycle management using providers (E2B, Docker, etc.)
and authenticated sandbox creation through the Agent Registry (Issue #1307).
"""

from nexus.sandbox.auth_service import SandboxAuthResult, SandboxAuthService
from nexus.sandbox.events import AgentEventLog
from nexus.sandbox.sandbox_manager import SandboxManager
from nexus.sandbox.sandbox_provider import (
    CodeExecutionResult,
    SandboxInfo,
    SandboxNotFoundError,
    SandboxProvider,
)

__all__ = [
    "AgentEventLog",
    "CodeExecutionResult",
    "SandboxAuthResult",
    "SandboxAuthService",
    "SandboxInfo",
    "SandboxManager",
    "SandboxNotFoundError",
    "SandboxProvider",
]
