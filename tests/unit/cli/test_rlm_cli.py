"""Tests for nexus rlm CLI commands."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.base import BaseServiceClient
from nexus.cli.clients.rlm import RLMClient
from nexus.cli.commands.rlm import rlm

MOCK_URL = "http://localhost:2026"


def _patch_client(**method_returns: object) -> tuple[ExitStack, dict[str, MagicMock]]:
    """Patch BaseServiceClient so RLMClient can be instantiated without httpx.

    Returns an ExitStack context manager and a dict of method mocks.
    """
    stack = ExitStack()
    stack.enter_context(patch.dict(os.environ, {"NEXUS_NO_AUTO_JSON": "1"}))
    stack.enter_context(patch.object(BaseServiceClient, "__init__", lambda self, *a, **kw: None))
    stack.enter_context(patch.object(BaseServiceClient, "__enter__", lambda self: self))
    stack.enter_context(patch.object(BaseServiceClient, "__exit__", lambda self, *a: None))
    mocks: dict[str, MagicMock] = {}
    for method_name, return_value in method_returns.items():
        m = stack.enter_context(patch.object(RLMClient, method_name, return_value=return_value))
        mocks[method_name] = m
    return stack, mocks


class TestRlmInfer:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(
            infer={
                "status": "complete",
                "answer": "This is a summary.",
                "iterations": 3,
                "total_tokens": 1500,
            }
        )
        with stack:
            result = runner.invoke(
                rlm,
                ["infer", "/doc.pdf", "--prompt", "Summarize", "--remote-url", MOCK_URL],
            )
        assert result.exit_code == 0
        assert "This is a summary." in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(infer={"status": "complete", "answer": "Summary", "iterations": 2})
        with stack:
            result = runner.invoke(
                rlm,
                [
                    "infer",
                    "/doc.pdf",
                    "--prompt",
                    "Summarize",
                    "--remote-url",
                    MOCK_URL,
                    "--json",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["status"] == "complete"

    def test_client_args(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(infer={"status": "complete"})
        with stack:
            runner.invoke(
                rlm,
                ["infer", "/doc.pdf", "--prompt", "Summarize", "--remote-url", MOCK_URL],
            )
        mocks["infer"].assert_called_once_with(
            "/doc.pdf", prompt="Summarize", model=None, max_iterations=None
        )

    def test_client_args_with_options(self) -> None:
        runner = CliRunner()
        stack, mocks = _patch_client(infer={"status": "complete"})
        with stack:
            runner.invoke(
                rlm,
                [
                    "infer",
                    "/doc.pdf",
                    "--prompt",
                    "Summarize",
                    "--model",
                    "gpt-4",
                    "--max-iterations",
                    "5",
                    "--remote-url",
                    MOCK_URL,
                ],
            )
        mocks["infer"].assert_called_once_with(
            "/doc.pdf", prompt="Summarize", model="gpt-4", max_iterations=5
        )

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(rlm, ["infer", "/doc.pdf", "--prompt", "Summarize"])
        assert result.exit_code != 0


class TestRlmStatus:
    def test_happy_path(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(status={"available": True, "model": "gpt-4", "max_concurrent": 10})
        with stack:
            result = runner.invoke(rlm, ["status", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "Yes" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(status={"available": True, "model": "gpt-4"})
        with stack:
            result = runner.invoke(rlm, ["status", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["available"] is True

    def test_unavailable_status(self) -> None:
        runner = CliRunner()
        stack, _ = _patch_client(status={"available": False})
        with stack:
            result = runner.invoke(rlm, ["status", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No" in result.output

    def test_missing_url_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(rlm, ["status"])
        assert result.exit_code != 0
