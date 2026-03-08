"""Tests for nexus.cli.utils — BackendConfig, get_zone_id, parse_subject, handle_error."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import nexus
from nexus.cli.exit_codes import ExitCode
from nexus.cli.utils import (
    BackendConfig,
    _apply_common_config,
    create_operation_context,
    get_zone_id,
    handle_error,
    parse_subject,
    resolve_content,
)

# ---------------------------------------------------------------------------
# BackendConfig
# ---------------------------------------------------------------------------


class TestBackendConfig:
    """BackendConfig is a frozen dataclass."""

    def test_defaults(self) -> None:
        config = BackendConfig()
        assert config.backend == "local"
        assert config.data_dir == str(Path(nexus.NEXUS_STATE_DIR) / "data")
        assert config.config_path is None
        assert config.remote_url is None
        assert config.remote_api_key is None
        assert config.gcs_bucket is None

    def test_frozen(self) -> None:
        config = BackendConfig()
        with pytest.raises(AttributeError):
            config.backend = "gcs"

    def test_all_params(self) -> None:
        config = BackendConfig(
            backend="gcs",
            data_dir="/custom",
            config_path="/etc/nexus.yaml",
            gcs_bucket="my-bucket",
            gcs_project="my-project",
            gcs_credentials="/creds.json",
            remote_url="http://localhost:2026",
            remote_api_key="nx_test_key",
        )
        assert config.backend == "gcs"
        assert config.data_dir == "/custom"
        assert config.config_path == "/etc/nexus.yaml"
        assert config.gcs_bucket == "my-bucket"
        assert config.gcs_project == "my-project"
        assert config.gcs_credentials == "/creds.json"
        assert config.remote_url == "http://localhost:2026"
        assert config.remote_api_key == "nx_test_key"

    def test_none_vs_empty_string(self) -> None:
        config = BackendConfig(remote_url="", remote_api_key=None)
        assert config.remote_url == ""
        assert config.remote_api_key is None


# ---------------------------------------------------------------------------
# _apply_common_config
# ---------------------------------------------------------------------------


class TestApplyCommonConfig:
    def test_applies_optional_params_when_set(self) -> None:
        d: dict[str, Any] = {}
        _apply_common_config(
            d,
            enforce_permissions=True,
            allow_admin_bypass=False,
            enforce_zone_isolation=True,
        )
        assert d["enforce_permissions"] is True
        assert d["allow_admin_bypass"] is False
        assert d["enforce_zone_isolation"] is True

    def test_skips_unset_optional_params(self) -> None:
        d: dict[str, Any] = {}
        _apply_common_config(d)
        assert "enforce_permissions" not in d
        assert "allow_admin_bypass" not in d
        assert "enforce_zone_isolation" not in d

    def test_mutates_input_dict(self) -> None:
        d: dict[str, Any] = {"profile": "standalone"}
        _apply_common_config(d)
        assert d["profile"] == "standalone"  # Original key preserved

    def test_custom_memory_settings(self) -> None:
        d: dict[str, Any] = {}
        _apply_common_config(d, memory_main_capacity=50, memory_recall_max_age_hours=12.0)
        assert d["memory_main_capacity"] == 50
        assert d["memory_recall_max_age_hours"] == 12.0


# ---------------------------------------------------------------------------
# get_zone_id
# ---------------------------------------------------------------------------


class TestGetZoneId:
    def test_returns_param_when_set(self) -> None:
        assert get_zone_id("my-zone") == "my-zone"

    def test_returns_none_when_not_set(self) -> None:
        assert get_zone_id(None) is None

    def test_empty_string_returns_none(self) -> None:
        # Empty string is falsy, so get_zone_id returns None
        assert get_zone_id("") is None


# ---------------------------------------------------------------------------
# parse_subject
# ---------------------------------------------------------------------------


class TestParseSubject:
    def test_valid_subject(self) -> None:
        result = parse_subject("user:alice")
        assert result == ("user", "alice")

    def test_subject_with_colon_in_id(self) -> None:
        result = parse_subject("agent:ns:bot1")
        assert result == ("agent", "ns:bot1")

    def test_none_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_SUBJECT", raising=False)
        monkeypatch.delenv("NEXUS_SUBJECT_TYPE", raising=False)
        monkeypatch.delenv("NEXUS_SUBJECT_ID", raising=False)
        result = parse_subject(None)
        assert result is None

    def test_invalid_format_exits(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            parse_subject("no_colon")
        assert exc_info.value.code == 1

    def test_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_SUBJECT", "user:bob")
        result = parse_subject(None)
        assert result == ("user", "bob")

    def test_env_var_type_id_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_SUBJECT", raising=False)
        monkeypatch.setenv("NEXUS_SUBJECT_TYPE", "agent")
        monkeypatch.setenv("NEXUS_SUBJECT_ID", "bot1")
        result = parse_subject(None)
        assert result == ("agent", "bot1")


# ---------------------------------------------------------------------------
# create_operation_context
# ---------------------------------------------------------------------------


class TestCreateOperationContext:
    def test_empty_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_SUBJECT", raising=False)
        monkeypatch.delenv("NEXUS_SUBJECT_TYPE", raising=False)
        monkeypatch.delenv("NEXUS_SUBJECT_ID", raising=False)
        monkeypatch.delenv("NEXUS_ZONE_ID", raising=False)
        ctx = create_operation_context()
        assert ctx == {}

    def test_full_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_SUBJECT", raising=False)
        monkeypatch.delenv("NEXUS_SUBJECT_TYPE", raising=False)
        monkeypatch.delenv("NEXUS_ZONE_ID", raising=False)
        ctx = create_operation_context(
            subject="user:alice",
            zone_id="z1",
            is_admin=True,
            is_system=True,
            admin_capabilities=("admin:read:*",),
        )
        assert ctx["subject"] == ("user", "alice")
        assert ctx["zone"] == "z1"
        assert ctx["is_admin"] is True
        assert ctx["is_system"] is True
        assert ctx["admin_capabilities"] == {"admin:read:*"}

    def test_only_subject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_SUBJECT", raising=False)
        monkeypatch.delenv("NEXUS_SUBJECT_TYPE", raising=False)
        monkeypatch.delenv("NEXUS_ZONE_ID", raising=False)
        ctx = create_operation_context(subject="agent:bot")
        assert ctx == {"subject": ("agent", "bot")}


# ---------------------------------------------------------------------------
# resolve_content
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# handle_error
# ---------------------------------------------------------------------------


class TestHandleError:
    """handle_error maps exceptions to semantic exit codes."""

    def _assert_exit_code(self, exc: Exception, expected_code: int) -> None:
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
