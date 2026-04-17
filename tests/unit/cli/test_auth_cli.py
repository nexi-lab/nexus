from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from nexus.bricks.auth.unified_service import FileSecretCredentialStore, UnifiedAuthService
from nexus.cli.commands.auth_cli import auth


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


def test_connect_s3_guides_and_stores_native(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = _build_service(tmp_path)
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    runner = CliRunner()
    result = runner.invoke(auth, ["connect", "s3"], input="native\n")

    assert result.exit_code == 0
    assert "Choose auth mode for s3" in result.output
    assert "Setup steps for s3 (native)" in result.output
    assert "Next: nexus auth test s3" in result.output

    stored = service._secret_store.get("s3")  # noqa: SLF001
    assert stored is not None
    assert stored.kind.value == "native"


def test_connect_gcs_secret_prompts_for_credentials_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = _build_service(tmp_path)
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    runner = CliRunner()
    result = runner.invoke(
        auth,
        ["connect", "gcs"],
        input="secret\n/tmp/service-account.json\n",
    )

    assert result.exit_code == 0
    assert "Choose auth mode for gcs" in result.output
    assert "Setup steps for gcs (secret)" in result.output
    assert "credentials_path" in result.output

    stored = service._secret_store.get("gcs")  # noqa: SLF001
    assert stored is not None
    assert stored.data["credentials_path"] == "/tmp/service-account.json"


def test_connect_gws_prompts_for_user_email_and_runs_google_setup(
    monkeypatch,
) -> None:
    called: dict[str, str | None] = {}
    monkeypatch.setattr(
        "nexus.bricks.auth.cli_commands._build_auth_service",
        lambda: _StubAuthService(),
    )
    monkeypatch.setenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET", "client-secret")

    def _fake_setup(*, user_email: str, service_name: str = "gws", **kwargs: object) -> None:
        called.update(
            {
                "service_name": service_name,
                "user_email": user_email,
            }
        )

    monkeypatch.setattr("nexus.fs._oauth_support.run_google_oauth_setup", _fake_setup)

    runner = CliRunner()
    result = runner.invoke(auth, ["connect", "gws"], input="alice@example.com\n")

    assert result.exit_code == 0
    assert "Setup steps for gws (oauth)" in result.output
    assert called["user_email"] == "alice@example.com"
    assert called["service_name"] == "gws"


def test_test_auth_reports_actionable_guidance_for_missing_gws_oauth(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = _build_service(tmp_path)
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    runner = CliRunner()
    result = runner.invoke(auth, ["test", "gws"])

    assert result.exit_code != 0
    assert "Run `nexus auth connect gws oauth`." in result.output


def test_doctor_shows_auth_statuses(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = _build_service(tmp_path)
    service.connect_secret("s3", {"access_key_id": "AKIA_TEST", "secret_access_key": "secret"})
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)
    monkeypatch.setattr(service, "_detect_native", lambda service_name: None)

    runner = CliRunner()
    result = runner.invoke(auth, ["doctor"])

    assert result.exit_code != 0
    assert "s3" in result.output
    assert "authed" in result.output
    assert "gcs" in result.output
    assert "One or more services need auth setup." in result.output
