"""Tests for nexus.cli.state — runtime state and config management."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from nexus.cli.state import (
    STATE_FILENAME,
    load_project_config,
    load_project_config_optional,
    load_runtime_state,
    resolve_connection_env,
    save_project_config,
    save_runtime_state,
)

# ---------------------------------------------------------------------------
# load_project_config / save_project_config
# ---------------------------------------------------------------------------


class TestProjectConfig:
    def test_load_existing(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "nexus.yaml"
        cfg_path.write_text(yaml.dump({"preset": "shared"}))
        with patch("nexus.cli.state.CONFIG_SEARCH_PATHS", (str(cfg_path),)):
            config = load_project_config()
        assert config["preset"] == "shared"

    def test_load_missing_exits(self, tmp_path: Path) -> None:
        with (
            patch("nexus.cli.state.CONFIG_SEARCH_PATHS", (str(tmp_path / "nope.yaml"),)),
            pytest.raises(SystemExit),
        ):
            load_project_config()

    def test_load_optional_missing(self, tmp_path: Path) -> None:
        with patch("nexus.cli.state.CONFIG_SEARCH_PATHS", (str(tmp_path / "nope.yaml"),)):
            config = load_project_config_optional()
        assert config == {}

    def test_load_optional_existing(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "nexus.yaml"
        cfg_path.write_text(yaml.dump({"preset": "demo"}))
        with patch("nexus.cli.state.CONFIG_SEARCH_PATHS", (str(cfg_path),)):
            config = load_project_config_optional()
        assert config["preset"] == "demo"

    def test_save_roundtrip(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "nexus.yaml"
        cfg_path.write_text(yaml.dump({"preset": "shared"}))
        with patch("nexus.cli.state.CONFIG_SEARCH_PATHS", (str(cfg_path),)):
            save_project_config({"preset": "demo", "auth": "database"})
        with open(cfg_path) as f:
            saved = yaml.safe_load(f)
        assert saved["preset"] == "demo"
        assert saved["auth"] == "database"


# ---------------------------------------------------------------------------
# load_runtime_state / save_runtime_state
# ---------------------------------------------------------------------------


class TestRuntimeState:
    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        assert load_runtime_state(tmp_path) == {}

    def test_save_and_load(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "nexus-data"
        data_dir.mkdir()
        state = {
            "ports": {"http": 2027, "grpc": 2029},
            "api_key": "sk-test123",
            "build_mode": "local",
            "image_used": "nexus:local-abc12345",
        }
        save_runtime_state(data_dir, state)

        loaded = load_runtime_state(data_dir)
        assert loaded["ports"]["http"] == 2027
        assert loaded["ports"]["grpc"] == 2029
        assert loaded["api_key"] == "sk-test123"
        assert loaded["build_mode"] == "local"
        assert loaded["version"] == 1
        assert "started_at" in loaded

    def test_save_creates_data_dir(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "new-data"
        save_runtime_state(data_dir, {"ports": {"http": 2026}})
        assert data_dir.exists()
        assert (data_dir / STATE_FILENAME).exists()

    def test_save_preserves_started_at(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        save_runtime_state(data_dir, {"started_at": "2026-01-01T00:00:00"})
        loaded = load_runtime_state(data_dir)
        assert loaded["started_at"] == "2026-01-01T00:00:00"

    def test_load_malformed_returns_empty(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / STATE_FILENAME).write_text("not json{{{")
        assert load_runtime_state(data_dir) == {}

    def test_load_non_dict_returns_empty(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / STATE_FILENAME).write_text('"just a string"')
        assert load_runtime_state(data_dir) == {}

    def test_atomic_write(self, tmp_path: Path) -> None:
        """No .tmp files left behind after successful write."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        save_runtime_state(data_dir, {"test": True})
        tmp_files = list(data_dir.glob("*.tmp"))
        assert tmp_files == []


# ---------------------------------------------------------------------------
# resolve_connection_env
# ---------------------------------------------------------------------------


class TestResolveConnectionEnv:
    def test_basic_env(self) -> None:
        config = {
            "ports": {"http": 2026, "grpc": 2028, "dragonfly": 6379},
            "api_key": "sk-fromconfig",
            "services": ["nexus", "postgres", "dragonfly"],
        }
        env = resolve_connection_env(config, state={})
        assert env["NEXUS_URL"] == "http://localhost:2026"
        assert env["NEXUS_API_KEY"] == "sk-fromconfig"
        assert env["NEXUS_GRPC_HOST"] == "localhost:2028"
        assert env["NEXUS_GRPC_PORT"] == "2028"
        assert env["NEXUS_DRAGONFLY_URL"] == "redis://localhost:6379"
        assert "DATABASE_URL" in env
        assert "5432" in env["DATABASE_URL"]

    def test_state_overrides_config(self) -> None:
        config = {
            "ports": {"http": 2026, "grpc": 2028},
            "api_key": "sk-old",
            "services": [],
        }
        state = {
            "ports": {"http": 3026, "grpc": 3028},
            "api_key": "sk-new",
        }
        env = resolve_connection_env(config, state)
        assert env["NEXUS_URL"] == "http://localhost:3026"
        assert env["NEXUS_API_KEY"] == "sk-new"
        assert env["NEXUS_GRPC_PORT"] == "3028"

    def test_tls_from_state(self) -> None:
        config: dict[str, Any] = {"ports": {}, "services": []}
        state = {
            "tls": {
                "cert": "/data/tls/node.pem",
                "key": "/data/tls/node-key.pem",
                "ca": "/data/tls/ca.pem",
            }
        }
        env = resolve_connection_env(config, state)
        # NEXUS_URL is always http (TLS is gRPC-only, not HTTP)
        assert env["NEXUS_URL"].startswith("http://")
        assert env["NEXUS_TLS_CERT"] == "/data/tls/node.pem"
        assert env["NEXUS_TLS_CA"] == "/data/tls/ca.pem"

    def test_tls_from_config_fallback(self) -> None:
        config = {
            "ports": {},
            "services": [],
            "tls": True,
            "tls_cert": "/data/tls/server.crt",
            "tls_key": "/data/tls/server.key",
            "tls_ca": "/data/tls/ca.crt",
        }
        env = resolve_connection_env(config, state={})
        assert env["NEXUS_TLS_CERT"] == "/data/tls/server.crt"

    def test_no_database_url_without_postgres(self) -> None:
        config = {"ports": {}, "services": ["nexus"]}
        env = resolve_connection_env(config, state={})
        assert "DATABASE_URL" not in env

    def test_no_dragonfly_url_without_dragonfly(self) -> None:
        config = {"ports": {}, "services": ["nexus", "postgres"]}
        env = resolve_connection_env(config, state={})
        assert "NEXUS_DRAGONFLY_URL" not in env

    def test_no_api_key_when_empty(self) -> None:
        config: dict[str, Any] = {"ports": {}, "services": []}
        env = resolve_connection_env(config, state={})
        assert "NEXUS_API_KEY" not in env
