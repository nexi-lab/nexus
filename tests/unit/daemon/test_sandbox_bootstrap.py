"""Tests for SandboxBootstrapper (Issue #3786).

Tests cover:
1. Successful boot with hub → local zone mounted, remote zones mounted via
   nexus_fs.sys_setattr with backend_type="remote", search_registry registered,
   BootIndexer started.
2. Hub handshake fails (HandshakeAuthError / HandshakeConnectionError) → local-only
   boot, no crash, local zone still mounted.
3. hub_url=None → skip handshake entirely, local-only boot.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.exceptions import HandshakeAuthError, HandshakeConnectionError
from nexus.contracts.metadata import DT_MOUNT
from nexus.daemon.sandbox_bootstrap import SandboxBootstrapper

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bootstrapper(
    tmp_path: Path,
    hub_url: str | None = "grpc://hub.example.com",
    hub_token: str | None = "tok-abc",
) -> tuple[SandboxBootstrapper, MagicMock, MagicMock, MagicMock]:
    """Create a SandboxBootstrapper with fresh mock dependencies."""
    nexus_fs = MagicMock()
    search_registry = MagicMock()
    search_daemon = MagicMock()
    health_state: dict[str, str] = {"status": "indexing"}

    sb = SandboxBootstrapper(
        workspace=tmp_path,
        hub_url=hub_url,
        hub_token=hub_token,
        nexus_fs=nexus_fs,
        search_registry=search_registry,
        search_daemon=search_daemon,
        health_state=health_state,
    )
    return sb, nexus_fs, search_registry, search_daemon


def _mounted_paths(nexus_fs: MagicMock) -> list[str]:
    """Return all paths passed to nexus_fs.sys_setattr."""
    return [c.args[0] for c in nexus_fs.sys_setattr.call_args_list]


# ---------------------------------------------------------------------------
# 1. Successful boot with hub
# ---------------------------------------------------------------------------


class TestSandboxBootstrapperSuccessWithHub:
    """Full happy-path: local zone + remote hub zones all mounted."""

    def test_local_zone_mounted_in_nexus_fs(self, tmp_path: Path) -> None:
        """nexus_fs.sys_setattr is called for /zone/local with DT_MOUNT."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(tmp_path)

        mock_session = MagicMock()
        mock_session.zones = []

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as MockHandshake,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            MockHandshake.return_value.run.return_value = mock_session
            sb.run()

        assert "/zone/local" in _mounted_paths(nexus_fs)
        local_call = next(
            c for c in nexus_fs.sys_setattr.call_args_list if c.args[0] == "/zone/local"
        )
        assert local_call.kwargs["entry_type"] == DT_MOUNT
        # Issue #3786: zone_id="root" so canonical_key is /root/zone/local —
        # matches _build_rust_ctx which always supplies zone_id=ROOT_ZONE_ID
        # for Python-bound calls.  Was "local" pre-fix but the VFS router
        # then looked at /local/zone/local and missed the mount.
        assert local_call.kwargs["zone_id"] == "root"

    def test_remote_zones_mounted_in_nexus_fs(self, tmp_path: Path) -> None:
        """For each HubZoneGrant, sys_setattr is called for /zone/<id>."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(tmp_path)

        mock_grant_1 = MagicMock()
        mock_grant_1.zone_id = "company"
        mock_grant_1.permission = "r"

        mock_grant_2 = MagicMock()
        mock_grant_2.zone_id = "shared"
        mock_grant_2.permission = "rw"

        mock_session = MagicMock()
        mock_session.zones = [mock_grant_1, mock_grant_2]

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as MockHandshake,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            MockHandshake.return_value.run.return_value = mock_session
            sb.run()

        paths = _mounted_paths(nexus_fs)
        assert "/zone/company" in paths
        assert "/zone/shared" in paths

    def test_remote_zone_mounted_with_rust_native_remote_backend(self, tmp_path: Path) -> None:
        """Remote zones use backend_type='remote' so Rust constructs the gRPC client."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(tmp_path)

        mock_grant = MagicMock()
        mock_grant.zone_id = "shared"
        mock_grant.permission = "rw"

        mock_session = MagicMock()
        mock_session.transport = MagicMock()
        mock_session.zones = [mock_grant]

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as MockHandshake,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            MockHandshake.return_value.run.return_value = mock_session
            sb.run()

        remote_calls = [
            c for c in nexus_fs.sys_setattr.call_args_list if c.args[0] == "/zone/shared"
        ]
        assert len(remote_calls) == 1
        kw = remote_calls[0].kwargs
        assert kw["entry_type"] == DT_MOUNT
        assert kw["backend_type"] == "remote"
        # Issue #3786: zone_id="root" so canonical_key is /root/zone/shared —
        # matches _build_rust_ctx (always supplies zone_id=ROOT_ZONE_ID).
        assert kw["zone_id"] == "root"
        assert kw["remote_auth_token"] == "tok-abc"
        assert "hub.example.com" in kw["server_address"]

    def test_local_zone_registered_in_search_registry(self, tmp_path: Path) -> None:
        """search_registry.register() is called with zone_id='local' and search_daemon."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(tmp_path)

        mock_session = MagicMock()
        mock_session.zones = []

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as MockHandshake,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            MockHandshake.return_value.run.return_value = mock_session
            sb.run()

        calls = search_registry.register.call_args_list
        zone_ids = [c.args[0] if c.args else c.kwargs.get("zone_id") for c in calls]
        assert "local" in zone_ids

    def test_remote_zones_registered_in_search_registry(self, tmp_path: Path) -> None:
        """search_registry.register_remote() is called for each hub zone."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(tmp_path)

        mock_grant = MagicMock()
        mock_grant.zone_id = "company"
        mock_grant.permission = "r"

        mock_session = MagicMock()
        mock_session.zones = [mock_grant]

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as MockHandshake,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            MockHandshake.return_value.run.return_value = mock_session
            sb.run()

        calls = search_registry.register_remote.call_args_list
        zone_ids = [c.args[0] if c.args else c.kwargs.get("zone_id") for c in calls]
        assert "company" in zone_ids

    def test_boot_indexer_started(self, tmp_path: Path) -> None:
        """BootIndexer.start_async() is called during run()."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(tmp_path)

        mock_session = MagicMock()
        mock_session.zones = []

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as MockHandshake,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer") as MockIndexer,
        ):
            MockHandshake.return_value.run.return_value = mock_session
            mock_indexer_instance = MockIndexer.return_value
            sb.run()

        mock_indexer_instance.start_async.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Hub handshake fails → local-only mode
# ---------------------------------------------------------------------------


class TestSandboxBootstrapperHandshakeFailure:
    """When the handshake fails, boot continues in local-only mode."""

    @pytest.mark.parametrize(
        "exc_class",
        [HandshakeAuthError, HandshakeConnectionError],
    )
    def test_handshake_error_does_not_crash(
        self, tmp_path: Path, exc_class: type[Exception]
    ) -> None:
        """HandshakeAuthError / HandshakeConnectionError are caught — no re-raise."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(tmp_path)

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as MockHandshake,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            MockHandshake.return_value.run.side_effect = exc_class("error")
            # Must not raise
            sb.run()

    @pytest.mark.parametrize(
        "exc_class",
        [HandshakeAuthError, HandshakeConnectionError],
    )
    def test_handshake_error_emits_warn_log(
        self,
        tmp_path: Path,
        exc_class: type[Exception],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A WARN-level message is logged when the handshake fails."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(tmp_path)

        with (
            caplog.at_level(logging.WARNING, logger="nexus.daemon.sandbox_bootstrap"),
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as MockHandshake,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            MockHandshake.return_value.run.side_effect = exc_class("handshake failed")
            sb.run()

        assert any(r.levelno >= logging.WARNING for r in caplog.records), (
            "Expected at least one WARNING log after handshake failure"
        )

    @pytest.mark.parametrize(
        "exc_class",
        [HandshakeAuthError, HandshakeConnectionError],
    )
    def test_handshake_failure_local_zone_still_mounted(
        self, tmp_path: Path, exc_class: type[Exception]
    ) -> None:
        """Even after handshake failure, the local zone is mounted."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(tmp_path)

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as MockHandshake,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            MockHandshake.return_value.run.side_effect = exc_class("error")
            sb.run()

        assert "/zone/local" in _mounted_paths(nexus_fs)

    @pytest.mark.parametrize(
        "exc_class",
        [HandshakeAuthError, HandshakeConnectionError],
    )
    def test_handshake_failure_no_remote_zones_registered(
        self, tmp_path: Path, exc_class: type[Exception]
    ) -> None:
        """After handshake failure, register_remote is never called."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(tmp_path)

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as MockHandshake,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            MockHandshake.return_value.run.side_effect = exc_class("error")
            sb.run()

        search_registry.register_remote.assert_not_called()


# ---------------------------------------------------------------------------
# 3. hub_url=None → skip handshake, local-only boot
# ---------------------------------------------------------------------------


class TestSandboxBootstrapperNoHub:
    """When hub_url is None, the handshake is skipped entirely."""

    def test_no_hub_url_skips_handshake(self, tmp_path: Path) -> None:
        """FederationHandshake is never instantiated when hub_url=None."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(
            tmp_path, hub_url=None, hub_token=None
        )

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake") as MockHandshake,
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            sb.run()

        MockHandshake.assert_not_called()

    def test_no_hub_url_local_zone_mounted(self, tmp_path: Path) -> None:
        """Local zone is still mounted when hub_url=None."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(
            tmp_path, hub_url=None, hub_token=None
        )

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake"),
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            sb.run()

        assert "/zone/local" in _mounted_paths(nexus_fs)

    def test_no_hub_url_no_remote_zones(self, tmp_path: Path) -> None:
        """No remote zones are registered when hub_url=None."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(
            tmp_path, hub_url=None, hub_token=None
        )

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake"),
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer"),
        ):
            sb.run()

        search_registry.register_remote.assert_not_called()

    def test_no_hub_url_boot_indexer_still_started(self, tmp_path: Path) -> None:
        """BootIndexer is started even in local-only mode (hub_url=None)."""
        sb, nexus_fs, search_registry, search_daemon = _make_bootstrapper(
            tmp_path, hub_url=None, hub_token=None
        )

        with (
            patch("nexus.daemon.sandbox_bootstrap.FederationHandshake"),
            patch("nexus.daemon.sandbox_bootstrap.BootIndexer") as MockIndexer,
        ):
            mock_indexer_instance = MockIndexer.return_value
            sb.run()

        mock_indexer_instance.start_async.assert_called_once()
