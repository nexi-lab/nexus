"""Sandbox security audit logging.

Structured logging for sandbox lifecycle and security events.
All events use the standard Python ``logging`` module with structured
extra fields for log aggregation and alerting.

Issue #1000: Enhance agent sandboxing with network isolation.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.sandbox.security_profile import SandboxSecurityProfile

logger = logging.getLogger("nexus.sandbox.audit")


class SandboxAuditLogger:
    """Logs sandbox security events for monitoring and alerting.

    All methods are synchronous, fire-and-forget. They never raise
    exceptions — logging failures are caught with a stderr fallback
    to avoid disrupting sandbox operations while still surfacing issues.

    Usage::

        audit = SandboxAuditLogger()
        audit.log_creation("abc123", profile, agent_id="user1,SkillBuilder")
        audit.log_violation("abc123", "network_escape", "curl to 8.8.8.8")
        audit.log_destruction("abc123")
    """

    def log_creation(
        self,
        sandbox_id: str,
        profile: SandboxSecurityProfile,
        agent_id: str | None = None,
    ) -> None:
        """Log sandbox creation with security profile details.

        Args:
            sandbox_id: Unique sandbox identifier.
            profile: Security profile applied to the sandbox.
            agent_id: Optional agent identifier.
        """
        try:
            logger.info(
                "Sandbox created: %s (profile=%s, agent=%s, network=%s, "
                "fuse=%s, egress_domains=%d)",
                sandbox_id,
                profile.name,
                agent_id or "unknown",
                profile.network_mode or "bridge",
                profile.allow_fuse,
                len(profile.allowed_egress_domains),
                extra=self._creation_extra(sandbox_id, profile, agent_id),
            )
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            print(f"[audit] log_creation failed: {exc}", file=sys.stderr)

    def log_violation(
        self,
        sandbox_id: str,
        violation_type: str,
        details: str,
    ) -> None:
        """Log a sandbox security violation.

        Args:
            sandbox_id: Sandbox where the violation occurred.
            violation_type: Category of violation (e.g., ``"network_escape"``,
                ``"capability_escalation"``, ``"filesystem_write"``).
            details: Human-readable description of the violation.
        """
        try:
            logger.warning(
                "Sandbox violation: %s [%s] %s",
                sandbox_id,
                violation_type,
                details,
                extra={
                    "event": "sandbox_violation",
                    "sandbox_id": sandbox_id,
                    "violation_type": violation_type,
                    "details": details,
                },
            )
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            print(f"[audit] log_violation failed: {exc}", file=sys.stderr)

    def log_destruction(
        self,
        sandbox_id: str,
        reason: str = "normal",
    ) -> None:
        """Log sandbox destruction.

        Args:
            sandbox_id: Sandbox being destroyed.
            reason: Why the sandbox was destroyed (e.g., ``"normal"``,
                ``"timeout"``, ``"error"``).
        """
        try:
            logger.info(
                "Sandbox destroyed: %s (reason=%s)",
                sandbox_id,
                reason,
                extra={
                    "event": "sandbox_destroyed",
                    "sandbox_id": sandbox_id,
                    "reason": reason,
                },
            )
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            print(f"[audit] log_destruction failed: {exc}", file=sys.stderr)

    def log_egress_attempt(
        self,
        sandbox_id: str,
        domain: str,
        allowed: bool,
    ) -> None:
        """Log an egress attempt through the proxy.

        Args:
            sandbox_id: Sandbox making the request.
            domain: Target domain.
            allowed: Whether the proxy allowed the request.
        """
        try:
            level = logging.DEBUG if allowed else logging.ERROR
            logger.log(
                level,
                "Egress %s: %s -> %s",
                "allowed" if allowed else "BLOCKED",
                sandbox_id,
                domain,
                extra={
                    "event": "sandbox_egress",
                    "sandbox_id": sandbox_id,
                    "domain": domain,
                    "allowed": allowed,
                },
            )
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            print(f"[audit] log_egress_attempt failed: {exc}", file=sys.stderr)

    @staticmethod
    def _creation_extra(
        sandbox_id: str,
        profile: SandboxSecurityProfile,
        agent_id: str | None,
    ) -> dict[str, Any]:
        """Build structured extra dict for creation events."""
        return {
            "event": "sandbox_created",
            "sandbox_id": sandbox_id,
            "profile_name": profile.name,
            "agent_id": agent_id,
            "network_mode": profile.network_mode,
            "allow_fuse": profile.allow_fuse,
            "read_only_root": profile.read_only_root,
            "memory_limit": profile.memory_limit,
            "cpu_limit": profile.cpu_limit,
            "egress_domain_count": len(profile.allowed_egress_domains),
        }
