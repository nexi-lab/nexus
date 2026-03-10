"""Tests for nexus reputation CLI commands."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.reputation import ReputationClient
from nexus.cli.commands.reputation import reputation

MOCK_URL = "http://localhost:2026"
_ENV = {"NEXUS_NO_AUTO_JSON": "1"}


@contextmanager
def _mock_client(**overrides: Any):
    """Patch ReputationClient so service_command uses a mock instance.

    The @service_command decorator captures client_class in a closure at import
    time, so patching the module-level name has no effect.  Instead we patch the
    class methods directly via ``patch.object``.
    """
    with ExitStack() as stack:
        stack.enter_context(patch.object(ReputationClient, "__init__", lambda self, **kw: None))
        stack.enter_context(patch.object(ReputationClient, "__enter__", lambda self: self))
        stack.enter_context(patch.object(ReputationClient, "__exit__", lambda self, *a: False))
        mocks: dict[str, MagicMock] = {}
        for name, retval in overrides.items():
            m = MagicMock(return_value=retval)
            stack.enter_context(patch.object(ReputationClient, name, m))
            mocks[name] = m
        yield mocks


class TestReputationShow:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            show={
                "composite_score": 0.85,
                "reliability_score": 0.9,
                "quality_score": 0.8,
                "timeliness_score": 0.85,
                "fairness_score": 0.85,
                "total_ratings": 42,
            }
        ):
            result = runner.invoke(reputation, ["show", "alice", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "0.85" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(show={"composite_score": 0.85, "total_ratings": 42}):
            result = runner.invoke(
                reputation, ["show", "alice", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["composite_score"] == 0.85

    def test_default_context_and_window(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(show={"composite_score": 0.7}) as mocks:
            runner.invoke(reputation, ["show", "alice", "--remote-url", MOCK_URL])
        mocks["show"].assert_called_once_with("alice", context="general", window="all_time")

    def test_with_window(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(show={"composite_score": 0.7}) as mocks:
            result = runner.invoke(
                reputation,
                ["show", "alice", "--window", "30d", "--remote-url", MOCK_URL],
            )
        assert result.exit_code == 0
        mocks["show"].assert_called_once_with("alice", context="general", window="30d")

    def test_missing_url_fails(self) -> None:
        runner = CliRunner(env=_ENV)
        result = runner.invoke(reputation, ["show", "alice"])
        assert result.exit_code != 0


class TestReputationLeaderboard:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            leaderboard={
                "leaderboard": [
                    {"agent_id": "alice", "composite_score": 0.95, "total_ratings": 100},
                    {"agent_id": "bob", "composite_score": 0.90, "total_ratings": 80},
                ]
            }
        ):
            result = runner.invoke(reputation, ["leaderboard", "--remote-url", MOCK_URL])
        assert result.exit_code == 0

    def test_empty(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(leaderboard={"leaderboard": []}):
            result = runner.invoke(reputation, ["leaderboard", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No reputation data" in result.output

    def test_default_limit_and_zone(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(leaderboard={"leaderboard": []}) as mocks:
            runner.invoke(reputation, ["leaderboard", "--remote-url", MOCK_URL])
        mocks["leaderboard"].assert_called_once_with(zone_id=None, limit=20)

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            leaderboard={
                "leaderboard": [
                    {"agent_id": "alice", "composite_score": 0.95},
                ]
            }
        ):
            result = runner.invoke(reputation, ["leaderboard", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["leaderboard"]) == 1


class TestReputationFeedback:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(submit_feedback={"status": "accepted"}):
            result = runner.invoke(
                reputation,
                [
                    "feedback",
                    "exch_1",
                    "--rater",
                    "alice",
                    "--rated",
                    "bob",
                    "--outcome",
                    "positive",
                    "--remote-url",
                    MOCK_URL,
                ],
            )
        assert result.exit_code == 0
        assert "submitted" in result.output.lower()

    def test_client_args(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(submit_feedback={"status": "accepted"}) as mocks:
            runner.invoke(
                reputation,
                [
                    "feedback",
                    "exch_1",
                    "--rater",
                    "alice",
                    "--rated",
                    "bob",
                    "--outcome",
                    "negative",
                    "--reliability",
                    "0.3",
                    "--quality",
                    "0.2",
                    "--remote-url",
                    MOCK_URL,
                ],
            )
        mocks["submit_feedback"].assert_called_once_with(
            "exch_1",
            rater_agent_id="alice",
            rated_agent_id="bob",
            outcome="negative",
            reliability_score=0.3,
            quality_score=0.2,
        )

    def test_outcome_required(self) -> None:
        runner = CliRunner(env=_ENV)
        result = runner.invoke(
            reputation,
            ["feedback", "exch_1", "--rater", "alice", "--rated", "bob", "--remote-url", MOCK_URL],
        )
        assert result.exit_code != 0

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(submit_feedback={"status": "accepted"}):
            result = runner.invoke(
                reputation,
                [
                    "feedback",
                    "exch_1",
                    "--rater",
                    "alice",
                    "--rated",
                    "bob",
                    "--outcome",
                    "neutral",
                    "--remote-url",
                    MOCK_URL,
                    "--json",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["status"] == "accepted"


class TestReputationDisputeCreate:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(dispute_create={"dispute_id": "dsp_1", "status": "filed"}):
            result = runner.invoke(
                reputation,
                [
                    "dispute",
                    "create",
                    "exch_1",
                    "--complainant",
                    "alice",
                    "--respondent",
                    "bob",
                    "--reason",
                    "Service not delivered",
                    "--remote-url",
                    MOCK_URL,
                ],
            )
        assert result.exit_code == 0
        assert "dsp_1" in result.output

    def test_reason_required(self) -> None:
        runner = CliRunner(env=_ENV)
        result = runner.invoke(
            reputation,
            [
                "dispute",
                "create",
                "exch_1",
                "--complainant",
                "alice",
                "--respondent",
                "bob",
                "--remote-url",
                MOCK_URL,
            ],
        )
        assert result.exit_code != 0

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(dispute_create={"dispute_id": "dsp_1", "status": "filed"}):
            result = runner.invoke(
                reputation,
                [
                    "dispute",
                    "create",
                    "exch_1",
                    "--complainant",
                    "alice",
                    "--respondent",
                    "bob",
                    "--reason",
                    "Bad quality",
                    "--remote-url",
                    MOCK_URL,
                    "--json",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["dispute_id"] == "dsp_1"
