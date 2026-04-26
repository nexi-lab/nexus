"""Cross-zone leak regression for /api/v2/agents (#3871 round 6).

Pre-#3871 the registered-agent listing filtered on the deprecated
APIKeyModel.zone_id column. After Phase 2 that column is NULL for new
keys, so '?zone_id=eng' could include agents whose only junction zone
was 'ops' and label them as eng. The fix joins through api_key_zones.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.server.api.v2.routers.agent_status import (
    _get_agent_registry,
    _get_nexus_fs,
    router,
)
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models import Base, ZoneModel


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/agents.db")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)
    with SessionLocal() as s:
        s.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
        s.add(ZoneModel(zone_id="ops", name="ops", phase="Active"))
        s.commit()
    return SessionLocal


@pytest.fixture
def app(session_factory):
    """Build a TestClient app with the agent_status router and a sqlite-backed
    nexus_fs.SessionLocal. Auth and agent_registry are stubbed out."""
    from nexus.server.api.v2.dependencies import _get_require_auth

    app = FastAPI()
    fake_fs = SimpleNamespace(SessionLocal=session_factory)
    fake_registry = MagicMock()
    fake_registry.list_processes.return_value = []
    app.state.agent_registry = fake_registry
    app.state.nexus_fs = fake_fs
    app.dependency_overrides[_get_agent_registry] = lambda: fake_registry
    app.dependency_overrides[_get_nexus_fs] = lambda: fake_fs
    app.dependency_overrides[_get_require_auth()] = lambda: {
        "authenticated": True,
        "subject_type": "user",
        "subject_id": "test",
        "zone_id": "root",
        "is_admin": True,
    }
    app.include_router(router)
    return app


def test_list_agents_filters_by_junction_zone_no_cross_zone_leak(app, session_factory):
    """Two agents in different zones — each zone listing returns only its own."""
    with session_factory() as s:
        create_api_key(
            s,
            user_id="alice",
            name="agent:alice-eng",
            subject_type="agent",
            subject_id="agent-eng-1",
            zones=["eng"],
        )
        create_api_key(
            s,
            user_id="bob",
            name="agent:bob-ops",
            subject_type="agent",
            subject_id="agent-ops-1",
            zones=["ops"],
        )
        s.commit()

    client = TestClient(app)

    eng_resp = client.get("/api/v2/agents?zone_id=eng")
    assert eng_resp.status_code == 200, eng_resp.text
    eng_ids = [a["agent_id"] for a in eng_resp.json()["agents"]]
    eng_zones = [a["zone_id"] for a in eng_resp.json()["agents"]]
    assert "agent-eng-1" in eng_ids
    assert "agent-ops-1" not in eng_ids, "cross-zone leak: ops agent appears in eng listing"
    assert all(z == "eng" for z in eng_zones), f"non-eng zone in eng listing: {eng_zones}"

    ops_resp = client.get("/api/v2/agents?zone_id=ops")
    assert ops_resp.status_code == 200, ops_resp.text
    ops_ids = [a["agent_id"] for a in ops_resp.json()["agents"]]
    ops_zones = [a["zone_id"] for a in ops_resp.json()["agents"]]
    assert "agent-ops-1" in ops_ids
    assert "agent-eng-1" not in ops_ids, "cross-zone leak: eng agent appears in ops listing"
    assert all(z == "ops" for z in ops_zones), f"non-ops zone in ops listing: {ops_zones}"
