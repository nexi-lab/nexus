from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from nexus.bricks.auth.unified_service import FileSecretCredentialStore, UnifiedAuthService
from nexus.fs._auth_cli import auth
from nexus.fs._oauth_support import get_fs_database_url, run_google_oauth_setup


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
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    runner = CliRunner()
    result = runner.invoke(auth, ["connect", "s3"], input="native\n")

    assert result.exit_code == 0
    assert "Choose auth mode for s3" in result.output
    assert "Setup steps for s3 (native)" in result.output
    assert "Next: nexus auth test s3" in result.output


def test_fs_auth_connect_gws_uses_local_oauth_setup(monkeypatch) -> None:
    called: dict[str, str | None] = {}
    monkeypatch.setattr(
        "nexus.bricks.auth.cli_commands._build_auth_service", lambda: _StubAuthService()
    )

    def _fake_google_setup(
        *,
        user_email: str,
        service_name="gws",
        client_id=None,
        client_secret=None,
        db_path=None,
        zone_id=None,
    ):  # noqa: ANN001
        called.update(
            {
                "user_email": user_email,
                "service_name": service_name,
                "client_id": client_id,
                "client_secret": client_secret,
                "db_path": db_path,
                "zone_id": zone_id,
            }
        )

    monkeypatch.setattr("nexus.fs._oauth_support.run_google_oauth_setup", _fake_google_setup)

    runner = CliRunner()
    result = runner.invoke(auth, ["connect", "gws"], input="alice@example.com\n")

    assert result.exit_code == 0
    assert "Setup steps for gws (oauth)" in result.output
    assert called["user_email"] == "alice@example.com"
    assert called["service_name"] == "gws"


def test_fs_database_url_does_not_inherit_global_nexus_database_url(monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://postgres:nexus@localhost:46702/nexus")
    monkeypatch.delenv("NEXUS_FS_DATABASE_URL", raising=False)
    assert get_fs_database_url() is None


def test_fs_auth_test_gws_prints_target_breakdown(monkeypatch) -> None:
    class _Service:
        async def test_service(self, service_name, user_email=None, target=None, context=None):  # noqa: ANN001, ARG002
            assert service_name == "gws"
            assert target is None
            return {
                "success": False,
                "service": "gws",
                "source": "native:gws_cli",
                "message": "chat: chat scopes missing",
                "checks": [
                    {
                        "target": "drive",
                        "success": True,
                        "source": "native:gws_cli",
                        "message": "drive target is ready via local gws CLI.",
                    },
                    {
                        "target": "chat",
                        "success": False,
                        "source": "native:gws_cli",
                        "message": "chat scopes missing",
                    },
                ],
            }

    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: _Service())
    monkeypatch.setenv("NEXUS_NO_AUTO_JSON", "1")

    runner = CliRunner()
    result = runner.invoke(auth, ["test", "gws"])

    assert result.exit_code != 0
    assert "gws target readiness" in result.output
    assert "Ready:" in result.output
    assert "Needs action:" in result.output
    assert "Next steps" in result.output
    assert "drive" in result.output
    assert "chat" in result.output


def test_fs_google_oauth_setup_stores_service_specific_provider(
    monkeypatch, tmp_path: Path
) -> None:
    calls: dict[str, str] = {}

    class _Provider:
        def __init__(self, **kwargs):  # noqa: ANN003
            calls["provider_name"] = str(kwargs["provider_name"])
            calls["scope_text"] = " ".join(kwargs["scopes"])

        def get_authorization_url(self) -> str:
            return "https://example.test/oauth"

        async def exchange_code(self, code: str):  # noqa: ANN001
            calls["auth_code"] = code
            return SimpleNamespace(
                access_token="ya29.test",
                refresh_token="1//refresh",
                token_type="Bearer",
                expires_at=None,
                scopes=None,
                client_id=None,
                token_uri=None,
            )

    class _Manager:
        async def store_credential(self, **kwargs):  # noqa: ANN003
            calls["stored_provider"] = str(kwargs["provider"])
            return "cred-123"

        def close(self) -> None:
            return None

    monkeypatch.setenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(
        "nexus.fs._oauth_support.get_token_manager", lambda db_path=None: _Manager()
    )
    monkeypatch.setattr(
        "nexus.fs._oauth_support._il.import_module",
        lambda name: (
            SimpleNamespace(GoogleOAuthProvider=_Provider)
            if name == "nexus.bricks.auth.oauth.providers.google"
            else None
        ),
    )

    monkeypatch.setattr("click.prompt", lambda *args, **kwargs: "code-123")
    run_google_oauth_setup(user_email="alice@example.com", service_name="google-drive")

    assert calls["provider_name"] == "google-drive"
    assert calls["stored_provider"] == "google-drive"
