"""Tests for CLI utility functions."""

import pytest

from nexus.cli.exit_codes import ExitCode
from nexus.cli.utils import BackendConfig, resolve_content


class TestBackendConfig:
    """BackendConfig is a frozen dataclass."""

    def test_defaults(self) -> None:
        config = BackendConfig()
        assert config.backend == "local"
        assert config.remote_url is None
        assert config.gcs_bucket is None

    def test_frozen(self) -> None:
        config = BackendConfig()
        with pytest.raises(AttributeError):
            config.backend = "gcs"

    def test_custom_values(self) -> None:
        config = BackendConfig(
            backend="gcs",
            gcs_bucket="my-bucket",
            remote_url="http://localhost:2026",
        )
        assert config.backend == "gcs"
        assert config.gcs_bucket == "my-bucket"
        assert config.remote_url == "http://localhost:2026"


class TestResolveContent:
    """resolve_content extracts content from CLI args, files, or stdin."""

    def test_from_string_content(self) -> None:
        result = resolve_content("hello world", None)
        assert result == b"hello world"

    def test_from_file_object(self, tmp_path: pytest.TempPathFactory) -> None:
        from io import BytesIO

        f = BytesIO(b"file content")
        result = resolve_content(None, f)
        assert result == b"file content"

    def test_file_takes_priority(self) -> None:
        from io import BytesIO

        f = BytesIO(b"from file")
        result = resolve_content("from arg", f)
        assert result == b"from file"

    def test_stdin_dash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from io import BytesIO

        monkeypatch.setattr(
            "sys.stdin", type("FakeStdin", (), {"buffer": BytesIO(b"from stdin")})()
        )
        result = resolve_content("-", None)
        assert result == b"from stdin"

    def test_no_content_exits(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            resolve_content(None, None)
        assert exc_info.value.code == ExitCode.USAGE_ERROR


class TestHandleError:
    """handle_error maps exceptions to semantic exit codes."""

    def _assert_exit_code(self, exc: Exception, expected_code: int) -> None:
        from nexus.cli.utils import handle_error

        with pytest.raises(SystemExit) as exc_info:
            handle_error(exc)
        assert exc_info.value.code == expected_code

    def test_permission_error(self) -> None:
        self._assert_exit_code(PermissionError("denied"), ExitCode.PERMISSION_DENIED)

    def test_file_not_found(self) -> None:
        from nexus.contracts.exceptions import NexusFileNotFoundError

        self._assert_exit_code(NexusFileNotFoundError("/test"), ExitCode.NOT_FOUND)

    def test_validation_error(self) -> None:
        from nexus.contracts.exceptions import ValidationError

        self._assert_exit_code(ValidationError("bad input"), ExitCode.USAGE_ERROR)

    def test_timeout_error(self) -> None:
        self._assert_exit_code(TimeoutError("timed out"), ExitCode.TEMPFAIL)

    def test_connection_error(self) -> None:
        self._assert_exit_code(ConnectionError("refused"), ExitCode.UNAVAILABLE)

    def test_os_error(self) -> None:
        self._assert_exit_code(OSError("disk full"), ExitCode.UNAVAILABLE)

    def test_nexus_error(self) -> None:
        from nexus.contracts.exceptions import NexusError

        self._assert_exit_code(NexusError("generic"), ExitCode.GENERAL_ERROR)

    def test_unexpected_error(self) -> None:
        self._assert_exit_code(RuntimeError("unexpected"), ExitCode.INTERNAL_ERROR)
