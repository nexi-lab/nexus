"""Tests for SSRFConfig / SecurityConfig (Issue #3792)."""

import pytest
from pydantic import ValidationError

from nexus.config import NexusConfig, SecurityConfig, SSRFConfig


class TestSSRFConfigDefaults:
    def test_defaults_safe(self) -> None:
        cfg = SSRFConfig()
        assert cfg.allow_private is False
        assert cfg.extra_deny_cidrs == ()

    def test_nexus_config_has_security_ssrf(self) -> None:
        cfg = NexusConfig()
        assert isinstance(cfg.security, SecurityConfig)
        assert isinstance(cfg.security.ssrf, SSRFConfig)
        assert cfg.security.ssrf.allow_private is False


class TestSSRFConfigValidation:
    def test_valid_extra_deny_cidrs(self) -> None:
        cfg = SSRFConfig(extra_deny_cidrs=["10.100.0.0/16", "203.0.113.0/24"])
        assert len(cfg.extra_deny_cidrs) == 2

    def test_invalid_cidr_rejected_at_load(self) -> None:
        with pytest.raises(ValidationError):
            SSRFConfig(extra_deny_cidrs=["not-a-cidr"])

    def test_ipv6_cidr_accepted(self) -> None:
        cfg = SSRFConfig(extra_deny_cidrs=["fc00::/7"])
        assert "fc00::/7" in cfg.extra_deny_cidrs
