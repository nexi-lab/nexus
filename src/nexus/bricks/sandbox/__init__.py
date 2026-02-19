"""Sandbox subsystem for Nexus code execution environments.

Provides sandbox lifecycle management using providers (E2B, Docker, etc.)
and authenticated sandbox creation through the Agent Registry (Issue #1307).
Smart routing (Issue #1317) selects the cheapest provider automatically.
"""

from nexus.bricks.sandbox.auth_service import SandboxAuthResult, SandboxAuthService
from nexus.bricks.sandbox.events import AgentEventLog
from nexus.bricks.sandbox.sandbox_manager import SandboxManager
from nexus.bricks.sandbox.sandbox_provider import (
    CodeExecutionResult,
    EscalationNeeded,
    SandboxInfo,
    SandboxNotFoundError,
    SandboxProvider,
    validate_language,
)
from nexus.bricks.sandbox.sandbox_router import SandboxRouter
from nexus.bricks.sandbox.sandbox_router_metrics import SandboxRouterMetrics
from nexus.bricks.sandbox.security_profile import SandboxSecurityProfile

__all__ = [
    "AgentEventLog",
    "CodeExecutionResult",
    "EscalationNeeded",
    "SandboxAuthResult",
    "SandboxAuthService",
    "SandboxInfo",
    "SandboxManager",
    "SandboxNotFoundError",
    "SandboxProvider",
    "SandboxRouter",
    "SandboxRouterMetrics",
    "SandboxSecurityProfile",
    "validate_language",
]
