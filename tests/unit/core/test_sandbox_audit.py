"""Tests for SandboxAuditLogger (Issue #1000).

Covers creation, violation, destruction, and egress logging.
"""

from __future__ import annotations

import logging

import pytest

from nexus.sandbox.sandbox_audit import SandboxAuditLogger
from nexus.sandbox.security_profile import SandboxSecurityProfile


@pytest.fixture()
def audit() -> SandboxAuditLogger:
    return SandboxAuditLogger()


@pytest.fixture()
def strict_profile() -> SandboxSecurityProfile:
    return SandboxSecurityProfile.strict()


@pytest.fixture()
def standard_profile() -> SandboxSecurityProfile:
    return SandboxSecurityProfile.standard()


# ---------------------------------------------------------------------------
# Creation logging
# ---------------------------------------------------------------------------


class TestLogCreation:
    def test_logs_creation_info(
        self,
        audit: SandboxAuditLogger,
        strict_profile: SandboxSecurityProfile,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="nexus.sandbox.audit"):
            audit.log_creation("abc123", strict_profile, agent_id="user1,UntrustedAgent")

        assert "abc123" in caplog.text
        assert "strict" in caplog.text
        assert "user1,UntrustedAgent" in caplog.text

    def test_logs_profile_details(
        self,
        audit: SandboxAuditLogger,
        standard_profile: SandboxSecurityProfile,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="nexus.sandbox.audit"):
            audit.log_creation("def456", standard_profile)

        assert "standard" in caplog.text
        assert "none" in caplog.text  # network_mode

    def test_unknown_agent_when_none(
        self,
        audit: SandboxAuditLogger,
        strict_profile: SandboxSecurityProfile,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="nexus.sandbox.audit"):
            audit.log_creation("xyz789", strict_profile)

        assert "unknown" in caplog.text

    def test_creation_extra_fields(
        self,
        audit: SandboxAuditLogger,
        strict_profile: SandboxSecurityProfile,
    ) -> None:
        extra = SandboxAuditLogger._creation_extra(
            "abc123",
            strict_profile,
            "user1,UntrustedAgent",
        )
        assert extra["event"] == "sandbox_created"
        assert extra["sandbox_id"] == "abc123"
        assert extra["profile_name"] == "strict"
        assert extra["agent_id"] == "user1,UntrustedAgent"
        assert extra["network_mode"] == "none"
        assert extra["allow_fuse"] is False
        assert extra["read_only_root"] is True


# ---------------------------------------------------------------------------
# Violation logging
# ---------------------------------------------------------------------------


class TestLogViolation:
    def test_logs_violation_warning(
        self,
        audit: SandboxAuditLogger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="nexus.sandbox.audit"):
            audit.log_violation("abc123", "network_escape", "curl to 8.8.8.8")

        assert "abc123" in caplog.text
        assert "network_escape" in caplog.text
        assert "curl to 8.8.8.8" in caplog.text

    def test_violation_is_warning_level(
        self,
        audit: SandboxAuditLogger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="nexus.sandbox.audit"):
            audit.log_violation("abc123", "capability_escalation", "attempted SYS_ADMIN")

        assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Destruction logging
# ---------------------------------------------------------------------------


class TestLogDestruction:
    def test_logs_destruction(
        self,
        audit: SandboxAuditLogger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="nexus.sandbox.audit"):
            audit.log_destruction("abc123")

        assert "abc123" in caplog.text
        assert "normal" in caplog.text

    def test_custom_reason(
        self,
        audit: SandboxAuditLogger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="nexus.sandbox.audit"):
            audit.log_destruction("abc123", reason="timeout")

        assert "timeout" in caplog.text


# ---------------------------------------------------------------------------
# Egress logging
# ---------------------------------------------------------------------------


class TestLogEgressAttempt:
    def test_allowed_egress_is_debug(
        self,
        audit: SandboxAuditLogger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="nexus.sandbox.audit"):
            audit.log_egress_attempt("abc123", "api.openai.com", allowed=True)

        assert "allowed" in caplog.text
        assert "api.openai.com" in caplog.text
        assert any(r.levelno == logging.DEBUG for r in caplog.records)

    def test_blocked_egress_is_error(
        self,
        audit: SandboxAuditLogger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="nexus.sandbox.audit"):
            audit.log_egress_attempt("abc123", "evil.com", allowed=False)

        assert "BLOCKED" in caplog.text
        assert "evil.com" in caplog.text
        assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


class TestErrorResilience:
    def test_creation_never_raises(self, audit: SandboxAuditLogger) -> None:
        """log_creation should never raise, even with bad input."""
        # Pass something that would fail attribute access
        audit.log_creation("id", None)  # type: ignore[arg-type]

    def test_violation_never_raises(self, audit: SandboxAuditLogger) -> None:
        audit.log_violation("id", "type", "details")

    def test_destruction_never_raises(self, audit: SandboxAuditLogger) -> None:
        audit.log_destruction("id")

    def test_egress_never_raises(self, audit: SandboxAuditLogger) -> None:
        audit.log_egress_attempt("id", "domain", allowed=True)
