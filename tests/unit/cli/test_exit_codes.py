"""Tests for CLI exit codes module."""

import pytest

from nexus.cli.exit_codes import ExitCode


class TestExitCode:
    """Exit codes follow sysexits.h conventions."""

    @pytest.mark.parametrize(
        ("code", "expected_value"),
        [
            (ExitCode.SUCCESS, 0),
            (ExitCode.GENERAL_ERROR, 1),
            (ExitCode.USAGE_ERROR, 64),
            (ExitCode.DATA_ERROR, 65),
            (ExitCode.NOT_FOUND, 66),
            (ExitCode.UNAVAILABLE, 69),
            (ExitCode.INTERNAL_ERROR, 70),
            (ExitCode.TEMPFAIL, 75),
            (ExitCode.PERMISSION_DENIED, 77),
            (ExitCode.CONFIG_ERROR, 78),
        ],
    )
    def test_exit_code_values(self, code: ExitCode, expected_value: int) -> None:
        assert int(code) == expected_value

    def test_exit_codes_are_int(self) -> None:
        """All exit codes should be usable as integer exit codes."""
        for code in ExitCode:
            assert isinstance(int(code), int)

    def test_no_bash_reserved_codes(self) -> None:
        """Exit codes 126-255 are reserved by bash; we must not use them.

        Exception: signal-based codes (128+N) are intentional — e.g.
        INTERRUPTED=130 (128+SIGINT) is the conventional shell code for
        processes killed by a signal and must surface through ``nexus ready``.
        """
        # 128+1 .. 128+64 covers all standard POSIX signals.
        _SIGNAL_CODES = set(range(128 + 1, 128 + 64 + 1))
        for code in ExitCode:
            v = int(code)
            if v in _SIGNAL_CODES:
                continue  # intentional signal-based exit code
            assert v < 126 or v > 255, f"ExitCode.{code.name}={v} is in bash reserved range"
