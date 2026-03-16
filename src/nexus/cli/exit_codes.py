"""Semantic exit codes for the Nexus CLI.

Uses POSIX sysexits.h codes (64-78) to avoid conflicts with bash reserved
codes (2 = misuse of shell builtins, 126-128+N = signals).

Reference: https://man.freebsd.org/cgi/man.cgi?query=sysexits
"""

from enum import IntEnum


class ExitCode(IntEnum):
    """CLI exit codes following sysexits.h conventions."""

    SUCCESS = 0
    GENERAL_ERROR = 1
    USAGE_ERROR = 64  # EX_USAGE — bad command invocation
    DATA_ERROR = 65  # EX_DATAERR — bad input data
    NOT_FOUND = 66  # EX_NOINPUT — file/resource not found
    UNAVAILABLE = 69  # EX_UNAVAILABLE — service/connection unavailable
    INTERNAL_ERROR = 70  # EX_SOFTWARE — internal error / bug
    TEMPFAIL = 75  # EX_TEMPFAIL — temporary failure (timeout, retry)
    PERMISSION_DENIED = 77  # EX_NOPERM — permission denied
    CONFIG_ERROR = 78  # EX_CONFIG — bad configuration
