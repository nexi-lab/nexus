"""Shared test helpers for nexus.bricks.auth CLI tests."""

from __future__ import annotations

from pathlib import Path

from nexus.bricks.auth.unified_service import FileSecretCredentialStore, UnifiedAuthService


def build_unified_service_for_tests(tmp_path: Path) -> UnifiedAuthService:
    """Return a fully isolated UnifiedAuthService backed by a temp credential file.

    Mirrors the ``_build_service`` helper in tests/unit/cli/test_auth_cli.py.
    Passes ``oauth_service=None`` so no real OAuth backend is needed — suitable
    for testing commands that exercise the secret/native store paths (list, etc.).
    Safe to monkeypatch at ``nexus.bricks.auth.cli_commands._build_auth_service``.
    """
    return UnifiedAuthService(
        oauth_service=None,
        secret_store=FileSecretCredentialStore(tmp_path / "credentials.json"),
    )
