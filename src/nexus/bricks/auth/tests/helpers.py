"""Isolated credential stores for authentication service tests."""

from __future__ import annotations

from pathlib import Path

from nexus.bricks.auth.unified_service import FileSecretCredentialStore, UnifiedAuthService


def build_unified_service_for_tests(tmp_path: Path) -> UnifiedAuthService:
    """Return a fully isolated UnifiedAuthService backed by a temp credential file.

    Passes ``oauth_service=None`` so no real OAuth backend is needed.
    """
    return UnifiedAuthService(
        oauth_service=None,
        secret_store=FileSecretCredentialStore(tmp_path / "credentials.json"),
    )
