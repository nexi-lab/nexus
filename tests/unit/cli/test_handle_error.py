"""Tests for handle_error() — the unified error handler.

Verifies exception → exit code mapping (sysexits.h conventions).
"""

from __future__ import annotations

import pytest

from nexus.cli.exit_codes import ExitCode
from nexus.contracts.exceptions import (
    NexusError,
    NexusFileNotFoundError,
    ValidationError,
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
        assert exc_info.value.code == ExitCode.GENERAL_ERROR

    def test_generic_exception(self) -> None:
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit) as exc_info:
            handle_error(RuntimeError("unexpected"))
        assert exc_info.value.code == ExitCode.INTERNAL_ERROR
