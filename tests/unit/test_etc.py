"""Tests for nexus.etc — conf.d loader utility."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.etc import get_brick_config, load_toml_file, resolve_etc_dir, resolve_state_dir


class TestResolveStateDir:
    """Tests for resolve_state_dir()."""

    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_STATE_DIR", raising=False)
        result = resolve_state_dir()
        assert result == Path.home() / ".nexus"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_STATE_DIR", "/tmp/custom-nexus")
        result = resolve_state_dir()
        assert result == Path("/tmp/custom-nexus")

    def test_tilde_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_STATE_DIR", "~/my-nexus")
        result = resolve_state_dir()
        assert result == Path.home() / "my-nexus"


class TestResolveEtcDir:
    """Tests for resolve_etc_dir()."""

    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_STATE_DIR", raising=False)
        result = resolve_etc_dir()
        assert result == Path.home() / ".nexus" / "etc"

    def test_explicit_state_dir(self) -> None:
        result = resolve_etc_dir("/tmp/test-nexus")
        assert result == Path("/tmp/test-nexus/etc")


class TestLoadTomlFile:
    """Tests for load_toml_file()."""

    def test_valid_toml(self, tmp_path: Path) -> None:
        f = tmp_path / "test.toml"
        f.write_text('key = "value"\ncount = 42\n')
        result = load_toml_file(f)
        assert result == {"key": "value", "count": 42}

    def test_missing_file(self, tmp_path: Path) -> None:
        result = load_toml_file(tmp_path / "nonexistent")
        assert result == {}

    def test_malformed_toml(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.toml"
        f.write_text("this is not valid toml [[[")
        result = load_toml_file(f)
        assert result == {}

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.toml"
        f.write_text("")
        result = load_toml_file(f)
        assert result == {}

    def test_comments_only(self, tmp_path: Path) -> None:
        f = tmp_path / "comments.toml"
        f.write_text("# This is a comment\n# Another comment\n")
        result = load_toml_file(f)
        assert result == {}

    def test_nested_tables(self, tmp_path: Path) -> None:
        f = tmp_path / "nested.toml"
        f.write_text('[section]\nkey = "value"\n')
        result = load_toml_file(f)
        assert result == {"section": {"key": "value"}}


class TestGetBrickConfig:
    """Tests for get_brick_config()."""

    def test_existing_brick(self, tmp_path: Path) -> None:
        confd = tmp_path / "etc" / "conf.d"
        confd.mkdir(parents=True)
        (confd / "cache").write_text('backend = "dragonfly"\nttl = 300\n')
        result = get_brick_config("cache", state_dir=tmp_path)
        assert result == {"backend": "dragonfly", "ttl": 300}

    def test_missing_brick(self, tmp_path: Path) -> None:
        confd = tmp_path / "etc" / "conf.d"
        confd.mkdir(parents=True)
        result = get_brick_config("nonexistent", state_dir=tmp_path)
        assert result == {}

    def test_missing_confd_dir(self, tmp_path: Path) -> None:
        result = get_brick_config("cache", state_dir=tmp_path)
        assert result == {}

    def test_brick_with_secrets(self, tmp_path: Path) -> None:
        confd = tmp_path / "etc" / "conf.d"
        confd.mkdir(parents=True)
        (confd / "llm").write_text('api_key = "sk-test"\nmodel = "gpt-4"\n')
        result = get_brick_config("llm", state_dir=tmp_path)
        assert result["api_key"] == "sk-test"
        assert result["model"] == "gpt-4"

    def test_default_confd_files_are_comments_only(self) -> None:
        """Repo-shipped etc/conf.d/ files should parse as empty (all commented)."""
        repo_confd = Path(__file__).resolve().parents[2] / "etc" / "conf.d"
        if not repo_confd.is_dir():
            pytest.skip("repo etc/conf.d/ not found")
        for f in repo_confd.iterdir():
            if f.name.startswith("."):
                continue
            result = load_toml_file(f)
            assert result == {}, f"Expected empty config for default {f.name}, got {result}"
