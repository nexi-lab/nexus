"""Tests for nexus.cli.utils — BackendConfig, get_zone_id, parse_subject, handle_error."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import nexus
from nexus.cli.utils import (
    BackendConfig,
    _apply_common_config,
    create_operation_context,
    get_zone_id,
    handle_error,
    parse_subject,
)

# ---------------------------------------------------------------------------
# BackendConfig
# ---------------------------------------------------------------------------


class TestBackendConfig:
    def test_defaults(self) -> None:
        config = BackendConfig()
        assert config.backend == "local"
        assert config.data_dir == str(Path(nexus.NEXUS_STATE_DIR) / "data")
        assert config.config_path is None
        assert config.remote_url is None
        assert config.remote_api_key is None
        assert config.gcs_bucket is None

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
    def test_applies_all_defaults(self) -> None:
        d: dict[str, Any] = {}
        result = _apply_common_config(d)
        assert result["enable_memory_paging"] is True
        assert result["memory_main_capacity"] == 100
        assert result["memory_recall_max_age_hours"] == 24.0
        assert "enforce_permissions" not in result
        assert "allow_admin_bypass" not in result
        assert "enforce_zone_isolation" not in result

    def test_applies_optional_params_when_set(self) -> None:
        d: dict[str, Any] = {}
        result = _apply_common_config(
            d,
            enforce_permissions=True,
            allow_admin_bypass=False,
            enforce_zone_isolation=True,
        )
        assert result["enforce_permissions"] is True
        assert result["allow_admin_bypass"] is False
        assert result["enforce_zone_isolation"] is True

    def test_mutates_input_dict(self) -> None:
        d: dict[str, Any] = {"mode": "standalone"}
        result = _apply_common_config(d)
        assert result is d  # Same object
        assert "enable_memory_paging" in d

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
# handle_error
# ---------------------------------------------------------------------------


class TestHandleError:
    def test_permission_error_exits_3(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            handle_error(PermissionError("denied"))
        assert exc_info.value.code == 3

    def test_not_found_exits_2(self) -> None:
        from nexus.contracts.exceptions import NexusFileNotFoundError

        with pytest.raises(SystemExit) as exc_info:
            handle_error(NexusFileNotFoundError("/missing"))
        assert exc_info.value.code == 2

    def test_validation_error_exits_1(self) -> None:
        from nexus.contracts.exceptions import ValidationError

        with pytest.raises(SystemExit) as exc_info:
            handle_error(ValidationError("bad input"))
        assert exc_info.value.code == 1

    def test_nexus_error_exits_1(self) -> None:
        from nexus.contracts.exceptions import NexusError

        with pytest.raises(SystemExit) as exc_info:
            handle_error(NexusError("something broke"))
        assert exc_info.value.code == 1

    def test_unexpected_error_exits_1(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            handle_error(RuntimeError("oops"))
        assert exc_info.value.code == 1
