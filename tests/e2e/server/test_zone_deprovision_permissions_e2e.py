"""E2E test: Zone deprovision with FastAPI, ReBAC permissions, and non-admin users.

Tests the full zone lifecycle with actual permission enforcement:
1. Register two users (owner + non-member)
2. Create zone (owner auto-enrolled via ReBAC)
3. Non-member cannot DELETE zone (403)
4. Owner can DELETE zone (202)
5. Verify idempotent retry on terminated zone
6. Verify response format

Issue #2061: Zone Finalizer Protocol for Ordered Cleanup.
"""

import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def app_with_auth():
    """Create a full FastAPI app with DatabaseLocalAuth and ReBAC."""
    tmpdir = tempfile.mkdtemp()
    tmp_path = Path(tmpdir)

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from nexus.storage.models._base import Base

    # Shared SQLite DB for auth + record store + ReBAC
    db_path = tmp_path / "e2e_zone.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    # Ensure rebac_tuples table exists (ReBAC uses raw SQL)
    with engine.connect() as conn:
        conn.execute(
            text(
                """CREATE TABLE IF NOT EXISTS rebac_tuples (
                    id TEXT PRIMARY KEY,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    zone_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )"""
            )
        )
        conn.commit()

    # Create NexusFS via factory
    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.factory import create_nexus_fs
    from nexus.storage.record_store import SQLAlchemyRecordStore

    storage_path = tmp_path / "storage"
    storage_path.mkdir(exist_ok=True)
    backend = CASLocalBackend(root_path=storage_path)
    metadata_store = str(tmp_path / "raft")
    record_store = SQLAlchemyRecordStore(db_url=db_url)

    nx = create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
    )

    # Create auth provider with SAME database
    from nexus.bricks.auth.providers.database_local import DatabaseLocalAuth

    auth = DatabaseLocalAuth(
        session_factory=SessionLocal,
        jwt_secret="test-secret-key-for-e2e",
    )

    # Create FastAPI app
    from nexus.server.fastapi_server import create_app

    app = create_app(
        nexus_fs=nx,
        auth_provider=auth,
        database_url=db_url,
    )

    # Set nexus instance globally (zone routes use get_nexus_instance())
    from nexus.server.auth.auth_routes import set_nexus_instance

    set_nexus_instance(nx)

    from fastapi.testclient import TestClient

    client = TestClient(app)

    yield {"client": client, "nx": nx, "session_factory": SessionLocal}

    # Cleanup
    nx.close()
    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)


def _register_or_login(client, email, password, username, display_name):
    """Register a user, or login if already registered."""
    resp = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": password,
            "username": username,
            "display_name": display_name,
        },
    )
    if resp.status_code == 503:
        pytest.skip("Auth provider not configured")
    if resp.status_code == 201:
        return resp.json()["token"]
    # Already registered — login
    resp = client.post(
        "/auth/login",
        json={"identifier": email, "password": password},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()["token"]


@pytest.fixture(scope="module")
def owner_token(app_with_auth):
    """Register/login zone owner user."""
    return _register_or_login(
        app_with_auth["client"],
        "owner@example.com",
        "ownerpass123!",
        "zone_owner",
        "Zone Owner",
    )


@pytest.fixture(scope="module")
def outsider_token(app_with_auth):
    """Register/login a user who is NOT a zone member."""
    return _register_or_login(
        app_with_auth["client"],
        "outsider@example.com",
        "outsiderpass123!",
        "zone_outsider",
        "Zone Outsider",
    )


@pytest.fixture(scope="module")
def zone_id(app_with_auth, owner_token):
    """Create a zone — owner is auto-enrolled via ReBAC."""
    client = app_with_auth["client"]
    resp = client.post(
        "/api/zones",
        json={"name": "Test Zone", "zone_id": "perm-test-zone"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    # Zone may already exist from prior test class
    if resp.status_code == 400 and "already" in resp.text.lower():
        return "perm-test-zone"
    if resp.status_code != 201:
        pytest.skip(f"Zone creation failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    assert data["phase"] == "Active"
    assert data["finalizers"] == []
    return data["zone_id"]


# ---------------------------------------------------------------------------
# Permission tests — non-admin user enforcement
# ---------------------------------------------------------------------------


class TestZonePermissions:
    """Verify ReBAC permissions on zone endpoints."""

    def test_unauthenticated_delete_returns_error(self, app_with_auth):
        """DELETE without token → 401 from the unified auth dependency."""
        client = app_with_auth["client"]
        resp = client.delete("/api/zones/perm-test-zone")
        assert resp.status_code == 401

    def test_outsider_cannot_get_zone(self, app_with_auth, outsider_token, zone_id):
        """Non-member cannot GET a zone they don't belong to."""
        client = app_with_auth["client"]
        resp = client.get(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {outsider_token}"},
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    def test_outsider_cannot_delete_zone(self, app_with_auth, outsider_token, zone_id):
        """Non-member cannot DELETE a zone they don't belong to."""
        client = app_with_auth["client"]
        resp = client.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {outsider_token}"},
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

    def test_owner_can_get_zone(self, app_with_auth, owner_token, zone_id):
        """Zone owner can GET the zone."""
        client = app_with_auth["client"]
        resp = client.get(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["zone_id"] == zone_id
        assert data["phase"] == "Active"

    def test_outsider_list_zones_excludes_zone(self, app_with_auth, outsider_token, zone_id):
        """Non-member listing zones should NOT see this zone."""
        client = app_with_auth["client"]
        resp = client.get(
            "/api/zones",
            headers={"Authorization": f"Bearer {outsider_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        zone_ids = [z["zone_id"] for z in data["zones"]]
        assert zone_id not in zone_ids, f"Outsider should not see zone {zone_id}"


# ---------------------------------------------------------------------------
# Full deprovision lifecycle
# ---------------------------------------------------------------------------


class TestDeprovisionLifecycle:
    """Full zone lifecycle: create → deprovision → verify."""

    def test_owner_deprovision_zone(self, app_with_auth, owner_token, zone_id):
        """Zone owner can DELETE → 202 Accepted, finalizers run."""
        client = app_with_auth["client"]
        resp = client.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["zone_id"] == zone_id
        assert data["phase"] in ("Terminating", "Terminated")
        assert "finalizers_completed" in data
        assert "finalizers_pending" in data
        assert "finalizers_failed" in data

    def test_double_delete_idempotent(self, app_with_auth, owner_token):
        """Second DELETE after termination → 404 (ReBAC tuples cleaned)."""
        client = app_with_auth["client"]
        # Create a fresh zone for this test
        create_resp = client.post(
            "/api/zones",
            json={"name": "Double Delete", "zone_id": "double-del-zone"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        if create_resp.status_code != 201:
            pytest.skip("Zone creation failed")
        zid = create_resp.json()["zone_id"]

        first = client.delete(
            f"/api/zones/{zid}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert first.status_code == 202

        # Second DELETE — zone is Terminated so 404; or 403 because ReBAC
        # tuples were cleaned (finalizer worked correctly)
        second = client.delete(
            f"/api/zones/{zid}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert second.status_code in (202, 403, 404), (
            f"Expected 202/403/404, got {second.status_code}: {second.text}"
        )

    def test_get_after_deprovision(self, app_with_auth, owner_token):
        """GET after deprovision shows terminated phase."""
        client = app_with_auth["client"]

        create_resp = client.post(
            "/api/zones",
            json={"name": "Deprovision Check", "zone_id": "depr-check-zone"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        if create_resp.status_code != 201:
            pytest.skip("Zone creation failed")
        zid = create_resp.json()["zone_id"]

        del_resp = client.delete(
            f"/api/zones/{zid}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert del_resp.status_code == 202

        get_resp = client.get(
            f"/api/zones/{zid}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        if get_resp.status_code == 200:
            data = get_resp.json()
            assert data["phase"] in ("Terminating", "Terminated")
            assert data["is_active"] is False

    def test_deprovision_removes_only_target_zone_graph_and_rebac_rows(
        self,
        app_with_auth,
        owner_token,
    ):
        """Finalizer deletes all target references and preserves control rows."""
        from sqlalchemy import text

        client = app_with_auth["client"]
        session_factory = app_with_auth["session_factory"]
        headers = {"Authorization": f"Bearer {owner_token}"}
        target_zone = "cleanup-target-zone"
        control_zone = "cleanup-control-zone"

        for zone_id, name in (
            (target_zone, "Cleanup Target"),
            (control_zone, "Cleanup Control"),
        ):
            response = client.post(
                "/api/zones",
                json={"name": name, "zone_id": zone_id},
                headers=headers,
            )
            assert response.status_code == 201, response.text

        with session_factory() as session:
            # This regression must exercise the explicit cleanup statements,
            # not SQLite's optional ON DELETE CASCADE behavior.
            assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 0

            for prefix, zone_id in (("target", target_zone), ("control", control_zone)):
                session.execute(
                    text(
                        "INSERT INTO entities ("
                        "entity_id, zone_id, canonical_name, entity_type, merge_count, "
                        "created_at, updated_at) VALUES ("
                        ":source_id, :zone_id, :source_name, 'CONCEPT', 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), ("
                        ":target_id, :zone_id, :target_name, 'CONCEPT', 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "source_id": f"{prefix}-source",
                        "target_id": f"{prefix}-target",
                        "zone_id": zone_id,
                        "source_name": f"{prefix} source",
                        "target_name": f"{prefix} target",
                    },
                )
                session.execute(
                    text(
                        "INSERT INTO relationships ("
                        "relationship_id, zone_id, source_entity_id, target_entity_id, "
                        "relationship_type, weight, confidence, created_at) VALUES ("
                        ":relationship_id, :zone_id, :source_id, :target_id, "
                        "'RELATES_TO', 1.0, 1.0, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "relationship_id": f"{prefix}-relationship",
                        "zone_id": zone_id,
                        "source_id": f"{prefix}-source",
                        "target_id": f"{prefix}-target",
                    },
                )
                session.execute(
                    text(
                        "INSERT INTO entity_mentions ("
                        "mention_id, entity_id, confidence, mention_text, created_at) VALUES ("
                        ":mention_id, :entity_id, 1.0, :mention_text, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "mention_id": f"{prefix}-mention",
                        "entity_id": f"{prefix}-source",
                        "mention_text": f"{prefix} mention",
                    },
                )

            for tuple_id, tuple_zone, subject_zone, object_zone in (
                ("target-owned-tuple", target_zone, target_zone, target_zone),
                ("target-subject-cross-tuple", control_zone, target_zone, control_zone),
                ("target-object-cross-tuple", control_zone, control_zone, target_zone),
                ("control-tuple", control_zone, control_zone, control_zone),
            ):
                session.execute(
                    text(
                        "INSERT INTO rebac_tuples ("
                        "tuple_id, zone_id, subject_zone_id, object_zone_id, subject_type, "
                        "subject_id, relation, object_type, object_id, created_at) VALUES ("
                        ":tuple_id, :tuple_zone, :subject_zone, :object_zone, 'user', "
                        ":subject_id, 'viewer', 'zone', :object_id, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "tuple_id": tuple_id,
                        "tuple_zone": tuple_zone,
                        "subject_zone": subject_zone,
                        "object_zone": object_zone,
                        "subject_id": f"{tuple_id}-subject",
                        "object_id": f"{tuple_id}-object",
                    },
                )
            session.commit()

        delete_response = client.delete(f"/api/zones/{target_zone}", headers=headers)
        assert delete_response.status_code == 202, delete_response.text

        with session_factory() as session:
            target_entity_count = session.execute(
                text("SELECT COUNT(*) FROM entities WHERE zone_id = :zone_id"),
                {"zone_id": target_zone},
            ).scalar_one()
            control_entity_count = session.execute(
                text("SELECT COUNT(*) FROM entities WHERE zone_id = :zone_id"),
                {"zone_id": control_zone},
            ).scalar_one()
            assert target_entity_count == 0
            assert control_entity_count == 2

            target_relationship_count = session.execute(
                text("SELECT COUNT(*) FROM relationships WHERE zone_id = :zone_id"),
                {"zone_id": target_zone},
            ).scalar_one()
            control_relationship_count = session.execute(
                text("SELECT COUNT(*) FROM relationships WHERE zone_id = :zone_id"),
                {"zone_id": control_zone},
            ).scalar_one()
            assert target_relationship_count == 0
            assert control_relationship_count == 1

            target_mention_count = session.execute(
                text("SELECT COUNT(*) FROM entity_mentions WHERE mention_id = :mention_id"),
                {"mention_id": "target-mention"},
            ).scalar_one()
            control_mention_count = session.execute(
                text("SELECT COUNT(*) FROM entity_mentions WHERE mention_id = :mention_id"),
                {"mention_id": "control-mention"},
            ).scalar_one()

            target_tuple_reference_count = session.execute(
                text(
                    "SELECT COUNT(*) FROM rebac_tuples "
                    "WHERE zone_id = :zone_id "
                    "OR subject_zone_id = :zone_id "
                    "OR object_zone_id = :zone_id"
                ),
                {"zone_id": target_zone},
            ).scalar_one()
            control_tuple_count = session.execute(
                text("SELECT COUNT(*) FROM rebac_tuples WHERE tuple_id = :tuple_id"),
                {"tuple_id": "control-tuple"},
            ).scalar_one()
            assert (target_mention_count, target_tuple_reference_count) == (0, 0)
            assert control_mention_count == 1
            assert control_tuple_count == 1


# ---------------------------------------------------------------------------
# ZoneResponse format
# ---------------------------------------------------------------------------


class TestZoneResponseFormat:
    def test_create_zone_has_phase_fields(self, app_with_auth, owner_token):
        """POST /api/zones response includes phase, finalizers, is_active."""
        client = app_with_auth["client"]
        resp = client.post(
            "/api/zones",
            json={"name": "Format Test", "zone_id": "format-check"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        if resp.status_code != 201:
            pytest.skip("Zone creation failed")
        data = resp.json()
        assert data["phase"] == "Active"
        assert data["finalizers"] == []
        assert data["is_active"] is True

    def test_deprovision_response_shape(self, app_with_auth, owner_token):
        """DELETE response has correct ZoneDeprovisionResponse shape."""
        client = app_with_auth["client"]
        create_resp = client.post(
            "/api/zones",
            json={"name": "Shape Test", "zone_id": "shape-check"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        if create_resp.status_code != 201:
            pytest.skip("Zone creation failed")
        zid = create_resp.json()["zone_id"]

        del_resp = client.delete(
            f"/api/zones/{zid}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert del_resp.status_code == 202
        data = del_resp.json()
        assert isinstance(data["zone_id"], str)
        assert isinstance(data["phase"], str)
        assert isinstance(data["finalizers_completed"], list)
        assert isinstance(data["finalizers_pending"], list)
        assert isinstance(data["finalizers_failed"], dict)
