"""Tests for nexus.cli.config — profile management and connection resolution."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from nexus.cli.config import (
    NexusCliConfig,
    ProfileEntry,
    ResolvedConnection,
    _coerce_value,
    get_setting,
    load_cli_config,
    reset_setting,
    resolve_connection,
    save_cli_config,
    set_setting,
)
from nexus.config import _load_from_environment
from tests.unit.cli.conftest import make_config

# ---------------------------------------------------------------------------
# ProfileEntry
# ---------------------------------------------------------------------------


class TestProfileEntry:
    def test_from_dict_full(self) -> None:
        entry = ProfileEntry.from_dict(
            {
                "url": "http://localhost:2026",
                "api-key": "nx_test_abc",
                "zone-id": "us-west-1",
            }
        )
        assert entry.url == "http://localhost:2026"
        assert entry.api_key == "nx_test_abc"
        assert entry.zone_id == "us-west-1"

    def test_from_dict_minimal(self) -> None:
        entry = ProfileEntry.from_dict({})
        assert entry.url is None
        assert entry.api_key is None
        assert entry.zone_id is None

    def test_to_dict_roundtrip(self) -> None:
        original = ProfileEntry(url="http://x", api_key="key", zone_id="z1")
        restored = ProfileEntry.from_dict(original.to_dict())
        assert restored == original

    def test_to_dict_omits_none(self) -> None:
        entry = ProfileEntry(url="http://x")
        d = entry.to_dict()
        assert "url" in d
        assert "api-key" not in d
        assert "zone-id" not in d

    def test_frozen(self) -> None:
        entry = ProfileEntry(url="http://x")
        with pytest.raises(AttributeError):
            entry.url = "http://y"


# ---------------------------------------------------------------------------
# NexusCliConfig
# ---------------------------------------------------------------------------


class TestNexusCliConfig:
    def test_from_dict_empty(self) -> None:
        config = NexusCliConfig.from_dict({})
        assert config.current_profile is None
        assert config.profiles == {}
        assert config.settings == {}

    def test_from_dict_full(self) -> None:
        config = NexusCliConfig.from_dict(
            {
                "current-profile": "prod",
                "profiles": {
                    "prod": {"url": "http://prod", "api-key": "k1"},
                    "dev": {"url": "http://dev"},
                },
                "settings": {"output": {"format": "json"}},
            }
        )
        assert config.current_profile == "prod"
        assert len(config.profiles) == 2
        assert config.profiles["prod"].api_key == "k1"
        assert config.settings == {"output": {"format": "json"}}

    def test_to_dict_roundtrip(self, sample_config: NexusCliConfig) -> None:
        d = sample_config.to_dict()
        restored = NexusCliConfig.from_dict(d)
        assert restored.current_profile == sample_config.current_profile
        assert len(restored.profiles) == len(sample_config.profiles)

    def test_from_dict_ignores_invalid_profiles(self) -> None:
        config = NexusCliConfig.from_dict(
            {
                "profiles": {
                    "good": {"url": "http://x"},
                    "bad": "not-a-dict",
                },
            }
        )
        assert "good" in config.profiles
        assert "bad" not in config.profiles

    def test_from_dict_invalid_settings(self) -> None:
        config = NexusCliConfig.from_dict({"settings": "not-a-dict"})
        assert config.settings == {}


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


class TestFileIO:
    def test_save_and_load(self, tmp_config_file: Path) -> None:
        config = make_config(
            current_profile="dev",
            profiles={"dev": {"url": "http://localhost:2026", "api_key": "key123"}},
        )
        save_cli_config(config, path=tmp_config_file)

        loaded = load_cli_config(path=tmp_config_file)
        assert loaded.current_profile == "dev"
        assert loaded.profiles["dev"].url == "http://localhost:2026"
        assert loaded.profiles["dev"].api_key == "key123"

    def test_load_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        config = load_cli_config(path=tmp_path / "does-not-exist.yaml")
        assert config.current_profile is None
        assert config.profiles == {}

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "config.yaml"
        config = make_config(current_profile="x", profiles={"x": {"url": "http://x"}})
        save_cli_config(config, path=nested)
        assert nested.exists()

    def test_save_sets_file_permissions(self, tmp_config_file: Path) -> None:
        config = make_config()
        save_cli_config(config, path=tmp_config_file)
        mode = tmp_config_file.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_load_empty_file(self, tmp_config_file: Path) -> None:
        tmp_config_file.write_text("")
        config = load_cli_config(path=tmp_config_file)
        assert config.current_profile is None

    def test_load_invalid_yaml_type(self, tmp_config_file: Path) -> None:
        tmp_config_file.write_text("just a string")
        config = load_cli_config(path=tmp_config_file)
        assert config.current_profile is None


# ---------------------------------------------------------------------------
# resolve_connection — precedence matrix
# ---------------------------------------------------------------------------


class TestResolveConnection:
    """Parametrized tests for the full precedence chain."""

    def test_explicit_url_wins_over_everything(self) -> None:
        config = make_config(
            current_profile="prod",
            profiles={"prod": {"url": "http://prod-from-profile"}},
        )
        resolved = resolve_connection(
            remote_url="http://explicit-flag",
            remote_api_key="flag-key",
            profile_name="prod",
            config=config,
        )
        assert resolved.url == "http://explicit-flag"
        assert resolved.api_key == "flag-key"
        assert "flag" in resolved.source.lower() or "env" in resolved.source.lower()

    def test_profile_flag_wins_over_current_profile(self) -> None:
        config = make_config(
            current_profile="default-profile",
            profiles={
                "default-profile": {"url": "http://default"},
                "staging": {"url": "http://staging"},
            },
        )
        resolved = resolve_connection(profile_name="staging", config=config)
        assert resolved.url == "http://staging"
        assert "staging" in resolved.source

    def test_current_profile_used_when_no_flags(self) -> None:
        config = make_config(
            current_profile="prod",
            profiles={"prod": {"url": "http://prod", "api_key": "k1", "zone_id": "z1"}},
        )
        resolved = resolve_connection(config=config)
        assert resolved.url == "http://prod"
        assert resolved.api_key == "k1"
        assert resolved.zone_id == "z1"
        assert "current-profile" in resolved.source

    def test_local_default_when_nothing_set(self) -> None:
        resolved = resolve_connection(config=NexusCliConfig())
        assert resolved.url is None
        assert resolved.api_key is None
        assert not resolved.is_remote
        assert "local" in resolved.source.lower()

    def test_whitespace_url_treated_as_none(self) -> None:
        resolved = resolve_connection(remote_url="   ", config=NexusCliConfig())
        assert resolved.url is None
        assert not resolved.is_remote

    def test_whitespace_api_key_treated_as_none(self) -> None:
        resolved = resolve_connection(
            remote_url="http://x",
            remote_api_key="   ",
            config=NexusCliConfig(),
        )
        assert resolved.api_key is None

    def test_unknown_profile_warns_and_falls_back(self, capsys: pytest.CaptureFixture[str]) -> None:
        config = make_config(profiles={"real": {"url": "http://real"}})
        resolved = resolve_connection(profile_name="nonexistent", config=config)
        assert not resolved.is_remote
        assert "local" in resolved.source.lower()

    def test_zone_id_from_flag_overrides_profile(self) -> None:
        config = make_config(
            current_profile="prod",
            profiles={"prod": {"url": "http://prod", "zone_id": "profile-zone"}},
        )
        resolved = resolve_connection(zone_id="flag-zone", config=config)
        assert resolved.zone_id == "flag-zone"

    def test_zone_id_from_profile_when_no_flag(self) -> None:
        config = make_config(
            current_profile="prod",
            profiles={"prod": {"url": "http://prod", "zone_id": "profile-zone"}},
        )
        resolved = resolve_connection(config=config)
        assert resolved.zone_id == "profile-zone"

    @pytest.mark.parametrize(
        (
            "remote_url",
            "profile_name",
            "current_profile",
            "profiles",
            "expected_url",
            "expected_source_contains",
        ),
        [
            # Precedence 1: Explicit URL always wins
            ("http://flag", None, "prod", {"prod": {"url": "http://prod"}}, "http://flag", "flag"),
            # Precedence 2: --profile flag
            (
                None,
                "staging",
                "prod",
                {"prod": {"url": "http://prod"}, "staging": {"url": "http://staging"}},
                "http://staging",
                "staging",
            ),
            # Precedence 3: current-profile from config
            (
                None,
                None,
                "prod",
                {"prod": {"url": "http://prod"}},
                "http://prod",
                "current-profile",
            ),
            # Precedence 4: No URL → local
            (None, None, None, {}, None, "local"),
            # Env var URL (mapped to remote_url by Click)
            (
                "http://from-env",
                None,
                "prod",
                {"prod": {"url": "http://prod"}},
                "http://from-env",
                "flag",
            ),
            # Profile with no URL → local-like
            (None, None, "local", {"local": {}}, None, "local"),
        ],
        ids=[
            "explicit-url-wins",
            "profile-flag-wins",
            "current-profile-used",
            "fallback-to-local",
            "env-var-as-url-wins",
            "profile-with-no-url",
        ],
    )
    def test_precedence_matrix(
        self,
        remote_url: str | None,
        profile_name: str | None,
        current_profile: str | None,
        profiles: dict[str, dict[str, str]],
        expected_url: str | None,
        expected_source_contains: str,
    ) -> None:
        config = make_config(current_profile=current_profile, profiles=profiles)
        resolved = resolve_connection(
            remote_url=remote_url,
            profile_name=profile_name,
            config=config,
        )
        assert resolved.url == expected_url
        assert expected_source_contains in resolved.source.lower()


# ---------------------------------------------------------------------------
# ResolvedConnection
# ---------------------------------------------------------------------------


class TestResolvedConnection:
    def test_is_remote_true(self) -> None:
        r = ResolvedConnection(url="http://x")
        assert r.is_remote is True

    def test_is_remote_false_none(self) -> None:
        r = ResolvedConnection(url=None)
        assert r.is_remote is False

    def test_is_remote_false_empty(self) -> None:
        r = ResolvedConnection(url="")
        assert r.is_remote is False

    def test_is_remote_false_whitespace(self) -> None:
        r = ResolvedConnection(url="   ")
        assert r.is_remote is False

    def test_frozen(self) -> None:
        r = ResolvedConnection(url="http://x")
        with pytest.raises(AttributeError):
            r.url = "http://y"


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


class TestSettings:
    def test_get_setting_nested(self) -> None:
        settings = {"output": {"format": "json"}}
        assert get_setting(settings, "output.format") == "json"

    def test_get_setting_top_level(self) -> None:
        settings = {"default-zone-id": "z1"}
        assert get_setting(settings, "default-zone-id") == "z1"

    def test_get_setting_missing_returns_default(self) -> None:
        assert get_setting({}, "output.format") == "table"

    def test_set_setting_creates_nested(self) -> None:
        result = set_setting({}, "output.format", "json")
        assert result == {"output": {"format": "json"}}

    def test_set_setting_immutable(self) -> None:
        original = {"output": {"format": "table"}}
        result = set_setting(original, "output.format", "json")
        assert original["output"]["format"] == "table"  # unchanged
        assert result["output"]["format"] == "json"

    def test_reset_setting(self) -> None:
        settings = {"output": {"format": "json"}}
        result = reset_setting(settings, "output.format")
        assert result["output"]["format"] == "table"

    def test_reset_nonexistent_key(self) -> None:
        result = reset_setting({}, "output.format")
        assert result == {"output": {"format": "table"}}


class TestCoerceValue:
    @pytest.mark.parametrize(
        ("input_val", "expected"),
        [
            ("true", True),
            ("True", True),
            ("yes", True),
            ("false", False),
            ("False", False),
            ("no", False),
            ("null", None),
            ("none", None),
            ("42", 42),
            ("3.14", 3.14),
            ("hello", "hello"),
        ],
    )
    def test_coerce(self, input_val: str, expected: object) -> None:
        assert _coerce_value(input_val) == expected

    def test_non_string_passthrough(self) -> None:
        assert _coerce_value(42) == 42
        assert _coerce_value(True) is True


# ---------------------------------------------------------------------------
# _load_from_environment — conditional provider registration
# ---------------------------------------------------------------------------


def test_load_from_environment_registers_pdf_inspector_when_available(monkeypatch):
    pytest.importorskip("pdf_inspector")
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    config = _load_from_environment()
    names = [p["name"] for p in config.parse_providers]
    assert "pdf-inspector" in names
    pdf = next(p for p in config.parse_providers if p["name"] == "pdf-inspector")
    assert pdf["priority"] == 20


def test_load_from_environment_skips_pdf_inspector_when_unavailable(monkeypatch):
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "pdf_inspector", None)

    config = _load_from_environment()
    names = [p["name"] for p in (config.parse_providers or [])]
    assert "pdf-inspector" not in names


def test_load_from_environment_skips_markitdown_when_unavailable(monkeypatch):
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "markitdown", None)

    config = _load_from_environment()
    names = [p["name"] for p in (config.parse_providers or [])]
    assert "markitdown" not in names
