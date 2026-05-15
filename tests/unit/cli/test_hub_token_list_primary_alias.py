"""token_list deprecated `zone` field equals get_primary_zone (#3871)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.cli.commands.hub import hub
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def db_with_keys(tmp_path, monkeypatch):
    db_path = tmp_path / "hub.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        create_api_key(s, user_id="u1", name="alice", zones=["eng", "ops"])
        s.commit()
    monkeypatch.setenv("NEXUS_DATABASE_URL", db_url)
    return SessionLocal


def test_token_list_json_zone_field_equals_primary(db_with_keys):
    runner = CliRunner()
    result = runner.invoke(hub, ["token", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    alice = next(r for r in payload["tokens"] if r["name"] == "alice")
    assert alice["zone"] == "eng"  # primary by granted_at, NOT None


def test_token_list_json_zone_field_is_none_for_zoneless_admin_key(db_with_keys):
    """Zoneless admin keys legitimately have no primary; emit None, not crash."""
    SessionLocal = db_with_keys
    with SessionLocal() as s:
        create_api_key(s, user_id="u1", name="root", is_admin=True)
        s.commit()
    runner = CliRunner()
    result = runner.invoke(hub, ["token", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    root_row = next(r for r in payload["tokens"] if r["name"] == "root")
    assert root_row["zone"] is None
