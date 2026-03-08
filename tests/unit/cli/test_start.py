"""Tests for ``nexus start`` command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli.commands.start import _init_tls, start


@pytest.fixture()
def cli_runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# _init_tls
# ---------------------------------------------------------------------------


class TestInitTls:
    @patch("nexus.security.tls.config.ZoneTlsConfig.from_data_dir")
    def test_skips_when_already_initialized(self, mock_from_data: MagicMock) -> None:
        """If TLS is already present, _init_tls should not generate new certs."""
        mock_existing = MagicMock()
        mock_existing.ca_cert_path = "/fake/ca.pem"
        mock_from_data.return_value = mock_existing

        with (
            patch("nexus.security.tls.certgen.load_pem_cert") as mock_load,
            patch("nexus.security.tls.certgen.cert_fingerprint", return_value="abc123"),
        ):
            mock_load.return_value = MagicMock()
            _init_tls("/fake/data", "default", 1)

    @patch("nexus.security.tls.config.ZoneTlsConfig.from_data_dir", return_value=None)
    @patch("nexus.security.tls.certgen.generate_zone_ca")
    @patch("nexus.security.tls.certgen.save_pem")
    @patch("nexus.security.tls.certgen.generate_node_cert")
    @patch("nexus.security.tls.certgen.cert_fingerprint", return_value="deadbeef")
    def test_generates_when_not_present(
        self,
        _mock_fp: MagicMock,
        mock_node: MagicMock,
        mock_save: MagicMock,
        mock_ca: MagicMock,
        _mock_config: MagicMock,
    ) -> None:
        mock_ca.return_value = (MagicMock(), MagicMock())
        mock_node.return_value = (MagicMock(), MagicMock())

        _init_tls("/fake/data", "test-zone", 2)

        mock_ca.assert_called_once_with("test-zone")
        mock_node.assert_called_once()
        assert mock_save.call_count == 4  # ca.pem, ca-key.pem, node.pem, node-key.pem


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

# start.py uses `from nexus.cli.utils import get_filesystem` at module level,
# so we patch it in the start module.  The other four symbols are lazily imported
# inside the start() function body, so we patch at their *source* modules.


class TestStartCommand:
    @patch("nexus.cli.commands.server.start_background_mount_sync")
    @patch("nexus.server.fastapi_server.run_server")
    @patch("nexus.server.fastapi_server.create_app")
    @patch("nexus.lib.env.get_database_url", return_value=None)
    @patch("nexus.cli.commands.start.get_filesystem")
    def test_start_basic(
        self,
        mock_get_fs: MagicMock,
        _mock_db: MagicMock,
        mock_create_app: MagicMock,
        mock_run: MagicMock,
        _mock_bg: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        mock_get_fs.return_value = MagicMock()
        mock_create_app.return_value = MagicMock()

        result = cli_runner.invoke(start, ["--skip-tls-init"])
        assert result.exit_code == 0
        mock_create_app.assert_called_once()
        mock_run.assert_called_once()

    @patch("nexus.cli.commands.server.start_background_mount_sync")
    @patch("nexus.server.fastapi_server.run_server")
    @patch("nexus.server.fastapi_server.create_app")
    @patch("nexus.lib.env.get_database_url", return_value=None)
    @patch("nexus.cli.commands.start.get_filesystem")
    @patch("nexus.security.tls.config.ZoneTlsConfig.from_data_dir", return_value=None)
    @patch("nexus.security.tls.certgen.generate_zone_ca")
    @patch("nexus.security.tls.certgen.save_pem")
    @patch("nexus.security.tls.certgen.generate_node_cert")
    @patch("nexus.security.tls.certgen.cert_fingerprint", return_value="deadbeef")
    def test_start_with_tls_init(
        self,
        _fp: MagicMock,
        mock_node: MagicMock,
        _save: MagicMock,
        mock_ca: MagicMock,
        _cfg: MagicMock,
        mock_get_fs: MagicMock,
        _db: MagicMock,
        _app: MagicMock,
        _run: MagicMock,
        _bg: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        mock_get_fs.return_value = MagicMock()
        mock_ca.return_value = (MagicMock(), MagicMock())
        mock_node.return_value = (MagicMock(), MagicMock())

        result = cli_runner.invoke(start)
        assert result.exit_code == 0
        mock_ca.assert_called_once()

    @patch("nexus.cli.commands.server.start_background_mount_sync")
    @patch("nexus.server.fastapi_server.run_server")
    @patch("nexus.server.fastapi_server.create_app")
    @patch("nexus.lib.env.get_database_url", return_value=None)
    @patch("nexus.cli.commands.start.get_filesystem")
    def test_start_custom_ports(
        self,
        mock_get_fs: MagicMock,
        _db: MagicMock,
        _app: MagicMock,
        mock_run: MagicMock,
        _bg: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        mock_get_fs.return_value = MagicMock()

        result = cli_runner.invoke(
            start, ["--port", "3000", "--grpc-port", "3001", "--skip-tls-init"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_run.call_args
        assert call_kwargs[1]["port"] == 3000

    @patch("nexus.cli.commands.server.start_background_mount_sync")
    @patch("nexus.server.fastapi_server.run_server")
    @patch("nexus.server.fastapi_server.create_app")
    @patch("nexus.lib.env.get_database_url", return_value=None)
    @patch("nexus.cli.commands.start.get_filesystem")
    def test_start_with_api_key(
        self,
        mock_get_fs: MagicMock,
        _db: MagicMock,
        _app: MagicMock,
        _run: MagicMock,
        _bg: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        mock_get_fs.return_value = MagicMock()

        result = cli_runner.invoke(start, ["--api-key", "test-key", "--skip-tls-init"])
        assert result.exit_code == 0
        call_kwargs = mock_get_fs.call_args
        assert call_kwargs[1]["enforce_permissions"] is True

    @patch("nexus.cli.commands.server.start_background_mount_sync")
    @patch("nexus.server.fastapi_server.run_server", side_effect=KeyboardInterrupt)
    @patch("nexus.server.fastapi_server.create_app")
    @patch("nexus.lib.env.get_database_url", return_value=None)
    @patch("nexus.cli.commands.start.get_filesystem")
    def test_start_ctrl_c(
        self,
        mock_get_fs: MagicMock,
        _db: MagicMock,
        _app: MagicMock,
        _run: MagicMock,
        _bg: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        mock_get_fs.return_value = MagicMock()

        result = cli_runner.invoke(start, ["--skip-tls-init"])
        assert result.exit_code == 0
        assert "stopped" in result.output.lower()
