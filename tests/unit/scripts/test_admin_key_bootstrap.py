"""Admin API key bootstrap must preserve post-#3871 key invariants."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.storage.models import APIKeyModel, APIKeyZoneModel, Base, ZoneModel

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_script(name: str) -> Any:
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def db_url_and_factory(tmp_path):
    db_path = tmp_path / "nexus.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    with SessionFactory() as session:
        session.add(ZoneModel(zone_id="root", name="Root", phase="Active"))
        session.commit()
    return db_url, SessionFactory


def _auth_result(SessionFactory, raw_key: str):
    provider = DatabaseAPIKeyAuth(SimpleNamespace(session_factory=SessionFactory))
    return asyncio.run(provider.authenticate(raw_key))


def test_custom_admin_key_writes_junction_and_null_legacy_zone(db_url_and_factory, monkeypatch):
    monkeypatch.delenv("NEXUS_API_KEY_SECRET", raising=False)
    db_url, SessionFactory = db_url_and_factory
    create_admin_key = _load_script("create_admin_key")
    raw_key = "sk-e2e-custom-admin-key-for-bootstrap"

    returned, success = create_admin_key.create_admin_key(
        db_url,
        "admin",
        custom_key=raw_key,
        skip_permissions=True,
    )

    assert success is True
    assert returned == raw_key
    key_hash = DatabaseAPIKeyAuth._hash_key(raw_key)
    with SessionFactory() as session:
        row = session.scalar(select(APIKeyModel).where(APIKeyModel.key_hash == key_hash))
        assert row is not None
        assert row.zone_id is None
        zones = session.scalars(
            select(APIKeyZoneModel.zone_id).where(APIKeyZoneModel.key_id == row.key_id)
        ).all()
        assert zones == ["root"]

    result = _auth_result(SessionFactory, raw_key)
    assert result.authenticated is True
    assert result.is_admin is True
    assert result.zone_set == ("root",)


def test_existing_custom_admin_key_is_repaired_when_legacy_zone_is_unbackfilled(
    db_url_and_factory,
    monkeypatch,
):
    monkeypatch.delenv("NEXUS_API_KEY_SECRET", raising=False)
    db_url, SessionFactory = db_url_and_factory
    create_admin_key = _load_script("create_admin_key")
    raw_key = "sk-e2e-existing-legacy-admin-key"
    key_hash = DatabaseAPIKeyAuth._hash_key(raw_key)

    with SessionFactory() as session:
        session.add(
            APIKeyModel(
                user_id="admin",
                key_hash=key_hash,
                name="Admin key (from environment)",
                zone_id="root",
                is_admin=1,
                subject_type="user",
                subject_id="admin",
                inherit_permissions=0,
                revoked=0,
            )
        )
        session.commit()

    returned, success = create_admin_key.create_admin_key(
        db_url,
        "admin",
        custom_key=raw_key,
        skip_permissions=True,
    )

    assert success is True
    assert returned == raw_key
    with SessionFactory() as session:
        row = session.scalar(select(APIKeyModel).where(APIKeyModel.key_hash == key_hash))
        assert row is not None
        assert row.zone_id is None
        zones = session.scalars(
            select(APIKeyZoneModel.zone_id).where(APIKeyZoneModel.key_id == row.key_id)
        ).all()
        assert zones == ["root"]

    assert _auth_result(SessionFactory, raw_key).authenticated is True


def test_check_api_key_rejects_legacy_unbackfilled_admin_key(db_url_and_factory, monkeypatch):
    monkeypatch.delenv("NEXUS_API_KEY_SECRET", raising=False)
    db_url, SessionFactory = db_url_and_factory
    check_api_key = _load_script("check_api_key")
    raw_key = "sk-e2e-check-legacy-admin-key"
    key_hash = DatabaseAPIKeyAuth._hash_key(raw_key)

    with SessionFactory() as session:
        session.add(
            APIKeyModel(
                user_id="admin",
                key_hash=key_hash,
                name="Admin key (from environment)",
                zone_id="root",
                is_admin=1,
                subject_type="user",
                subject_id="admin",
                inherit_permissions=0,
                revoked=0,
            )
        )
        session.commit()

    assert check_api_key.check_api_key(db_url, raw_key) == "MISSING"
