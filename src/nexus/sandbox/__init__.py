"""Sandbox subsystem for Nexus code execution environments.

Provides sandbox lifecycle management using providers (E2B, Docker, etc.).
"""

from nexus.sandbox.sandbox_manager import SandboxManager
from nexus.sandbox.sandbox_provider import (
    CodeExecutionResult,
    SandboxInfo,
    SandboxNotFoundError,
    SandboxProvider,
)

__all__ = [
    "CodeExecutionResult",
    "SandboxInfo",
    "SandboxManager",
    "SandboxNotFoundError",
    "SandboxProvider",
]
