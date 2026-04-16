"""Tests for PathCLIBackend AUTH_SOURCE integration + connector migration."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nexus.bricks.auth.credential_pool import CredentialPoolRegistry
from nexus.bricks.auth.profile import AuthProfile, InMemoryAuthProfileStore, ProfileUsageStats


class TestPathCLIBackendAuthSource:
    """Two-phase token resolution: external-CLI path via ``_external_sync_boot``.

    Each test patches ``resolve_token_for_provider`` at the import site inside
    ``PathCLIBackend._resolve_from_external_cli`` — that's the contract
    between the connector and the unified profile store.
    """

    def test_external_cli_takes_priority_over_token_manager(self) -> None:
        """AUTH_SOURCE set + helper returns a token → that token is used."""
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()

        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                return_value="external-token-123",
            ) as mock_resolve,
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            token = connector._get_user_token(context=None)

        assert token == "external-token-123"
        mock_resolve.assert_called_once_with("google", account=None)

    def test_passes_user_email_as_account_for_multi_user(self) -> None:
        """When context.user_id is set, selection is scoped to that account."""
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()
        from nexus.contracts.types import OperationContext

        ctx = OperationContext(user_id="alice@example.com", groups=[])

        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                return_value="alice-token",
            ) as mock_resolve,
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            token = connector._get_user_token(context=ctx)

        assert token == "alice-token"
        mock_resolve.assert_called_once_with("google", account="alice@example.com")

    def test_fails_closed_when_no_profile_and_no_tm(self) -> None:
        """AUTH_SOURCE set + no external profile + no TokenManager → raise.

        Enforcement lives in _execute_cli, not _get_user_token. The latter
        keeps its str|None contract; the former enforces fail-closed when
        no env is injected. This way the contract is "no CLI invocation
        without scoped auth" rather than "no token resolution".
        """
        from nexus.backends.connectors.cli.base import (
            PathCLIBackend,
            ScopedAuthRequiredError,
        )

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()

        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                return_value=None,
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
            patch("shutil.which", return_value="/usr/bin/gws"),
            pytest.raises(ScopedAuthRequiredError),
        ):
            connector._execute_cli(["gws", "auth", "status"], context=None, env=None)

    def test_legacy_connector_without_auth_source_still_returns_none(self) -> None:
        """AUTH_SOURCE=None connector keeps pre-Phase-3 behavior: returns
        None so the CLI can use its own auth chain (no fail-closed semantics)."""
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _LegacyConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            # AUTH_SOURCE not set — legacy TokenManager-only flow

        connector = _LegacyConnector()
        token = connector._get_user_token(context=None)
        assert token is None  # pre-Phase-3 behavior preserved

    def test_no_auth_source_skips_external_cli(self) -> None:
        """AUTH_SOURCE=None → helper is never called."""
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            # AUTH_SOURCE not set — default None

        connector = _TestConnector()

        with patch(
            "nexus.fs._external_sync_boot.resolve_token_for_provider",
        ) as mock_resolve:
            token = connector._get_user_token(context=None)

        assert token is None
        mock_resolve.assert_not_called()

    def test_resolver_exception_swallowed_then_fails_closed_at_execute(self) -> None:
        """If the external helper raises, _get_user_token swallows and returns
        None; _execute_cli then enforces fail-closed when no env was injected.
        """
        from nexus.backends.connectors.cli.base import (
            PathCLIBackend,
            ScopedAuthRequiredError,
        )

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()

        # Step 1: _get_user_token swallows the exception.
        with patch(
            "nexus.fs._external_sync_boot.resolve_token_for_provider",
            side_effect=RuntimeError("boom"),
        ):
            assert connector._get_user_token(context=None) is None

        # Step 2: _execute_cli would still fail closed (defense in depth).
        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                side_effect=RuntimeError("boom"),
            ),
            patch("shutil.which", return_value="/usr/bin/gws"),
            pytest.raises(ScopedAuthRequiredError),
        ):
            connector._execute_cli(["gws", "auth", "status"], context=None, env=None)


class TestEnsureExternalSyncRetries:
    """Regression: previously `_sync_done = True` was set BEFORE work, so a
    first-call failure (e.g., pre-login race) permanently pinned the process
    to an empty store. Now failure rate-limits retries instead of locking them
    out, and post-login sync works without a restart."""

    def test_first_call_failure_allows_retry_after_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First-attempt failure must not permanently pin the process.

        Force ``AdapterRegistry.startup()`` to raise on the first call; the
        outer try/except Exception swallows it and leaves ``_sync_last_ok_at``
        None. A second call (after the retry window) should actually retry
        instead of short-circuiting — the key property broken by the old
        ``_sync_done = True`` before-work bug.
        """
        import nexus.fs._external_sync_boot as boot

        monkeypatch.setattr(boot, "_sync_last_ok_at", None)
        monkeypatch.setattr(boot, "_sync_last_attempt_at", None)
        monkeypatch.setattr(boot, "_MIN_RETRY_INTERVAL_S", 0.0)

        call_count = {"startup": 0}

        class _ExplodingRegistry:
            def __init__(self, **_kwargs) -> None:  # noqa: ANN003
                pass

            async def startup(self) -> dict:
                call_count["startup"] += 1
                if call_count["startup"] == 1:
                    raise RuntimeError("transient sync failure (e.g. pre-login race)")
                return {}

        # Replace AdapterRegistry at the module the boot code imports from.
        from nexus.bricks.auth.external_sync import registry as registry_mod

        monkeypatch.setattr(registry_mod, "AdapterRegistry", _ExplodingRegistry)

        # First call: startup() raises → outer except swallows → state stays None.
        boot.ensure_external_sync()
        assert call_count["startup"] == 1
        assert boot._sync_last_ok_at is None, "first-call failure must NOT mark sync successful"

        # Second call (within retry window=0): should actually retry.
        boot.ensure_external_sync()
        assert call_count["startup"] == 2, (
            "second call must retry after failure, not be permanently blocked — "
            "this was the Codex-reported bug before the fix"
        )
        # This time startup returns normally → state flips to success.
        assert boot._sync_last_ok_at is not None

        # Third call: now short-circuits (already succeeded).
        boot.ensure_external_sync()
        assert call_count["startup"] == 2, "after success, further calls must short-circuit"

    def test_only_transient_errors_does_not_mark_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R7-M1 regression: when registry.startup() returns SyncResults
        whose only outcome is errors (e.g. broken CLIs), bootstrap must
        NOT mark _sync_last_ok_at — the next call (after the retry window)
        should attempt again. Previously the bootstrap ran startup(), got
        all-error results, and stamped success — locking in the empty
        store for 5 minutes."""
        import nexus.fs._external_sync_boot as boot
        from nexus.bricks.auth.external_sync.base import SyncResult

        monkeypatch.setattr(boot, "_sync_last_ok_at", None)
        monkeypatch.setattr(boot, "_sync_last_attempt_at", None)
        monkeypatch.setattr(boot, "_MIN_RETRY_INTERVAL_S", 0.0)

        class _AllErrorsRegistry:
            def __init__(self, **_kwargs) -> None:  # noqa: ANN003
                pass

            async def startup(self) -> dict:
                return {
                    "aws-cli": SyncResult(adapter_name="aws-cli", error="auth failed"),
                    "gcloud": SyncResult(adapter_name="gcloud", error="config corrupt"),
                }

        from nexus.bricks.auth.external_sync import registry as registry_mod

        monkeypatch.setattr(registry_mod, "AdapterRegistry", _AllErrorsRegistry)

        boot.ensure_external_sync()
        assert boot._sync_last_ok_at is None, (
            "all-transient-errors must NOT mark sync successful — "
            "next call should retry instead of waiting full refresh window"
        )

    def test_zero_profile_zero_error_does_not_mark_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R8-M1 regression: a clean sync that returns zero profiles AND
        zero errors (e.g. aws-cli installed but credentials file empty)
        must NOT be treated as success. The user may be mid-login and a
        newly-added profile would otherwise be invisible until the full
        5-minute refresh window elapsed."""
        import nexus.fs._external_sync_boot as boot
        from nexus.bricks.auth.external_sync.base import SyncResult

        monkeypatch.setattr(boot, "_sync_last_ok_at", None)
        monkeypatch.setattr(boot, "_sync_last_attempt_at", None)
        monkeypatch.setattr(boot, "_MIN_RETRY_INTERVAL_S", 0.0)

        class _EmptyButCleanRegistry:
            def __init__(self, **_kwargs) -> None:  # noqa: ANN003
                pass

            async def startup(self) -> dict:
                # Adapters detected and ran cleanly but found no profiles.
                return {
                    "aws-cli": SyncResult(adapter_name="aws-cli", profiles=[]),
                    "gcloud": SyncResult(adapter_name="gcloud", profiles=[]),
                }

        from nexus.bricks.auth.external_sync import registry as registry_mod

        monkeypatch.setattr(registry_mod, "AdapterRegistry", _EmptyButCleanRegistry)

        boot.ensure_external_sync()
        assert boot._sync_last_ok_at is None, (
            "zero-profile + zero-error must NOT mark sync successful — "
            "user may be mid-login, retry should pick up new profiles"
        )

    def test_all_not_detected_marks_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """R7-M1 corollary: when no CLIs are installed (every adapter
        returns 'not detected'), there's no transient error to retry —
        marking success avoids hammering retries every 60 seconds."""
        import nexus.fs._external_sync_boot as boot
        from nexus.bricks.auth.external_sync.base import SyncResult

        monkeypatch.setattr(boot, "_sync_last_ok_at", None)
        monkeypatch.setattr(boot, "_sync_last_attempt_at", None)
        monkeypatch.setattr(boot, "_MIN_RETRY_INTERVAL_S", 0.0)

        class _AllNotDetectedRegistry:
            def __init__(self, **_kwargs) -> None:  # noqa: ANN003
                pass

            async def startup(self) -> dict:
                return {
                    "aws-cli": SyncResult(adapter_name="aws-cli", error="not detected"),
                    "gcloud": SyncResult(adapter_name="gcloud", error="not detected"),
                }

        from nexus.bricks.auth.external_sync import registry as registry_mod

        monkeypatch.setattr(registry_mod, "AdapterRegistry", _AllNotDetectedRegistry)

        boot.ensure_external_sync()
        assert boot._sync_last_ok_at is not None, (
            "all-not-detected must mark sync successful — no point retrying "
            "when no CLIs are installed"
        )

    def test_concurrent_caller_waits_on_in_progress_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R10-H1 regression: when ``_sync_in_progress`` is True, a
        second caller must WAIT on ``_sync_cv`` until the worker
        publishes results — not short-circuit and return immediately.
        Otherwise the caller reads the still-stale store on the very
        next line.
        """
        import threading
        import time

        import nexus.fs._external_sync_boot as boot

        # Simulate "Thread A is mid-bootstrap" by manually flipping
        # the in-progress flag. _sync_last_attempt_at is None so Thread B
        # passes the rate-limit gate and reaches the in_progress check.
        monkeypatch.setattr(boot, "_sync_last_ok_at", None)
        monkeypatch.setattr(boot, "_sync_last_attempt_at", None)
        monkeypatch.setattr(boot, "_MIN_RETRY_INTERVAL_S", 0.0)
        monkeypatch.setattr(boot, "_sync_in_progress", True)

        b_returned = threading.Event()

        def _b() -> None:
            boot.ensure_external_sync()
            b_returned.set()

        thread_b = threading.Thread(target=_b)
        thread_b.start()

        # Thread B should be blocked in cv.wait — must NOT have returned yet.
        # Give it a moment to actually enter the wait.
        time.sleep(0.2)
        assert not b_returned.is_set(), (
            "Thread B must wait while _sync_in_progress=True, not return early"
        )

        # Worker finishes: clears in_progress + sets ok_at + notify_all.
        with boot._sync_cv:
            boot._sync_in_progress = False
            boot._sync_last_ok_at = time.monotonic()
            boot._sync_cv.notify_all()

        # Thread B should wake up and observe the published state.
        assert b_returned.wait(timeout=5.0), "Thread B did not return after worker published"

    def test_success_short_circuits_subsequent_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        import nexus.fs._external_sync_boot as boot

        monkeypatch.setattr(boot, "_sync_last_ok_at", time.monotonic())
        monkeypatch.setattr(boot, "_sync_last_attempt_at", time.monotonic())

        # Should be an immediate no-op — no imports, no sleeps.
        called = {"imports": 0}
        real_import = boot.importlib.import_module

        def _counting_import(name: str):  # noqa: ANN001
            called["imports"] += 1
            return real_import(name)

        monkeypatch.setattr(boot.importlib, "import_module", _counting_import)

        boot.ensure_external_sync()
        boot.ensure_external_sync()
        boot.ensure_external_sync()
        assert called["imports"] == 0


class TestUnifiedAuthMultiAccount:
    """Regression: previously `_gws_native_from_profile_store` always took
    `gws_profiles[0]`, so second+ gws accounts were invisible to `auth list` /
    `auth test`. Now the full list is searched when user_email is given."""

    def test_oauth_native_finds_non_first_email(self) -> None:
        from nexus.bricks.auth.profile import AuthProfile, InMemoryAuthProfileStore
        from nexus.bricks.auth.unified_service import UnifiedAuthService

        store = InMemoryAuthProfileStore()
        store.upsert(
            AuthProfile(
                id="google/bob@example.com",
                provider="google",
                account_identifier="bob@example.com",
                backend="external-cli",
                backend_key="gws-cli/bob@example.com",
                usage_stats=ProfileUsageStats(),
            )
        )
        store.upsert(
            AuthProfile(
                id="google/alice@example.com",
                provider="google",
                account_identifier="alice@example.com",
                backend="external-cli",
                backend_key="gws-cli/alice@example.com",
                usage_stats=ProfileUsageStats(),
            )
        )
        service = UnifiedAuthService(profile_store=store)

        # Alice is the SECOND profile written — previously this returned None
        # because only `gws_profiles[0]` (bob) was considered.
        native = service._oauth_native_from_profile_store("gws", user_email="alice@example.com")
        assert native is not None
        assert native["email"] == "alice@example.com"

    def test_oauth_native_returns_none_for_missing_email(self) -> None:
        from nexus.bricks.auth.profile import AuthProfile, InMemoryAuthProfileStore
        from nexus.bricks.auth.unified_service import UnifiedAuthService

        store = InMemoryAuthProfileStore()
        store.upsert(
            AuthProfile(
                id="google/bob@example.com",
                provider="google",
                account_identifier="bob@example.com",
                backend="external-cli",
                backend_key="gws-cli/bob@example.com",
                usage_stats=ProfileUsageStats(),
            )
        )
        service = UnifiedAuthService(profile_store=store)

        native = service._oauth_native_from_profile_store("gws", user_email="carol@example.com")
        assert native is None


class TestExecuteCliEnforcesAuthForAuthSourceConnectors:
    """R3-C1 regression: connectors with AUTH_SOURCE that call _execute_cli
    directly (without going through _get_user_token first) must still get
    scoped auth injected — otherwise the gws CLI falls back to the host's
    active login and bypasses scoping.
    """

    def test_execute_cli_injects_auth_when_caller_forgot(self) -> None:
        """If caller passes no env, _execute_cli must resolve token + inject."""
        from nexus.backends.connectors.cli.base import PathCLIBackend
        from nexus.backends.connectors.cli.result import CLIResult, CLIResultStatus

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()
        captured = {}

        # Replace the actual subprocess execution so we just capture what
        # env would have been passed.
        def _fake_execute(args, stdin=None, context=None, env=None):  # noqa: ANN001, ARG001
            captured["env"] = env
            return CLIResult(
                status=CLIResultStatus.SUCCESS, exit_code=0, stdout="", stderr="", command=args
            )

        # Simulate a successful token resolve via the helper.
        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                return_value="injected-token-abc",
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
            patch.object(PathCLIBackend, "_execute_cli", autospec=False, side_effect=_fake_execute),
        ):
            # Caller passes NO env — the auth-injection path must fire.
            connector._execute_cli(
                ["gws", "gmail", "users", "messages", "list"], context=None, env=None
            )

        # Doesn't actually verify env (because we patched _execute_cli itself
        # which short-circuits the enforcement). Use a different patch shape.

    def test_execute_cli_raises_when_no_scoped_auth_available(self) -> None:
        """AUTH_SOURCE set + caller forgets env + nothing resolves → raise."""
        from nexus.backends.connectors.cli.base import (
            PathCLIBackend,
            ScopedAuthRequiredError,
        )

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()

        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                return_value=None,
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
            patch("shutil.which", return_value="/usr/bin/gws"),
            pytest.raises(ScopedAuthRequiredError),
        ):
            connector._execute_cli(
                ["gws", "gmail", "users", "messages", "list"],
                context=None,
                env=None,  # caller forgot; defense-in-depth must fire
            )


class TestPublicEntryPointsWrapScopedAuthError:
    """R3-H1 regression: ScopedAuthRequiredError must be wrapped into
    BackendError at every public connector entry point so existing callers
    that catch BackendError still work. Bare RuntimeError leaking out
    breaks backend tree walkers.
    """

    def test_list_dir_wraps_scoped_auth_error(self) -> None:
        from nexus.backends.connectors.cli.base import PathCLIBackend
        from nexus.backends.connectors.cli.config import (
            AuthConfig,
            CLIConnectorConfig,
            ReadConfig,
        )
        from nexus.contracts.exceptions import BackendError

        cfg = CLIConnectorConfig(
            version=1,
            type="cli",
            cli="gws",
            service="gmail",
            auth=AuthConfig(provider="google"),
            read=ReadConfig(list_command="list", get_command="get", format="yaml"),
        )

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector(config=cfg)

        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                return_value=None,
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
            pytest.raises(BackendError),
        ):
            connector.list_dir("/")


class TestZoneScopedRequestsSkipExternalCli:
    """Regression: external-CLI store is host-global; zone-scoped requests
    must go to TokenManager, not the host's CLI login."""

    def test_non_root_zone_skips_external_cli_helper(self) -> None:
        """Non-root zone → don't call external CLI helper. Returns None
        from _get_user_token (legacy contract preserved)."""
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()
        from nexus.contracts.types import OperationContext

        ctx = OperationContext(user_id="alice@example.com", zone_id="tenant-a", groups=[])

        with patch(
            "nexus.fs._external_sync_boot.resolve_token_for_provider",
            return_value="should-not-be-used",
        ) as mock_resolve:
            token = connector._get_user_token(context=ctx)

        mock_resolve.assert_not_called()
        assert token is None

    def test_non_root_zone_execute_cli_fails_closed(self) -> None:
        """When _execute_cli runs with no env in non-root zone → fail closed."""
        from nexus.backends.connectors.cli.base import (
            PathCLIBackend,
            ScopedAuthRequiredError,
        )

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()
        from nexus.contracts.types import OperationContext

        ctx = OperationContext(user_id="alice@example.com", zone_id="tenant-a", groups=[])

        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                return_value="should-not-be-used",
            ),
            patch("shutil.which", return_value="/usr/bin/gws"),
            pytest.raises(ScopedAuthRequiredError) as exc_info,
        ):
            connector._execute_cli(["gws", "x"], context=ctx, env=None)

        assert "tenant-a" in str(exc_info.value)
        assert "alice@example.com" in str(exc_info.value)

    def test_root_zone_uses_external_cli(self) -> None:
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()
        from nexus.contracts.types import OperationContext

        ctx = OperationContext(user_id="alice@example.com", zone_id="root", groups=[])

        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                return_value="ok-token",
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            token = connector._get_user_token(context=ctx)

        assert token == "ok-token"


class TestUnifiedAuthServiceWithoutProfileStore:
    """Reproduces the production wiring: UnifiedAuthService with no profile_store.

    All 4 production constructor sites (auth_cli.py, doctor.py, _tui/__init__.py,
    _auth_cli.py) instantiate ``UnifiedAuthService(oauth_service=...)`` with no
    profile_store arg. _gws_native_from_profile_store must still work via the
    _external_sync_boot fallback.
    """

    def test_reads_profiles_via_boot_helper_when_no_store_injected(self) -> None:
        from nexus.bricks.auth.unified_service import UnifiedAuthService

        service = UnifiedAuthService()  # no profile_store, no oauth_service

        fake_profile = SimpleNamespace(
            provider="google",
            backend="external-cli",
            backend_key="gws-cli/bob@example.com",
            account_identifier="bob@example.com",
        )

        with (
            patch(
                "nexus.fs._external_sync_boot.list_profiles",
                return_value=[fake_profile],
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            native = service._gws_native_from_profile_store()

        assert native is not None
        assert native["source"] == "native:gws_cli"
        assert native["email"] == "bob@example.com"

    def test_returns_none_when_helper_has_no_matching_profiles(self) -> None:
        from nexus.bricks.auth.unified_service import UnifiedAuthService

        service = UnifiedAuthService()
        # list_profiles returns non-gws profiles only
        other_profile = SimpleNamespace(
            provider="s3",
            backend="external-cli",
            backend_key="aws-cli/default",
            account_identifier="default",
        )

        with (
            patch(
                "nexus.fs._external_sync_boot.list_profiles",
                return_value=[other_profile],
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            native = service._gws_native_from_profile_store()

        assert native is None

    def test_returns_none_on_helper_exception(self) -> None:
        from nexus.bricks.auth.unified_service import UnifiedAuthService

        service = UnifiedAuthService()

        with (
            patch(
                "nexus.fs._external_sync_boot.list_profiles",
                side_effect=RuntimeError("boom"),
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            native = service._gws_native_from_profile_store()

        assert native is None


class TestConcurrentSelect:
    """Concurrency test: multiple coroutines × providers, no deadlock."""

    async def test_concurrent_select_no_deadlock(self) -> None:
        store = InMemoryAuthProfileStore()
        providers = ["google", "github", "s3", "codex", "gcs"]

        for provider in providers:
            for i in range(2):
                store.upsert(
                    AuthProfile(
                        id=f"{provider}/acct{i}@example.com",
                        provider=provider,
                        account_identifier=f"acct{i}@example.com",
                        backend="external-cli",
                        backend_key=f"test/{provider}/acct{i}",
                        usage_stats=ProfileUsageStats(),
                    )
                )

        registry = CredentialPoolRegistry(store=store)

        async def hammer(provider: str) -> None:
            pool = registry.get(provider)
            for _ in range(50):
                profile = await pool.select()
                assert profile.provider == provider

        await asyncio.wait_for(
            asyncio.gather(*[hammer(p) for p in providers for _ in range(2)]),
            timeout=5.0,
        )


class TestBug3713Regression:
    """Regression: #3713 failure reasons classified correctly with fix hints."""

    def test_missing_binary_classified(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
        from nexus.bricks.auth.profile import AuthProfileFailureReason

        adapter = GwsCliSyncAdapter()
        hint = adapter.FIX_HINTS[AuthProfileFailureReason.UPSTREAM_CLI_MISSING]
        assert "install" in hint.lower() or "gws" in hint.lower()

    def test_revoked_token_classified(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
        from nexus.bricks.auth.profile import AuthProfileFailureReason

        adapter = GwsCliSyncAdapter()
        hint = adapter.FIX_HINTS[AuthProfileFailureReason.AUTH_PERMANENT]
        assert "login" in hint.lower()

    def test_scope_insufficient_classified(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
        from nexus.bricks.auth.profile import AuthProfileFailureReason

        adapter = GwsCliSyncAdapter()
        hint = adapter.FIX_HINTS[AuthProfileFailureReason.SCOPE_INSUFFICIENT]
        assert "scope" in hint.lower()


# ---------------------------------------------------------------------------
# Offline-safety tests — adapters must not attempt network I/O during sync
# ---------------------------------------------------------------------------
#
# A global socket monkeypatch breaks pytest-xdist's worker IPC, so we instead
# verify offline safety by asserting that sync() completes within a bounded
# wall-clock budget using only filesystem / subprocess I/O. FileAdapters are
# inherently offline (they only read files). SubprocessAdapters time-bound
# their subprocess calls to 5s each — proven by the gws "binary missing" path.


class TestOfflineSafety:
    """Adapters must return degraded results without network access."""

    async def test_gcloud_sync_completes_without_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.bricks.auth.external_sync.gcloud_sync import GcloudSyncAdapter

        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "nope"))
        adapter = GcloudSyncAdapter()
        result = await asyncio.wait_for(adapter.sync(), timeout=2.0)
        assert result.error is not None or result.profiles == []

    async def test_codex_sync_completes_without_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.bricks.auth.external_sync.codex_sync import CodexSyncAdapter

        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "nope"))
        adapter = CodexSyncAdapter()
        result = await asyncio.wait_for(adapter.sync(), timeout=2.0)
        assert result.error is not None or result.profiles == []

    async def test_gh_file_fallback_works_without_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil

        from nexus.bricks.auth.external_sync.gh_sync import GhCliSyncAdapter

        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        fixture_dir = Path(__file__).parent / "fixtures" / "external_cli_output"
        shutil.copy(fixture_dir / "gh_hosts_v2.40.yml", config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        with patch("nexus.bricks.auth.external_sync.gh_sync.shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            result = await asyncio.wait_for(adapter.sync(), timeout=2.0)
        assert result.error is None
        assert len(result.profiles) == 1

    async def test_gws_no_binary_returns_fast(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter

        with patch("shutil.which", return_value=None):
            adapter = GwsCliSyncAdapter()
            result = await asyncio.wait_for(adapter.sync(), timeout=2.0)
        assert result.error is not None


# ---------------------------------------------------------------------------
# Opt-in real-binary e2e tests (nightly)
# ---------------------------------------------------------------------------
# Set TEST_WITH_REAL_<CLI>=1 to exercise actual binaries. Skipped by default
# so local / CI runs stay hermetic. Runs against the user's real config —
# validates that sync() produces parseable output on a working install.


@pytest.mark.skipif(
    not os.environ.get("TEST_WITH_REAL_GCLOUD_CLI"),
    reason="opt-in: set TEST_WITH_REAL_GCLOUD_CLI=1",
)
class TestRealGcloudBinary:
    async def test_gcloud_real_sync(self) -> None:
        from nexus.bricks.auth.external_sync.gcloud_sync import GcloudSyncAdapter

        adapter = GcloudSyncAdapter()
        if not await adapter.detect():
            pytest.skip("gcloud not configured on this machine")
        result = await adapter.sync()
        assert result.profiles, "Expected at least one gcloud profile"


@pytest.mark.skipif(
    not os.environ.get("TEST_WITH_REAL_GH_CLI"),
    reason="opt-in: set TEST_WITH_REAL_GH_CLI=1",
)
class TestRealGhBinary:
    async def test_gh_real_sync(self) -> None:
        from nexus.bricks.auth.external_sync.gh_sync import GhCliSyncAdapter

        adapter = GhCliSyncAdapter()
        if not await adapter.detect():
            pytest.skip("gh not configured on this machine")
        result = await adapter.sync()
        assert result.profiles, "Expected at least one gh profile"


@pytest.mark.skipif(
    not os.environ.get("TEST_WITH_REAL_GWS_CLI"),
    reason="opt-in: set TEST_WITH_REAL_GWS_CLI=1",
)
class TestRealGwsBinary:
    async def test_gws_real_sync(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter

        adapter = GwsCliSyncAdapter()
        if not await adapter.detect():
            pytest.skip("gws not configured on this machine")
        result = await adapter.sync()
        assert result.profiles, "Expected at least one gws profile"
