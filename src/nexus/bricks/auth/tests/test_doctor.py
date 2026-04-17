from __future__ import annotations

import pytest

from nexus.bricks.auth.doctor import (
    DoctorLine,
    fix_hint_for,
)
from nexus.bricks.auth.profile import AuthProfileFailureReason


@pytest.mark.parametrize("reason", list(AuthProfileFailureReason))
def test_every_failure_reason_has_non_empty_generic_hint(reason: AuthProfileFailureReason) -> None:
    hint = fix_hint_for(reason=reason, provider=None)
    assert hint, f"no fix hint for {reason}"
    assert len(hint.strip()) > 10, f"hint for {reason} is suspiciously short: {hint!r}"


@pytest.mark.parametrize(
    "reason,provider,expected_substring",
    [
        (AuthProfileFailureReason.SESSION_EXPIRED, "gmail", "auth connect"),
        (AuthProfileFailureReason.UPSTREAM_CLI_MISSING, "gcs", "gcloud"),
        (AuthProfileFailureReason.UPSTREAM_CLI_MISSING, "s3", "aws"),
    ],
)
def test_provider_specific_hints_name_correct_cli(reason, provider, expected_substring) -> None:
    hint = fix_hint_for(reason=reason, provider=provider)
    assert expected_substring in hint, f"expected {expected_substring!r} in {hint!r}"


def test_doctor_line_format_includes_all_parts() -> None:
    line = DoctorLine(
        source="nexus-oauth",
        service="openai",
        profile_id="team",
        status="error",
        detail="session_expired",
        fix_hint="Run `nexus auth connect openai`.",
    )
    rendered = line.format()
    assert "[nexus-oauth]" in rendered
    assert "openai/team" in rendered
    assert "error" in rendered
    assert "session_expired" in rendered
    assert "Run `nexus auth connect openai`." in rendered


def test_doctor_line_ok_no_fix_hint_in_output() -> None:
    line = DoctorLine(
        source="aws-cli",
        service="s3",
        profile_id="default",
        status="ok",
        detail="via AWS_PROFILE=default",
        fix_hint="",
    )
    rendered = line.format()
    # fix_hint is empty → no trailing "— <hint>" after the detail
    assert rendered.endswith("via AWS_PROFILE=default")
