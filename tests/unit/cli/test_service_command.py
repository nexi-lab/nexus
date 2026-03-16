"""Tests for the @service_command decorator."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from nexus.cli.output import add_output_options
from nexus.cli.service_command import ServiceResult, service_command
from nexus.cli.utils import REMOTE_API_KEY_OPTION, REMOTE_URL_OPTION

MOCK_URL = "http://localhost:2026"


def _mock_client(**overrides: object) -> MagicMock:
    """Create a mock client with context-manager support."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    for k, v in overrides.items():
        setattr(client, k, MagicMock(return_value=v))
    return client


def _make_cli(client_cls: type | None = None) -> click.Group:
    """Build a minimal CLI group with a @service_command-decorated command."""

    @click.group()
    def cli() -> None:
        pass

    @cli.command("test-cmd")
    @add_output_options
    @REMOTE_API_KEY_OPTION
    @REMOTE_URL_OPTION
    @service_command(client_class=client_cls)
    def test_cmd(client: Any) -> ServiceResult:
        data = client.get_data()
        return ServiceResult(data=data, message="OK")

    return cli


class TestServiceResult:
    def test_frozen_dataclass(self) -> None:
        result = ServiceResult(data={"key": "value"}, message="done")
        assert result.data == {"key": "value"}
        assert result.message == "done"
        assert result.human_formatter is None

    def test_frozen_prevents_mutation(self) -> None:
        result = ServiceResult(data={})
        with pytest.raises(AttributeError):
            result.data = {"new": "value"}  # noqa: F841

    def test_human_formatter_field(self) -> None:
        formatter = MagicMock()
        result = ServiceResult(data={"x": 1}, human_formatter=formatter)
        assert result.human_formatter is formatter

    def test_defaults(self) -> None:
        result = ServiceResult(data=[1, 2, 3])
        assert result.human_formatter is None
        assert result.message is None


class TestServiceCommand:
    def test_happy_path(self) -> None:
        client = _mock_client(get_data={"result": "success"})
        mock_cls = MagicMock(return_value=client)
        cli = _make_cli(client_cls=mock_cls)
        runner = CliRunner()
        with patch.dict(os.environ, {"NEXUS_NO_AUTO_JSON": "1"}):
            result = runner.invoke(cli, ["test-cmd", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "OK" in result.output

    def test_missing_url_exits_nonzero(self) -> None:
        cli = _make_cli(client_cls=MagicMock)
        runner = CliRunner()
        result = runner.invoke(cli, ["test-cmd"])
        assert result.exit_code != 0

    def test_json_output_envelope(self) -> None:
        client = _mock_client(get_data={"result": "success"})
        mock_cls = MagicMock(return_value=client)
        cli = _make_cli(client_cls=mock_cls)
        runner = CliRunner()
        result = runner.invoke(cli, ["test-cmd", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "data" in data
        assert data["data"]["result"] == "success"

    def test_client_class_parameter(self) -> None:
        client = _mock_client(get_data={"ok": True})
        mock_cls = MagicMock(return_value=client)
        cli = _make_cli(client_cls=mock_cls)
        runner = CliRunner()
        runner.invoke(cli, ["test-cmd", "--remote-url", MOCK_URL])
        mock_cls.assert_called_once_with(url=MOCK_URL, api_key=None)

    def test_error_handling(self) -> None:
        client = _mock_client()
        client.get_data.side_effect = RuntimeError("boom")
        mock_cls = MagicMock(return_value=client)
        cli = _make_cli(client_cls=mock_cls)
        runner = CliRunner()
        result = runner.invoke(cli, ["test-cmd", "--remote-url", MOCK_URL])
        # render_error calls sys.exit with a nonzero code
        assert result.exit_code != 0

    def test_error_handling_json_mode(self) -> None:
        client = _mock_client()
        client.get_data.side_effect = RuntimeError("boom")
        mock_cls = MagicMock(return_value=client)
        cli = _make_cli(client_cls=mock_cls)
        runner = CliRunner()
        result = runner.invoke(cli, ["test-cmd", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["data"] is None
        assert "boom" in data["error"]["message"]
