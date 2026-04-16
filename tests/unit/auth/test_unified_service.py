from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus.bricks.auth.oauth.types import OAuthCredential
from nexus.bricks.auth.unified_service import FileSecretCredentialStore, UnifiedAuthService
from nexus.contracts.unified_auth import AuthStatus, CredentialKind


async def _fake_probe_all_ok(  # noqa: ANN001
    targets, *, native=None, user_email=None, access_token=None, source=None
):
    return {
        target: {
            "target": target,
            "success": True,
            "source": source or "oauth",
            "message": f"{target} target is ready via stored OAuth.",
        }
        for target in targets
    }


async def _fake_probe_native_ok(  # noqa: ANN001
    targets, *, native=None, user_email=None, access_token=None, source=None
):
    return {
        target: {
            "target": target,
            "success": True,
            "source": source or "native:gws_cli",
            "message": f"{target} target is ready via local gws CLI.",
        }
        for target in targets
    }


def _fake_native_gws(*args, **kwargs):  # noqa: ANN001, ARG001, ANN002, ANN003
    # Phase 3 (#3740): replaces async _detect_google_workspace_cli_native.
    # Signature accepts any args/kwargs so the same stub serves both the
    # zero-arg _gws_native_from_profile_store and the service/user_email
    # wrapper _oauth_native_from_profile_store.
    return {
        "source": "native:gws_cli",
        "email": "alice@example.com",
        "message": "Local gws CLI profile available for alice@example.com.",
    }


class _FakeOAuthService:
    def __init__(self) -> None:
        self._credentials = [
            {
                "provider": "google",
                "user_email": "alice@example.com",
                "is_expired": False,
            }
        ]

    async def list_credentials(self, context=None):  # noqa: ANN001, ARG002
        return list(self._credentials)

    async def test_credential(self, provider: str, user_email: str, context=None):  # noqa: ANN001, ARG002
        return {
            "success": provider == "google" and user_email == "alice@example.com",
            "provider": provider,
            "user_email": user_email,
            "message": "OAuth credential is valid.",
        }


@pytest.fixture
def secret_store(tmp_path: Path) -> FileSecretCredentialStore:
    return FileSecretCredentialStore(tmp_path / "credentials.json")


@pytest.fixture
def auth_service(secret_store: FileSecretCredentialStore) -> UnifiedAuthService:
    return UnifiedAuthService(oauth_service=_FakeOAuthService(), secret_store=secret_store)


def test_connect_secret_persists_record(auth_service: UnifiedAuthService) -> None:
    record = auth_service.connect_secret(
        "s3",
        {
            "access_key_id": "AKIA...",
            "secret_access_key": "secret",
            "region_name": "us-east-1",
        },
    )

    assert record.kind == CredentialKind.SECRET
    assert record.data["access_key_id"] == "AKIA..."


def test_resolve_backend_config_uses_stored_secret(auth_service: UnifiedAuthService) -> None:
    auth_service.connect_secret(
        "s3",
        {
            "access_key_id": "AKIA...",
            "secret_access_key": "secret",
        },
    )

    resolution = auth_service.resolve_backend_config("path_s3", {"bucket": "demo"})

    assert resolution.status == AuthStatus.AUTHED
    assert resolution.source == "stored:secret"
    assert resolution.resolved_config["access_key_id"] == "AKIA..."


def test_list_summaries_includes_oauth_and_secret(
    auth_service: UnifiedAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def _fake_get_stored_oauth_credential(provider: str, user_email: str):  # noqa: ANN001
        assert provider == "google"
        assert user_email == "alice@example.com"
        return OAuthCredential(
            access_token="ya29.test",
            refresh_token="1//refresh",
            scopes=(
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/documents",
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/chat.spaces.readonly",
            ),
            provider="google",
            user_email="alice@example.com",
        )

    monkeypatch.setattr(
        auth_service, "_get_stored_oauth_credential", _fake_get_stored_oauth_credential
    )
    monkeypatch.setattr(auth_service, "_probe_google_workspace_targets", _fake_probe_all_ok)

    auth_service.connect_secret(
        "gcs",
        {
            "credentials_path": "/tmp/gcs.json",
        },
    )

    summaries = asyncio.run(auth_service.list_summaries())

    summary_by_service = {summary.service: summary for summary in summaries}
    assert summary_by_service["gcs"].status == AuthStatus.AUTHED
    assert summary_by_service["google-drive"].status == AuthStatus.AUTHED
    assert summary_by_service["gws"].status == AuthStatus.AUTHED


def test_test_service_reports_missing_secret(
    auth_service: UnifiedAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setattr(auth_service, "_detect_native", lambda service: None)
    result = asyncio.run(auth_service.test_service("s3"))

    assert result["success"] is False
    assert "nexus auth connect s3" in result["message"]


def test_file_store_delete(secret_store: FileSecretCredentialStore) -> None:
    secret_store.upsert("s3", CredentialKind.NATIVE, {})

    assert secret_store.delete("s3") is True
    assert secret_store.get("s3") is None


def test_test_service_supports_gws_alias(
    auth_service: UnifiedAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def _fake_get_stored_oauth_credential(provider: str, user_email: str):  # noqa: ANN001
        assert provider == "google"
        assert user_email == "alice@example.com"
        return OAuthCredential(
            access_token="ya29.test",
            refresh_token="1//refresh",
            scopes=(
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/documents",
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/chat.spaces.readonly",
            ),
            provider="google",
            user_email="alice@example.com",
        )

    monkeypatch.setattr(
        auth_service, "_get_stored_oauth_credential", _fake_get_stored_oauth_credential
    )
    monkeypatch.setattr(
        auth_service,
        "_probe_google_workspace_targets",
        _fake_probe_all_ok,
    )

    result = asyncio.run(auth_service.test_service("gws", user_email="alice@example.com"))

    assert result["success"] is True
    assert result["service"] == "gws"
    assert result["source"] == "oauth"
    assert "targets ready" in result["message"].lower()


def test_list_summaries_prefers_native_gws_when_stored_oauth_expired(
    secret_store: FileSecretCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    oauth = _FakeOAuthService()
    oauth._credentials = [
        {
            "provider": "google",
            "user_email": "alice@example.com",
            "is_expired": True,
        }
    ]
    service = UnifiedAuthService(oauth_service=oauth, secret_store=secret_store)
    monkeypatch.setattr(service, "_gws_native_for_email", _fake_native_gws)
    monkeypatch.setattr(service, "_probe_google_workspace_targets", _fake_probe_native_ok)

    summaries = asyncio.run(service.list_summaries())
    summary_by_service = {summary.service: summary for summary in summaries}

    assert summary_by_service["gws"].status == AuthStatus.AUTHED
    assert summary_by_service["gws"].kind == CredentialKind.NATIVE
    assert summary_by_service["gws"].source == "native:gws_cli"
    assert summary_by_service["gws"].details["stored_oauth_status"] == AuthStatus.EXPIRED.value


def test_test_service_prefers_native_gws_when_stored_oauth_expired(
    secret_store: FileSecretCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    oauth = _FakeOAuthService()
    oauth._credentials = [
        {
            "provider": "google",
            "user_email": "alice@example.com",
            "is_expired": True,
        }
    ]
    service = UnifiedAuthService(oauth_service=oauth, secret_store=secret_store)
    monkeypatch.setattr(service, "_gws_native_for_email", _fake_native_gws)
    monkeypatch.setattr(service, "_probe_google_workspace_targets", _fake_probe_native_ok)

    result = asyncio.run(service.test_service("gws", user_email="alice@example.com"))

    assert result["success"] is True
    assert result["source"] == "native:gws_cli"
    assert "expired" in result["message"].lower()


def test_test_service_gws_reports_target_failures(
    secret_store: FileSecretCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    oauth = _FakeOAuthService()
    oauth._credentials = [
        {
            "provider": "google",
            "user_email": "alice@example.com",
            "is_expired": True,
        }
    ]
    service = UnifiedAuthService(oauth_service=oauth, secret_store=secret_store)
    monkeypatch.setattr(service, "_gws_native_for_email", _fake_native_gws)

    async def _fake_probe_chat_fails(
        targets, *, native=None, user_email=None, access_token=None, source=None
    ):  # noqa: ANN001, ARG001
        return {
            target: {
                "target": target,
                "success": target != "chat",
                "source": source or "native:gws_cli",
                "message": f"{target} ok" if target != "chat" else "chat scopes missing",
                "reason": None if target != "chat" else "missing_scopes",
            }
            for target in targets
        }

    monkeypatch.setattr(service, "_probe_google_workspace_targets", _fake_probe_chat_fails)

    result = asyncio.run(service.test_service("gws", user_email="alice@example.com"))

    assert result["success"] is False
    assert "chat" in result["message"]
    assert any(check["target"] == "chat" and not check["success"] for check in result["checks"])


def test_list_summaries_marks_gws_error_when_some_targets_fail(
    secret_store: FileSecretCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    oauth = _FakeOAuthService()
    oauth._credentials = [
        {
            "provider": "google",
            "user_email": "alice@example.com",
            "is_expired": True,
        }
    ]
    service = UnifiedAuthService(oauth_service=oauth, secret_store=secret_store)
    monkeypatch.setattr(service, "_gws_native_for_email", _fake_native_gws)

    async def _fake_probe_chat_scopes(
        targets, *, native=None, user_email=None, access_token=None, source=None
    ):  # noqa: ANN001, ARG001
        return {
            target: {
                "target": target,
                "success": target in {"drive", "docs", "sheets", "gmail", "calendar"},
                "source": source or "native:gws_cli",
                "message": f"{target} ok"
                if target != "chat"
                else "chat requires additional Google OAuth scopes",
                "reason": None if target != "chat" else "missing_scopes",
            }
            for target in targets
        }

    monkeypatch.setattr(service, "_probe_google_workspace_targets", _fake_probe_chat_scopes)

    summaries = asyncio.run(service.list_summaries())
    summary_by_service = {summary.service: summary for summary in summaries}

    assert summary_by_service["gws"].status == AuthStatus.ERROR
    assert "chat" in summary_by_service["gws"].message
    assert summary_by_service["google-drive"].status == AuthStatus.AUTHED


def test_test_service_gws_target_reports_missing_stored_scope(
    secret_store: FileSecretCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    oauth = _FakeOAuthService()
    service = UnifiedAuthService(oauth_service=oauth, secret_store=secret_store)

    async def _fake_get_stored_oauth_credential(provider: str, user_email: str):  # noqa: ANN001
        assert provider == "google"
        assert user_email == "alice@example.com"
        return OAuthCredential(
            access_token="ya29.test",
            refresh_token="1//refresh",
            scopes=(
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/drive.file",
            ),
            provider="google",
            user_email="alice@example.com",
        )

    monkeypatch.setattr(service, "_get_stored_oauth_credential", _fake_get_stored_oauth_credential)

    result = asyncio.run(service.test_service("gws", user_email="alice@example.com", target="chat"))

    assert result["success"] is False
    assert result["source"] == "oauth"
    assert "missing required google oauth scope" in result["message"].lower()
    assert result["checks"][0]["target"] == "chat"
    assert result["checks"][0]["reason"] == "missing_scopes"


def test_native_marker_requires_live_provider_chain_for_summary_and_resolution(
    secret_store: FileSecretCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    service = UnifiedAuthService(oauth_service=_FakeOAuthService(), secret_store=secret_store)
    service.connect_native("s3")
    monkeypatch.setattr(service, "_detect_native", lambda service_name: None)

    summaries = asyncio.run(service.list_summaries())
    summary_by_service = {summary.service: summary for summary in summaries}
    resolution = service.resolve_backend_config("path_s3", {"bucket": "demo"})

    assert summary_by_service["s3"].status == AuthStatus.NO_AUTH
    assert summary_by_service["s3"].source == "missing"
    assert resolution.status == AuthStatus.NO_AUTH
    assert resolution.source == "missing"


# ---------------------------------------------------------------------------
# Direct tests for _probe_google_workspace_targets (async parallel probes)
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Simulates asyncio.subprocess.Process for testing."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _make_subprocess_mock(process_map: dict[str, _FakeProcess]):
    """Return an async mock for asyncio.create_subprocess_exec.

    ``process_map`` maps the first arg (program name + subcommand) to a _FakeProcess.
    We identify each probe by matching the target-specific arguments from _GWS_TARGET_PROBES.
    """
    from nexus.bricks.auth.unified_service import _GWS_TARGET_PROBES

    _probe_to_target = {tuple(args): target for target, args in _GWS_TARGET_PROBES.items()}

    async def _mock_create(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        key = tuple(args)
        target = _probe_to_target.get(key)
        if target and target in process_map:
            return process_map[target]
        return _FakeProcess(returncode=1, stderr="unknown command")

    return _mock_create


@pytest.fixture
def probe_service(tmp_path: Path) -> UnifiedAuthService:
    store = FileSecretCredentialStore(tmp_path / "credentials.json")
    return UnifiedAuthService(oauth_service=_FakeOAuthService(), secret_store=store)


_NATIVE = {
    "source": "native:gws_cli",
    "email": "alice@example.com",
    "message": "Local gws CLI profile available for alice@example.com.",
}
_TARGETS = ("drive", "docs", "sheets", "gmail", "calendar", "chat")


def test_probe_all_targets_succeed(
    probe_service: UnifiedAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All 6 probes succeed → every target marked success."""
    import asyncio

    processes = {t: _FakeProcess(returncode=0, stdout="{}") for t in _TARGETS}
    monkeypatch.setattr(
        "nexus.bricks.auth.unified_service.asyncio.create_subprocess_exec",
        _make_subprocess_mock(processes),
    )

    checks = asyncio.run(
        probe_service._probe_google_workspace_targets(
            _TARGETS, native=_NATIVE, user_email="alice@example.com"
        )
    )

    assert len(checks) == 6
    for target in _TARGETS:
        assert checks[target]["success"] is True
        assert checks[target]["source"] == "native:gws_cli"


def test_probe_failure_classification(
    probe_service: UnifiedAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probes that fail are classified by error type: missing_scopes, expired, probe_failed."""
    import asyncio

    processes = {
        "drive": _FakeProcess(returncode=0, stdout="{}"),
        "docs": _FakeProcess(returncode=1, stderr="Insufficient Authentication Scopes for request"),
        "sheets": _FakeProcess(returncode=1, stderr="auth_expired: token is no longer valid"),
        "gmail": _FakeProcess(returncode=1, stderr="some other error occurred"),
        "calendar": _FakeProcess(returncode=0, stdout="{}"),
        "chat": _FakeProcess(returncode=0, stdout="{}"),
    }
    monkeypatch.setattr(
        "nexus.bricks.auth.unified_service.asyncio.create_subprocess_exec",
        _make_subprocess_mock(processes),
    )

    checks = asyncio.run(
        probe_service._probe_google_workspace_targets(
            _TARGETS, native=_NATIVE, user_email="alice@example.com"
        )
    )

    assert checks["drive"]["success"] is True
    assert checks["docs"]["success"] is False
    assert checks["docs"]["reason"] == "missing_scopes"
    assert checks["sheets"]["success"] is False
    assert checks["sheets"]["reason"] == "expired"
    assert checks["gmail"]["success"] is False
    assert checks["gmail"]["reason"] == "probe_failed"
    assert checks["calendar"]["success"] is True
    assert checks["chat"]["success"] is True


def test_probe_timeout_does_not_block_others(
    probe_service: UnifiedAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single probe timing out doesn't prevent other probes from returning."""

    class _TimeoutProcess:
        """Simulates a process whose communicate() raises TimeoutError."""

        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            raise TimeoutError()  # real asyncio.wait_for raises empty TimeoutError

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return -9

    processes: dict[str, _FakeProcess] = {}
    for t in _TARGETS:
        if t == "chat":
            continue
        processes[t] = _FakeProcess(returncode=0, stdout="{}")

    from nexus.bricks.auth.unified_service import _GWS_TARGET_PROBES

    _probe_to_target = {tuple(args): target for target, args in _GWS_TARGET_PROBES.items()}

    async def _mock_create(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        target = _probe_to_target.get(tuple(args))
        if target == "chat":
            return _TimeoutProcess()
        return processes.get(target, _FakeProcess(returncode=1, stderr="unknown"))

    monkeypatch.setattr(
        "nexus.bricks.auth.unified_service.asyncio.create_subprocess_exec",
        _mock_create,
    )

    checks = asyncio.run(
        probe_service._probe_google_workspace_targets(
            _TARGETS, native=_NATIVE, user_email="alice@example.com"
        )
    )

    assert len(checks) == 6
    for t in ("drive", "docs", "sheets", "gmail", "calendar"):
        assert checks[t]["success"] is True
    assert checks["chat"]["success"] is False
    assert checks["chat"]["reason"] == "probe_error"
    assert checks["chat"]["message"]  # must not be blank even with empty TimeoutError


def test_probe_mixed_results(
    probe_service: UnifiedAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mix of success and failure returns correct per-target results with access_token path."""
    import asyncio

    processes = {
        "drive": _FakeProcess(returncode=0, stdout="{}"),
        "docs": _FakeProcess(returncode=0, stdout="{}"),
        "sheets": _FakeProcess(returncode=1, stderr="permission denied"),
        "gmail": _FakeProcess(returncode=0, stdout="{}"),
        "calendar": _FakeProcess(returncode=1, stderr="Expired token"),
        "chat": _FakeProcess(returncode=0, stdout="{}"),
    }
    monkeypatch.setattr(
        "nexus.bricks.auth.unified_service.asyncio.create_subprocess_exec",
        _make_subprocess_mock(processes),
    )

    checks = asyncio.run(
        probe_service._probe_google_workspace_targets(
            _TARGETS, access_token="ya29.test", user_email="alice@example.com"
        )
    )

    assert checks["drive"]["success"] is True
    assert "stored OAuth" in checks["drive"]["message"]
    assert checks["sheets"]["success"] is False
    assert checks["sheets"]["reason"] == "probe_failed"
    assert checks["calendar"]["success"] is False
    assert checks["calendar"]["reason"] == "expired"


def test_probe_early_return_no_auth(probe_service: UnifiedAuthService) -> None:
    """When no access_token and no native, all targets return missing_google_auth."""
    import asyncio

    checks = asyncio.run(
        probe_service._probe_google_workspace_targets(_TARGETS, native=None, access_token=None)
    )

    assert len(checks) == 6
    for target in _TARGETS:
        assert checks[target]["success"] is False
        assert checks[target]["reason"] == "missing_google_auth"
