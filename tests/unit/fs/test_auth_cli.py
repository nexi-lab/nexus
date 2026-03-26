from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from nexus.bricks.auth.unified_service import FileSecretCredentialStore, UnifiedAuthService
from nexus.fs._auth_cli import auth


class _FakeOAuthService:
    async def list_credentials(self, context=None):  # noqa: ANN001, ARG002
        return []

    async def test_credential(self, provider: str, user_email: str, context=None):  # noqa: ANN001, ARG002
        return {
            "success": True,
            "provider": provider,
            "user_email": user_email,
            "message": "OAuth credential is valid.",
        }


class _StubAuthService:
    secret_store_path = Path("/tmp/test-auth-store.json")


def _build_service(tmp_path: Path) -> UnifiedAuthService:
    return UnifiedAuthService(
        oauth_service=_FakeOAuthService(),
        secret_store=FileSecretCredentialStore(tmp_path / "credentials.json"),
    )


def test_fs_auth_connect_s3_guides_and_stores_native(monkeypatch, tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    monkeypatch.setattr("nexus.fs._auth_cli._build_auth_service", lambda: service)

    runner = CliRunner()
    result = runner.invoke(auth, ["connect", "s3"], input="native\n")

    assert result.exit_code == 0
    assert "Choose auth mode for s3" in result.output
    assert "Setup steps for s3 (native)" in result.output
    assert "Next: nexus-fs auth test s3" in result.output


def test_fs_auth_connect_gws_uses_local_oauth_setup(monkeypatch) -> None:
    called: dict[str, str | None] = {}
    monkeypatch.setattr("nexus.fs._auth_cli._build_auth_service", lambda: _StubAuthService())

    def _fake_google_setup(
        *, user_email: str, client_id=None, client_secret=None, db_path=None, zone_id=None
    ):  # noqa: ANN001
        called.update(
            {
                "user_email": user_email,
                "client_id": client_id,
                "client_secret": client_secret,
                "db_path": db_path,
                "zone_id": zone_id,
            }
        )

    monkeypatch.setattr("nexus.fs._auth_cli.run_google_oauth_setup", _fake_google_setup)

    runner = CliRunner()
    result = runner.invoke(auth, ["connect", "gws"], input="alice@example.com\n")

    assert result.exit_code == 0
    assert "Setup steps for gws (oauth)" in result.output
    assert called["user_email"] == "alice@example.com"
