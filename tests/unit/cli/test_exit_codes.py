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
        """Exit codes 126-255 are reserved by bash; we must not use them."""
        for code in ExitCode:
            assert int(code) < 126 or int(code) > 255
