"""Rate limiting must remain opt-in for the default stack."""

from __future__ import annotations

from pathlib import Path

import yaml

from nexus.server.fastapi_server import _rate_limit_enabled_from_env


def test_rate_limiting_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv("NEXUS_RATE_LIMIT_ENABLED", raising=False)

    assert _rate_limit_enabled_from_env() is False


def test_rate_limiting_enabled_only_by_explicit_truthy_env(monkeypatch):
    monkeypatch.setenv("NEXUS_RATE_LIMIT_ENABLED", "true")

    assert _rate_limit_enabled_from_env() is True


def test_default_compose_stack_does_not_enable_rate_limiting():
    repo_root = Path(__file__).resolve().parents[3]
    stack = yaml.safe_load((repo_root / "nexus-stack.yml").read_text())

    value = stack["services"]["nexus"]["environment"]["NEXUS_RATE_LIMIT_ENABLED"]

    assert value == "${NEXUS_RATE_LIMIT_ENABLED:-false}"
