"""Tests for the ``database_url`` config field / NEXUS_DATABASE_URL env mapping.

Covers the UX fix where ``nexusd --database-url`` previously did not wire the
SQLAlchemy record store onto ``NexusFS`` (the flag was consumed only by
DatabaseAPIKeyAuth). Now the flag/env/config-key all route into
``cfg.database_url`` and are honored by ``nexus.connect()``.
"""

from __future__ import annotations

import pytest

from nexus.config import NexusConfig, _load_from_dict, _load_from_environment


class TestDatabaseUrlField:
    def test_default_is_none(self) -> None:
        cfg = NexusConfig()
        assert cfg.database_url is None

    def test_accepts_postgres_url(self) -> None:
        cfg = NexusConfig(database_url="postgresql://u:p@h/db")
        assert cfg.database_url == "postgresql://u:p@h/db"

    def test_accepts_sqlite_url(self) -> None:
        cfg = NexusConfig(database_url="sqlite:///tmp/x.db")
        assert cfg.database_url == "sqlite:///tmp/x.db"


class TestDatabaseUrlFromDict:
    def test_explicit_config_key_flows_through(self) -> None:
        cfg = _load_from_dict({"database_url": "sqlite:///tmp/from-dict.db"})
        assert cfg.database_url == "sqlite:///tmp/from-dict.db"

    def test_absent_key_stays_none(self) -> None:
        cfg = _load_from_dict({"profile": "cluster"})
        assert cfg.database_url is None


class TestDatabaseUrlFromEnv:
    def test_nexus_database_url_env_maps_to_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://e:e@h/e")
        cfg = _load_from_environment()
        assert cfg.database_url == "postgresql://e:e@h/e"

    def test_dict_key_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit config-dict wins over NEXUS_DATABASE_URL env."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://env:env@h/db")
        cfg = _load_from_dict({"database_url": "sqlite:///tmp/dict.db"})
        assert cfg.database_url == "sqlite:///tmp/dict.db"
