"""Tests for CLI output infrastructure."""

import json
from unittest.mock import patch

import pytest

from nexus.cli.exit_codes import ExitCode
from nexus.cli.output import (
    OutputOptions,
    _auto_json,
    _exception_to_error_code,
    _filter_fields,
    render_error,
    render_output,
)
from nexus.cli.timing import CommandTiming


class TestOutputOptions:
    """OutputOptions is a frozen dataclass."""

    def test_creation(self) -> None:
        opts = OutputOptions(
            json_output=True,
            quiet=False,
            verbosity=1,
            fields="path,size",
            request_id="abc123",
        )
        assert opts.json_output is True
        assert opts.quiet is False
        assert opts.verbosity == 1
        assert opts.fields == "path,size"
        assert opts.request_id == "abc123"

    def test_frozen(self) -> None:
        opts = OutputOptions(
            json_output=True,
            quiet=False,
            verbosity=0,
            fields=None,
            request_id="abc",
        )
        with pytest.raises(AttributeError):
            opts.json_output = False


class TestAutoJson:
    """TTY auto-detection for JSON output."""

    def test_auto_json_when_not_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_NO_AUTO_JSON", raising=False)
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            assert _auto_json() is True

    def test_no_auto_json_when_tty(self) -> None:
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = True
            assert _auto_json() is False

    def test_no_auto_json_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_NO_AUTO_JSON", "1")
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            assert _auto_json() is False


class TestFilterFields:
    """Field filtering for JSON output."""

    def test_filter_dict(self) -> None:
        data = {"path": "/a", "size": 100, "content_id": "abc"}
        result = _filter_fields(data, "path,size")
        assert result == {"path": "/a", "size": 100}

    def test_filter_list_of_dicts(self) -> None:
        data = [
            {"path": "/a", "size": 100},
            {"path": "/b", "size": 200},
        ]
        result = _filter_fields(data, "path")
        assert result == [{"path": "/a"}, {"path": "/b"}]

    def test_filter_preserves_non_dict(self) -> None:
        assert _filter_fields("hello", "path") == "hello"
        assert _filter_fields(42, "path") == 42


class TestRenderOutput:
    """render_output dispatches to JSON or human formatters."""

    def _json_opts(self, verbosity: int = 0, fields: str | None = None) -> OutputOptions:
        return OutputOptions(
            json_output=True,
            quiet=False,
            verbosity=verbosity,
            fields=fields,
            request_id="test-req-id",
        )

    def _human_opts(self, verbosity: int = 0, quiet: bool = False) -> OutputOptions:
        return OutputOptions(
            json_output=False,
            quiet=quiet,
            verbosity=verbosity,
            fields=None,
            request_id="test-req-id",
        )

    def test_json_output_envelope(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._json_opts()
        render_output(data={"path": "/test"}, output_opts=opts)

        output = json.loads(capsys.readouterr().out)
        assert output["data"] == {"path": "/test"}

    def test_json_output_with_timing(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._json_opts()
        timing = CommandTiming()
        with timing.phase("server"):
            pass

        render_output(data=[], output_opts=opts, timing=timing)

        output = json.loads(capsys.readouterr().out)
        assert "_timing" in output
        assert "total_ms" in output["_timing"]

    def test_json_output_with_request_id_at_v3(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._json_opts(verbosity=3)
        render_output(data=[], output_opts=opts)

        output = json.loads(capsys.readouterr().out)
        assert output["_request_id"] == "test-req-id"

    def test_json_output_no_request_id_below_v3(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._json_opts(verbosity=2)
        render_output(data=[], output_opts=opts)

        output = json.loads(capsys.readouterr().out)
        assert "_request_id" not in output

    def test_json_output_with_field_filter(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._json_opts(fields="path")
        render_output(data={"path": "/a", "size": 100}, output_opts=opts)

        output = json.loads(capsys.readouterr().out)
        assert output["data"] == {"path": "/a"}

    def test_quiet_suppresses_human_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._human_opts(quiet=True)
        render_output(data={"path": "/a"}, output_opts=opts, message="should not appear")

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_quiet_does_not_suppress_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = OutputOptions(
            json_output=True,
            quiet=True,
            verbosity=0,
            fields=None,
            request_id="test",
        )
        render_output(data={"path": "/a"}, output_opts=opts)

        output = json.loads(capsys.readouterr().out)
        assert output["data"] == {"path": "/a"}

    def test_human_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._human_opts()
        render_output(data=None, output_opts=opts, message="No files found")

        assert "No files found" in capsys.readouterr().out

    def test_human_formatter_called(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._human_opts()
        called_with = []

        def formatter(data: dict) -> None:
            called_with.append(data)
            print(f"formatted: {data['path']}")

        render_output(data={"path": "/x"}, output_opts=opts, human_formatter=formatter)

        assert called_with == [{"path": "/x"}]
        assert "formatted: /x" in capsys.readouterr().out

    def test_human_timing_on_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._human_opts(verbosity=1)
        timing = CommandTiming()
        with timing.phase("server"):
            pass

        render_output(data=None, output_opts=opts, message="done", timing=timing)

        captured = capsys.readouterr()
        assert "ms total" in captured.err


class TestRenderError:
    """render_error outputs structured errors and exits."""

    def _json_opts(self, verbosity: int = 0) -> OutputOptions:
        return OutputOptions(
            json_output=True,
            quiet=False,
            verbosity=verbosity,
            fields=None,
            request_id="err-req-id",
        )

    def test_json_error_envelope(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._json_opts()
        with pytest.raises(SystemExit) as exc_info:
            render_error(error=FileNotFoundError("no such file"), output_opts=opts)

        output = json.loads(capsys.readouterr().out)
        assert output["data"] is None
        assert output["error"]["message"] == "no such file"
        assert output["error"]["type"] == "FileNotFoundError"
        assert exc_info.value.code == ExitCode.GENERAL_ERROR

    def test_json_error_with_timing(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._json_opts()
        timing = CommandTiming()
        with timing.phase("server"):
            pass

        with pytest.raises(SystemExit):
            render_error(error=ValueError("bad"), output_opts=opts, timing=timing)

        output = json.loads(capsys.readouterr().out)
        assert "_timing" in output

    def test_json_error_with_request_id_v3(self, capsys: pytest.CaptureFixture[str]) -> None:
        opts = self._json_opts(verbosity=3)
        with pytest.raises(SystemExit):
            render_error(error=ValueError("x"), output_opts=opts)

        output = json.loads(capsys.readouterr().out)
        assert output["_request_id"] == "err-req-id"

    def test_human_error_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            render_error(error=RuntimeError("boom"), output_opts=None)

        captured = capsys.readouterr()
        assert "boom" in captured.err
        assert exc_info.value.code == ExitCode.GENERAL_ERROR

    def test_custom_exit_code(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            render_error(
                error=RuntimeError("x"),
                exit_code=ExitCode.NOT_FOUND,
            )
        assert exc_info.value.code == ExitCode.NOT_FOUND


class TestExceptionToErrorCode:
    """Maps exception types to machine-readable error codes."""

    def test_not_found(self) -> None:
        from nexus.contracts.exceptions import NexusFileNotFoundError

        assert _exception_to_error_code(NexusFileNotFoundError("/a")) == "NOT_FOUND"

    def test_permission_denied(self) -> None:
        assert _exception_to_error_code(PermissionError("denied")) == "PERMISSION_DENIED"

    def test_connection_error(self) -> None:
        assert _exception_to_error_code(ConnectionError("refused")) == "CONNECTION_ERROR"

    def test_timeout(self) -> None:
        assert _exception_to_error_code(TimeoutError("slow")) == "TIMEOUT"

    def test_generic_exception(self) -> None:
        assert _exception_to_error_code(RuntimeError("oops")) == "INTERNAL_ERROR"
