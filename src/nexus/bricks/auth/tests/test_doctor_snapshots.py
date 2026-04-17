"""Snapshot tests for `auth doctor` output.

Intentional output changes require `pytest --snapshot-update`.
Every failure scenario gets one snapshot — accidental formatting drift
across refactors will be caught here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from click.testing import CliRunner

from nexus.bricks.auth.cli_commands import auth
from nexus.bricks.auth.profile import AuthProfileFailureReason
from nexus.contracts.unified_auth import AuthStatus


@dataclass
class _StubSummary:
    service: str
    profile_id: str
    status: AuthStatus
    message: str
    source: str | None = None
    failure_reason: AuthProfileFailureReason | None = None


class _StubService:
    def __init__(self, summaries: list[_StubSummary]) -> None:
        self._summaries = summaries

    async def list_summaries(self) -> list[_StubSummary]:
        return self._summaries


SCENARIOS: list[tuple[str, list[_StubSummary]]] = [
    (
        "all_ok",
        [
            _StubSummary(
                service="openai",
                profile_id="team",
                status=AuthStatus.AUTHED,
                message="access token valid 54m",
                source="nexus-oauth",
            ),
            _StubSummary(
                service="s3",
                profile_id="default",
                status=AuthStatus.AUTHED,
                message="via AWS_PROFILE=default",
                source="aws-cli",
            ),
        ],
    ),
    (
        "cooldown",
        [
            _StubSummary(
                service="s3",
                profile_id="work-prod",
                status=AuthStatus.ERROR,
                message="rate_limit from 2026-04-10T10:14:22Z, expires in 43m",
                source="aws-cli",
                failure_reason=AuthProfileFailureReason.RATE_LIMIT,
            ),
        ],
    ),
    (
        "session_expired",
        [
            _StubSummary(
                service="gmail",
                profile_id="other@example.com",
                status=AuthStatus.ERROR,
                message="session_expired",
                source="nexus-oauth",
                failure_reason=AuthProfileFailureReason.SESSION_EXPIRED,
            ),
        ],
    ),
    (
        "scope_insufficient",
        [
            _StubSummary(
                service="gmail",
                profile_id="you@example.com",
                status=AuthStatus.ERROR,
                message="scope_insufficient",
                source="nexus-oauth",
                failure_reason=AuthProfileFailureReason.SCOPE_INSUFFICIENT,
            ),
        ],
    ),
    (
        "upstream_cli_missing_gcs",
        [
            _StubSummary(
                service="gcs",
                profile_id="my-project",
                status=AuthStatus.ERROR,
                message="gcloud CLI not installed",
                source="gcloud",
                failure_reason=AuthProfileFailureReason.UPSTREAM_CLI_MISSING,
            ),
        ],
    ),
    (
        "upstream_cli_missing_s3",
        [
            _StubSummary(
                service="s3",
                profile_id="default",
                status=AuthStatus.ERROR,
                message="aws CLI not installed",
                source="aws-cli",
                failure_reason=AuthProfileFailureReason.UPSTREAM_CLI_MISSING,
            ),
        ],
    ),
    (
        "proxy_or_tls",
        [
            _StubSummary(
                service="openai",
                profile_id="team",
                status=AuthStatus.ERROR,
                message="SSLError: certificate verify failed",
                source="nexus-oauth",
                failure_reason=AuthProfileFailureReason.PROXY_OR_TLS,
            ),
        ],
    ),
    (
        "mfa_required",
        [
            _StubSummary(
                service="github",
                profile_id="you",
                status=AuthStatus.ERROR,
                message="MFA challenge unmet",
                source="nexus-oauth",
                failure_reason=AuthProfileFailureReason.MFA_REQUIRED,
            ),
        ],
    ),
    (
        "clock_skew",
        [
            _StubSummary(
                service="openai",
                profile_id="team",
                status=AuthStatus.ERROR,
                message="token nbf in the future; clock skew ~180s",
                source="nexus-oauth",
                failure_reason=AuthProfileFailureReason.CLOCK_SKEW,
            ),
        ],
    ),
    (
        "mixed",
        [
            _StubSummary(
                service="openai",
                profile_id="team",
                status=AuthStatus.AUTHED,
                message="access token valid 54m",
                source="nexus-oauth",
            ),
            _StubSummary(
                service="s3",
                profile_id="work-prod",
                status=AuthStatus.ERROR,
                message="rate_limit from 2026-04-10T10:14:22Z, expires in 43m",
                source="aws-cli",
                failure_reason=AuthProfileFailureReason.RATE_LIMIT,
            ),
            _StubSummary(
                service="gmail",
                profile_id="other@example.com",
                status=AuthStatus.ERROR,
                message="session_expired",
                source="nexus-oauth",
                failure_reason=AuthProfileFailureReason.SESSION_EXPIRED,
            ),
        ],
    ),
]


@pytest.mark.parametrize("_name,summaries", SCENARIOS, ids=[s[0] for s in SCENARIOS])
@pytest.mark.xdist_group("doctor_snapshots")
def test_doctor_output_snapshot(monkeypatch, _name, summaries, snapshot):
    service = _StubService(summaries)
    monkeypatch.setattr("nexus.bricks.auth.cli_commands._build_auth_service", lambda: service)

    result = CliRunner().invoke(auth, ["doctor"])

    # Snapshot captures combined output + exit code so both are locked down.
    combined = f"exit_code={result.exit_code}\n{result.output}"
    assert combined == snapshot
