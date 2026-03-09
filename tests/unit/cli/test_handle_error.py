"""Tests for handle_error() — the unified error handler.

Tests both human mode (Rich console output) and JSON mode (structured envelope).
"""

from __future__ import annotations

import json

import pytest

from nexus.cli.exit_codes import ExitCode
from nexus.cli.output import OutputOptions
from nexus.contracts.exceptions import (
    NexusError,
    NexusFileNotFoundError,
    ValidationError,
)


def _json_opts() -> OutputOptions:
    return OutputOptions(
        json_output=True, quiet=False, verbosity=0, fields=None, request_id="test-req"
    )


def _human_opts() -> OutputOptions:
    return OutputOptions(
        json_output=False, quiet=False, verbosity=0, fields=None, request_id="test-req"
    )


class TestHandleErrorExitCodes:
    """Verify exception → exit code mapping."""

    def test_permission_error(self) -> None:
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit) as exc_info:
            handle_error(PermissionError("access denied"))
        assert exc_info.value.code == ExitCode.PERMISSION_DENIED

    def test_access_denied_error(self) -> None:
        from nexus.cli.utils import handle_error
        from nexus.contracts.exceptions import AccessDeniedError

        with pytest.raises(SystemExit) as exc_info:
            handle_error(AccessDeniedError("no access"))
        assert exc_info.value.code == ExitCode.PERMISSION_DENIED

    def test_not_found_error(self) -> None:
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit) as exc_info:
            handle_error(NexusFileNotFoundError("/missing"))
        assert exc_info.value.code == ExitCode.NOT_FOUND

    def test_validation_error(self) -> None:
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit) as exc_info:
            handle_error(ValidationError("bad input"))
        assert exc_info.value.code == ExitCode.USAGE_ERROR

    def test_connection_error(self) -> None:
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit) as exc_info:
            handle_error(ConnectionError("refused"))
        assert exc_info.value.code == ExitCode.UNAVAILABLE

    def test_timeout_error(self) -> None:
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit) as exc_info:
            handle_error(TimeoutError("timed out"))
        assert exc_info.value.code == ExitCode.TEMPFAIL

    def test_nexus_error(self) -> None:
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit) as exc_info:
            handle_error(NexusError("internal"))
        assert exc_info.value.code == ExitCode.INTERNAL_ERROR

    def test_generic_exception(self) -> None:
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit) as exc_info:
            handle_error(RuntimeError("unexpected"))
        assert exc_info.value.code == ExitCode.GENERAL_ERROR


class TestHandleErrorJsonMode:
    """Verify JSON output envelope when output_opts is provided."""

    def test_json_error_envelope(self, capsys: pytest.CaptureFixture[str]) -> None:
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit):
            handle_error(NexusFileNotFoundError("/missing"), output_opts=_json_opts())
        output = json.loads(capsys.readouterr().out)
        assert output["data"] is None
        assert output["error"]["code"] == "NOT_FOUND"
        assert output["error"]["type"] == "NexusFileNotFoundError"
        assert "/missing" in output["error"]["message"]

    def test_json_error_with_timing(self, capsys: pytest.CaptureFixture[str]) -> None:
        from nexus.cli.timing import CommandTiming
        from nexus.cli.utils import handle_error

        timing = CommandTiming()
        with timing.phase("server"):
            pass
        with pytest.raises(SystemExit):
            handle_error(
                RuntimeError("oops"),
                output_opts=_json_opts(),
                timing=timing,
            )
        output = json.loads(capsys.readouterr().out)
        assert "_timing" in output

    def test_human_mode_no_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When output_opts is human mode, should NOT produce JSON."""
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit):
            handle_error(RuntimeError("oops"), output_opts=_human_opts())
        captured = capsys.readouterr()
        # Should not be valid JSON (it's Rich console output)
        with pytest.raises(json.JSONDecodeError):
            json.loads(captured.out)

    def test_no_output_opts_uses_human(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When output_opts is None (legacy), uses human mode."""
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit):
            handle_error(RuntimeError("oops"))
        # Should not crash — human mode works without output_opts
