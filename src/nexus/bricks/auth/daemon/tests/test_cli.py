"""Lightweight tests for the `nexus daemon` CLI wiring (#3804).

Only covers things not already exercised by T18 integration tests:
the ``_build_encryption_provider`` fallback path. The individual
subcommands are smoke-tested via ``nexus daemon --help`` and covered
end-to-end by T18.
"""

from __future__ import annotations

import click
import pytest

from nexus.bricks.auth.daemon import cli as daemon_cli


def test_build_encryption_provider_defaults_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env → memory provider (the MVP default)."""
    monkeypatch.delenv("NEXUS_KMS_PROVIDER", raising=False)
    from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider

    ep = daemon_cli._build_encryption_provider()
    assert isinstance(ep, InMemoryEncryptionProvider)


def test_build_encryption_provider_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any non-'memory' value raises ClickException with the offending name."""
    monkeypatch.setenv("NEXUS_KMS_PROVIDER", "aws-kms")
    with pytest.raises(click.ClickException) as excinfo:
        daemon_cli._build_encryption_provider()
    assert "aws-kms" in str(excinfo.value.message)


def test_daemon_group_exposes_expected_subcommands() -> None:
    """All 5 MVP subcommands are registered on the group."""
    expected = {"join", "run", "status", "install", "uninstall"}
    registered = set(daemon_cli.daemon.commands.keys())
    assert expected <= registered, f"missing: {expected - registered}"
