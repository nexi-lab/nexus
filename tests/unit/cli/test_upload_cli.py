"""Tests for nexus upload CLI commands."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.base import BaseServiceClient
from nexus.cli.clients.upload import UploadClient
from nexus.cli.commands.upload import upload

MOCK_URL = "http://localhost:2026"


def _patch_client(**method_returns: object) -> tuple[ExitStack, dict[str, MagicMock]]:
    """Patch BaseServiceClient so UploadClient can be instantiated without httpx.

    Returns an ExitStack context manager and a dict of method mocks.
    """
    stack = ExitStack()
    stack.enter_context(patch.dict(os.environ, {"NEXUS_NO_AUTO_JSON": "1"}))
    stack.enter_context(patch.object(BaseServiceClient, "__init__", lambda self, *a, **kw: None))
    stack.enter_context(patch.object(BaseServiceClient, "__enter__", lambda self: self))
    stack.enter_context(patch.object(BaseServiceClient, "__exit__", lambda self, *a: None))
    mocks: dict[str, MagicMock] = {}
    for method_name, return_value in method_returns.items():
        m = stack.enter_context(patch.object(UploadClient, method_name, return_value=return_value))
        mocks[method_name] = m
    return stack, mocks


class TestUploadStatus:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(status={"upload_id": "upl_123", "offset": 5000, "length": 10000})
        with stack:
            result = runner.invoke(upload, ["status", "upl_123", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "upl_123" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(status={"upload_id": "upl_123", "offset": 5000, "length": 10000})
        with stack:
            result = runner.invoke(
                upload, ["status", "upl_123", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["upload_id"] == "upl_123"

    def test_client_args(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(status={"upload_id": "upl_123"})
        with stack:
            runner.invoke(upload, ["status", "upl_123", "--remote-url", MOCK_URL])
        mocks["status"].assert_called_once_with("upl_123")

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(upload, ["status", "upl_123"])
        assert result.exit_code != 0


class TestUploadCancel:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(cancel={"status": "cancelled"})
        with stack:
            result = runner.invoke(upload, ["cancel", "upl_123", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "cancelled" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(cancel={"status": "cancelled"})
        with stack:
            result = runner.invoke(
                upload, ["cancel", "upl_123", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["status"] == "cancelled"

    def test_client_args(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(cancel={"status": "cancelled"})
        with stack:
            runner.invoke(upload, ["cancel", "upl_123", "--remote-url", MOCK_URL])
        mocks["cancel"].assert_called_once_with("upl_123")

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(upload, ["cancel", "upl_123"])
        assert result.exit_code != 0
