"""Tests for MCP tool profile configuration (Issue #1272).

Tests cover:
- Profile data model
- Inheritance resolution (single-level, multi-level, cycles)
- YAML loading
- ReBAC grant generation and revocation
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from nexus.mcp.profiles import (
    TOOL_PATH_PREFIX,
    ProfileCycleError,
    ProfileNotFoundError,
    ToolProfile,
    ToolProfileConfig,
    grant_tools_for_profile,
    load_profiles,
    load_profiles_from_dict,
    resolve_inheritance,
    revoke_tools_by_tuple_ids,
)

# ---------------------------------------------------------------------------
# ToolProfile data model
# ---------------------------------------------------------------------------


class TestToolProfile:
    def test_create_minimal_profile(self):
        profile = ToolProfile(
            name="minimal",
            tools=frozenset(["nexus_read_file", "nexus_list_files"]),
        )
        assert profile.name == "minimal"
        assert len(profile.tools) == 2
        assert profile.extends is None
        assert profile.description == ""

    def test_tool_paths_returns_namespace_paths(self):
        profile = ToolProfile(
            name="test",
            tools=frozenset(["nexus_read_file", "nexus_write_file"]),
        )
        paths = profile.tool_paths()
        assert paths == frozenset(
            [
                "/tools/nexus_read_file",
                "/tools/nexus_write_file",
            ]
        )

    def test_empty_profile(self):
        profile = ToolProfile(name="empty", tools=frozenset())
        assert len(profile.tools) == 0
        assert profile.tool_paths() == frozenset()

    def test_profile_is_immutable(self):
        profile = ToolProfile(name="test", tools=frozenset(["a"]))
        with pytest.raises(AttributeError):
            profile.name = "changed"  # type: ignore[misc]


class TestToolProfileConfig:
    def test_get_profile(self):
        p = ToolProfile(name="minimal", tools=frozenset(["a"]))
        config = ToolProfileConfig(profiles={"minimal": p})
        assert config.get_profile("minimal") is p
        assert config.get_profile("nonexistent") is None

    def test_get_default(self):
        p = ToolProfile(name="minimal", tools=frozenset(["a"]))
        config = ToolProfileConfig(profiles={"minimal": p}, default_profile="minimal")
        assert config.get_default() is p

    def test_profile_names_sorted(self):
        profiles = {
            "z_profile": ToolProfile(name="z_profile", tools=frozenset()),
            "a_profile": ToolProfile(name="a_profile", tools=frozenset()),
            "m_profile": ToolProfile(name="m_profile", tools=frozenset()),
        }
        config = ToolProfileConfig(profiles=profiles)
        assert config.profile_names == ["a_profile", "m_profile", "z_profile"]


# ---------------------------------------------------------------------------
# Inheritance resolution
# ---------------------------------------------------------------------------


class TestResolveInheritance:
    def test_single_root_profile(self):
        raw = {
            "minimal": {
                "tools": ["read", "list"],
                "description": "Read-only",
            },
        }
        result = resolve_inheritance(raw)
        assert "minimal" in result
        assert result["minimal"].tools == frozenset(["read", "list"])
        assert result["minimal"].extends is None
        assert result["minimal"].description == "Read-only"

    def test_single_level_inheritance(self):
        raw = {
            "minimal": {"tools": ["read", "list"]},
            "coding": {"extends": "minimal", "tools": ["write", "edit"]},
        }
        result = resolve_inheritance(raw)
        assert result["minimal"].tools == frozenset(["read", "list"])
        assert result["coding"].tools == frozenset(["read", "list", "write", "edit"])
        assert result["coding"].extends == "minimal"

    def test_deep_inheritance_three_levels(self):
        raw = {
            "base": {"tools": ["a"]},
            "mid": {"extends": "base", "tools": ["b"]},
            "top": {"extends": "mid", "tools": ["c"]},
        }
        result = resolve_inheritance(raw)
        assert result["base"].tools == frozenset(["a"])
        assert result["mid"].tools == frozenset(["a", "b"])
        assert result["top"].tools == frozenset(["a", "b", "c"])

    def test_inheritance_deduplicates_tools(self):
        raw = {
            "base": {"tools": ["read", "write"]},
            "child": {"extends": "base", "tools": ["read", "delete"]},
        }
        result = resolve_inheritance(raw)
        assert result["child"].tools == frozenset(["read", "write", "delete"])

    def test_cycle_detection_raises_error(self):
        raw = {
            "a": {"extends": "b", "tools": ["x"]},
            "b": {"extends": "a", "tools": ["y"]},
        }
        with pytest.raises(ProfileCycleError, match="Cycle detected"):
            resolve_inheritance(raw)

    def test_self_cycle_detection(self):
        raw = {
            "a": {"extends": "a", "tools": ["x"]},
        }
        with pytest.raises(ProfileCycleError, match="Cycle detected"):
            resolve_inheritance(raw)

    def test_missing_parent_raises_error(self):
        raw = {
            "child": {"extends": "nonexistent", "tools": ["x"]},
        }
        with pytest.raises(ProfileNotFoundError, match="nonexistent"):
            resolve_inheritance(raw)

    def test_empty_tools_list(self):
        raw = {
            "empty": {"tools": []},
        }
        result = resolve_inheritance(raw)
        assert result["empty"].tools == frozenset()

    def test_no_tools_key_defaults_to_empty(self):
        raw = {
            "bare": {"description": "No tools key"},
        }
        result = resolve_inheritance(raw)
        assert result["bare"].tools == frozenset()

    def test_multiple_root_profiles(self):
        """Two independent profiles, no inheritance."""
        raw = {
            "read_only": {"tools": ["read"]},
            "write_only": {"tools": ["write"]},
        }
        result = resolve_inheritance(raw)
        assert result["read_only"].tools == frozenset(["read"])
        assert result["write_only"].tools == frozenset(["write"])


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


class TestLoadProfiles:
    def test_load_from_dict(self):
        raw = {
            "profiles": {
                "minimal": {"tools": ["read"]},
                "coding": {"extends": "minimal", "tools": ["write"]},
            },
            "default_profile": "coding",
        }
        config = load_profiles_from_dict(raw)
        assert config.default_profile == "coding"
        assert len(config.profiles) == 2
        assert config.profiles["coding"].tools == frozenset(["read", "write"])

    def test_load_from_dict_empty_profiles(self):
        raw = {"profiles": {}}
        config = load_profiles_from_dict(raw)
        assert len(config.profiles) == 0
        assert config.default_profile == "minimal"

    def test_load_from_dict_no_profiles_key(self):
        raw = {"something_else": True}
        config = load_profiles_from_dict(raw)
        assert len(config.profiles) == 0

    def test_load_from_yaml_file(self, tmp_path: Path):
        yaml_content = """\
profiles:
  minimal:
    description: "Read-only"
    tools:
      - nexus_read_file
      - nexus_list_files
  coding:
    extends: minimal
    description: "Coding tools"
    tools:
      - nexus_write_file
      - nexus_edit_file
default_profile: minimal
"""
        config_file = tmp_path / "tool_profiles.yaml"
        config_file.write_text(yaml_content)

        config = load_profiles(config_file)
        assert config.default_profile == "minimal"
        assert len(config.profiles) == 2
        assert "nexus_read_file" in config.profiles["coding"].tools
        assert "nexus_write_file" in config.profiles["coding"].tools

    def test_load_from_yaml_file_not_found(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(FileNotFoundError, match="not found"):
            load_profiles(missing)

    def test_load_from_yaml_malformed(self, tmp_path: Path):
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("not: [valid: yaml: {{")
        import yaml

        with pytest.raises(yaml.YAMLError):
            load_profiles(config_file)


# ---------------------------------------------------------------------------
# ReBAC grant generation
# ---------------------------------------------------------------------------


class TestGrantToolsForProfile:
    def _make_mock_rebac(self) -> MagicMock:
        rebac = MagicMock()
        # Each rebac_write returns a mock WriteResult
        write_result = MagicMock()
        write_result.tuple_id = "test-tuple-id"
        write_result.revision = 1
        rebac.rebac_write.return_value = write_result
        return rebac

    def test_grant_writes_correct_tuples(self):
        rebac = self._make_mock_rebac()
        profile = ToolProfile(
            name="test",
            tools=frozenset(["nexus_read_file", "nexus_write_file"]),
        )

        results = grant_tools_for_profile(
            rebac_manager=rebac,
            subject=("agent", "A"),
            profile=profile,
            zone_id="org_1",
        )

        assert len(results) == 2
        # Verify calls are sorted by tool name for determinism
        calls = rebac.rebac_write.call_args_list
        assert len(calls) == 2
        # First call: nexus_read_file (alphabetically first)
        assert calls[0] == call(
            subject=("agent", "A"),
            relation="direct_viewer",
            object=("file", "/tools/nexus_read_file"),
            zone_id="org_1",
        )
        # Second call: nexus_write_file
        assert calls[1] == call(
            subject=("agent", "A"),
            relation="direct_viewer",
            object=("file", "/tools/nexus_write_file"),
            zone_id="org_1",
        )

    def test_grant_empty_profile(self):
        rebac = self._make_mock_rebac()
        profile = ToolProfile(name="empty", tools=frozenset())

        results = grant_tools_for_profile(
            rebac_manager=rebac,
            subject=("agent", "A"),
            profile=profile,
        )

        assert results == []
        rebac.rebac_write.assert_not_called()

    def test_grant_without_zone_id(self):
        rebac = self._make_mock_rebac()
        profile = ToolProfile(
            name="test",
            tools=frozenset(["nexus_read_file"]),
        )

        results = grant_tools_for_profile(
            rebac_manager=rebac,
            subject=("user", "alice"),
            profile=profile,
        )

        assert len(results) == 1
        rebac.rebac_write.assert_called_once_with(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/tools/nexus_read_file"),
            zone_id=None,
        )


class TestRevokeToolsByTupleIds:
    def test_revoke_deletes_all_tuples(self):
        rebac = MagicMock()
        rebac.rebac_delete.return_value = True

        deleted = revoke_tools_by_tuple_ids(
            rebac_manager=rebac,
            tuple_ids=["id1", "id2", "id3"],
        )

        assert deleted == 3
        assert rebac.rebac_delete.call_count == 3

    def test_revoke_handles_already_deleted(self):
        rebac = MagicMock()
        # First succeeds, second already gone
        rebac.rebac_delete.side_effect = [True, False]

        deleted = revoke_tools_by_tuple_ids(
            rebac_manager=rebac,
            tuple_ids=["id1", "id2"],
        )

        assert deleted == 1

    def test_revoke_empty_list(self):
        rebac = MagicMock()

        deleted = revoke_tools_by_tuple_ids(
            rebac_manager=rebac,
            tuple_ids=[],
        )

        assert deleted == 0
        rebac.rebac_delete.assert_not_called()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_tool_path_prefix(self):
        assert TOOL_PATH_PREFIX == "/tools/"
