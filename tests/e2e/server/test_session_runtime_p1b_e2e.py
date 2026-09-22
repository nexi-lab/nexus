"""P1b implicit Task/Resolution/Attempt real-process E2E.

The tests keep the P1a five-kind record surface unchanged while proving that
the internal runtime path creates a true Attempt, writes all task snapshots to
the Session home Zone, enforces declared ResourceRefs, and links revocation to
both the PID and Attempt layers.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import httpx

from tests.e2e.server.test_session_runtime_p1a_e2e import (
    _create_runtime_delegation,
    _create_zone,
    _mint_user_key,
)
from tests.e2e.server.test_zone_v2_fault_injection_e2e import ServerHarness


def _create_session(client: httpx.Client, headers: dict, *, session_id: str, zone_id: str) -> None:
    response = client.post(
        "/v2/sessions",
        headers=headers,
        json={"session_id": session_id, "home_zone_id": zone_id},
    )
    assert response.status_code == 201, response.text


def _task_id_for_session(db_path: Path, session_id: str) -> str | None:
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT task_id FROM task_specs WHERE session_id = ?", (session_id,)
        ).fetchone()
    return str(row[0]) if row else None


def _counts(db_path: Path, session_id: str) -> tuple[int, int, int, int]:
    with sqlite3.connect(db_path) as db:
        task_count = db.execute(
            "SELECT COUNT(*) FROM task_specs WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        resolution_count = db.execute(
            "SELECT COUNT(*) FROM task_resolutions r JOIN task_specs t ON t.task_id = r.task_id "
            "WHERE t.session_id = ?",
            (session_id,),
        ).fetchone()[0]
        attempt_count = db.execute(
            "SELECT COUNT(*) FROM task_attempts WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        run_count = db.execute(
            "SELECT COUNT(*) FROM session_runtime_runs WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    return int(task_count), int(resolution_count), int(attempt_count), int(run_count)


def _resource(zone_id: str, path: str) -> dict:
    return {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": zone_id,
        "path": path,
    }


def test_p1b_implicit_task_resolution_attempt_and_home_zone_io(nexus_server, test_app) -> None:
    admin = {"Authorization": f"Bearer {nexus_server['api_key']}"}
    home = "p1b-home-zone"
    other = "p1b-execution-zone"
    _create_zone(test_app, admin, home, "p1b-home-create")
    _create_zone(test_app, admin, other, "p1b-other-create")
    home_delegation, _, _ = _create_runtime_delegation(
        test_app,
        admin,
        zone_id=home,
        org_id="p1b-org",
        user_id="p1b-user",
        key="p1b-home",
    )
    other_delegation, _, _ = _create_runtime_delegation(
        test_app,
        admin,
        zone_id=other,
        org_id="p1b-org",
        user_id="p1b-user",
        key="p1b-other",
    )
    user_key = _mint_user_key(test_app, admin, user_id="p1b-user", zone_id=home, key="p1b-user-key")
    user_home = {
        "Authorization": f"Bearer {user_key}",
        "X-Nexus-Zone-Delegation": home_delegation,
    }
    _create_session(test_app, admin, session_id="p1b-session", zone_id=home)

    started = test_app.post(
        "/v2/runtime/start",
        headers=user_home,
        json={
            "pid": "p1b-pid-1",
            "session_id": "p1b-session",
            "resource_refs": [_resource(home, "/input.txt")],
        },
    )
    assert started.status_code == 201, started.text
    first = started.json()
    assert first["task_id"] and first["attempt_id"]
    assert first["execution_zone_id"] == home

    task_response = test_app.get(
        f"/v2/sessions/p1b-session/tasks/{first['task_id']}", headers=user_home
    )
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()
    assert task["spec"]["input"]["resource_refs"] == [_resource(home, "/input.txt")]
    assert task["spec"]["storage"]["zone_id"] == home
    assert task["resolutions"][0]["status"] == "accepted"
    assert task["resolutions"][0]["execution_zone_id"] == home
    assert task["attempts"][0]["state"] == "running"
    assert task["attempts"][0]["pid_history"] == ["p1b-pid-1"]
    assert all(
        row["storage"]["zone_id"] == home
        for row in [task["spec"], *task["resolutions"], *task["attempts"]]
    )

    for record in [task["spec"], task["resolutions"][0], task["attempts"][0]]:
        read_back = test_app.get(
            "/api/v2/files/read",
            headers=admin,
            params={"path": record["storage"]["vfs_path"], "zone": home},
        )
        assert read_back.status_code == 200, read_back.text
        raw = read_back.json()["content"].encode()
        assert len(raw) == record["storage"]["bytes_written"]
        assert json.loads(raw)["task_id"] == first["task_id"]

    ledger = test_app.get("/v2/sessions/p1b-session/records", headers=admin)
    assert ledger.status_code == 200
    assert {row["record_kind"] for row in ledger.json()["records"]} <= {
        "session",
        "transcript",
        "context",
        "artifact",
        "verify",
    }

    cancelled = test_app.post(
        "/v2/runtime/runs/p1b-pid-1/cancel", headers=user_home, json={"mode": "terminate"}
    )
    assert cancelled.status_code == 200, cancelled.text
    after_cancel = test_app.get(
        f"/v2/sessions/p1b-session/tasks/{first['task_id']}", headers=admin
    ).json()
    assert after_cancel["attempts"][0]["state"] == "running"

    resumed = test_app.post(
        "/v2/runtime/resume",
        headers=user_home,
        json={"pid": "p1b-pid-1-resumed", "session_id": "p1b-session"},
    )
    assert resumed.status_code == 201, resumed.text
    assert resumed.json()["attempt_id"] == first["attempt_id"]
    after_resume = test_app.get(
        f"/v2/sessions/p1b-session/tasks/{first['task_id']}", headers=admin
    ).json()
    assert after_resume["attempts"][0]["pid_history"] == [
        "p1b-pid-1",
        "p1b-pid-1-resumed",
    ]

    second = test_app.post(
        "/v2/runtime/start",
        headers={
            "Authorization": f"Bearer {user_key}",
            "X-Nexus-Zone-Delegation": other_delegation,
        },
        json={
            "pid": "p1b-pid-2",
            "session_id": "p1b-session",
            "execution_zone_id": other,
            "decision_reason": "policy: data locality",
            "policy_version": "p1b-policy-1",
            "resource_refs": [_resource(other, "/other.txt")],
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["task_id"] == first["task_id"]
    assert second.json()["attempt_id"] != first["attempt_id"]
    assert second.json()["execution_zone_id"] == other
    final_task = test_app.get(
        f"/v2/sessions/p1b-session/tasks/{first['task_id']}", headers=admin
    ).json()
    assert len(final_task["attempts"]) == 2
    assert final_task["resolutions"][-1]["reason"] == "policy: data locality"
    assert final_task["resolutions"][-1]["policy_version"] == "p1b-policy-1"

    failed_start = test_app.post(
        "/v2/runtime/start",
        headers={
            "Authorization": f"Bearer {user_key}",
            "X-Nexus-Zone-Delegation": other_delegation,
        },
        json={
            "pid": "p1b-pid-2",
            "session_id": "p1b-session",
            "execution_zone_id": other,
            "decision_reason": "policy: duplicate pid probe",
            "policy_version": "p1b-policy-1",
        },
    )
    assert failed_start.status_code == 409, failed_start.text
    assert failed_start.json()["detail"]["code"] == "RUN_ALREADY_EXISTS"
    after_failed_start = test_app.get(
        f"/v2/sessions/p1b-session/tasks/{first['task_id']}", headers=admin
    ).json()
    assert after_failed_start["attempts"][-1]["state"] == "failed"
    assert after_failed_start["attempts"][-1]["failure"]["code"] == "RUN_ALREADY_EXISTS"


def test_p1b_rejections_revocation_and_authorization_order(nexus_server, test_app) -> None:
    admin = {"Authorization": f"Bearer {nexus_server['api_key']}"}
    home = "p1b-policy-home"
    other = "p1b-policy-other"
    _create_zone(test_app, admin, home, "p1b-policy-home-create")
    _create_zone(test_app, admin, other, "p1b-policy-other-create")

    _create_session(test_app, admin, session_id="p1b-rejected", zone_id=home)
    missing_decision = test_app.post(
        "/v2/runtime/start",
        headers=admin,
        json={
            "pid": "p1b-rejected-pid",
            "session_id": "p1b-rejected",
            "execution_zone_id": other,
        },
    )
    assert missing_decision.status_code == 422, missing_decision.text
    assert missing_decision.json()["detail"]["code"] == "CROSS_ZONE_DECISION_REQUIRED"
    rejected_task_id = _task_id_for_session(nexus_server["db_path"], "p1b-rejected")
    assert rejected_task_id is not None
    rejected = test_app.get(
        f"/v2/sessions/p1b-rejected/tasks/{rejected_task_id}", headers=admin
    ).json()
    assert [row["reason_code"] for row in rejected["resolutions"]] == ["INVALID_TASK_SPEC"]
    assert rejected["attempts"] == []
    assert _counts(nexus_server["db_path"], "p1b-rejected") == (1, 1, 0, 0)

    _create_session(test_app, admin, session_id="p1b-unauthorized", zone_id=home)
    user_key = _mint_user_key(
        test_app, admin, user_id="p1b-no-access", zone_id=home, key="p1b-no-access-key"
    )
    unauthorized = test_app.post(
        "/v2/runtime/start",
        headers={"Authorization": f"Bearer {user_key}"},
        json={
            "pid": "p1b-unauthorized-pid",
            "session_id": "p1b-unauthorized",
            "execution_zone_id": other,
        },
    )
    assert unauthorized.status_code == 403, unauthorized.text
    assert _counts(nexus_server["db_path"], "p1b-unauthorized") == (0, 0, 0, 0)

    home_delegation, _, _ = _create_runtime_delegation(
        test_app,
        admin,
        zone_id=home,
        org_id="p1b-resource-org",
        user_id="p1b-resource-user",
        key="p1b-resource-home",
    )
    resource_user_key = _mint_user_key(
        test_app,
        admin,
        user_id="p1b-resource-user",
        zone_id=home,
        key="p1b-resource-user-key",
    )
    _create_session(test_app, admin, session_id="p1b-resource", zone_id=home)
    denied_ref = test_app.post(
        "/v2/runtime/start",
        headers={
            "Authorization": f"Bearer {resource_user_key}",
            "X-Nexus-Zone-Delegation": home_delegation,
        },
        json={
            "pid": "p1b-resource-pid",
            "session_id": "p1b-resource",
            "resource_refs": [_resource(other, "/private/input.txt")],
        },
    )
    assert denied_ref.status_code == 403, denied_ref.text
    assert denied_ref.json()["detail"]["code"] == "ZONE_ACCESS_DENIED"
    denied_task_id = _task_id_for_session(nexus_server["db_path"], "p1b-resource")
    denied_task = test_app.get(
        f"/v2/sessions/p1b-resource/tasks/{denied_task_id}", headers=admin
    ).json()
    assert denied_task["resolutions"][0]["reason_code"] == "ZONE_ACCESS_DENIED"
    assert denied_task["attempts"] == []
    assert _counts(nexus_server["db_path"], "p1b-resource") == (1, 1, 0, 0)

    delegation_one, grant_one, _ = _create_runtime_delegation(
        test_app,
        admin,
        zone_id=home,
        org_id="p1b-revoke-org",
        user_id="p1b-revoke-user",
        key="p1b-revoke-one",
    )
    revoke_user_key = _mint_user_key(
        test_app,
        admin,
        user_id="p1b-revoke-user",
        zone_id=home,
        key="p1b-revoke-user-key",
    )
    _create_session(test_app, admin, session_id="p1b-revoke", zone_id=home)
    started = test_app.post(
        "/v2/runtime/start",
        headers={
            "Authorization": f"Bearer {revoke_user_key}",
            "X-Nexus-Zone-Delegation": delegation_one,
        },
        json={"pid": "p1b-revoke-pid", "session_id": "p1b-revoke"},
    )
    assert started.status_code == 201, started.text
    before_counts = _counts(nexus_server["db_path"], "p1b-revoke")
    revoked = test_app.delete(
        f"/v2/zones/{home}/grants/{grant_one}",
        headers={**admin, "Idempotency-Key": "p1b-revoke-grant"},
    )
    assert revoked.status_code == 202, revoked.text

    task_id = started.json()["task_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = test_app.get("/v2/runtime/runs/p1b-revoke-pid", headers=admin).json()
        task = test_app.get(f"/v2/sessions/p1b-revoke/tasks/{task_id}", headers=admin).json()
        if run["state"] == "revocation_pending" and task["attempts"][0]["state"] == "cancelled":
            break
        time.sleep(0.25)
    assert run["state"] == "revocation_pending"
    assert task["attempts"][0]["state"] == "cancelled"
    assert task["attempts"][0]["failure"]["code"] == "GRANT_REVOKED"

    delegation_two, _, _ = _create_runtime_delegation(
        test_app,
        admin,
        zone_id=home,
        org_id="p1b-revoke-org",
        user_id="p1b-revoke-user",
        key="p1b-revoke-two",
    )
    resumed = test_app.post(
        "/v2/runtime/resume",
        headers={
            "Authorization": f"Bearer {revoke_user_key}",
            "X-Nexus-Zone-Delegation": delegation_two,
        },
        json={"pid": "p1b-revoke-resume", "session_id": "p1b-revoke"},
    )
    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["detail"]["code"] == "ATTEMPT_NOT_ACTIVE"

    denied_after_revoke = test_app.post(
        "/v2/runtime/start",
        headers={
            "Authorization": f"Bearer {revoke_user_key}",
            "X-Nexus-Zone-Delegation": delegation_one,
        },
        json={"pid": "p1b-revoke-denied", "session_id": "p1b-revoke"},
    )
    assert denied_after_revoke.status_code == 403, denied_after_revoke.text
    assert _counts(nexus_server["db_path"], "p1b-revoke") == before_counts


def test_p1b_task_identity_survives_restart(tmp_path) -> None:
    harness = ServerHarness(tmp_path / "p1b")
    harness.start()
    admin = {"Authorization": f"Bearer {harness.api_key}"}
    try:
        with harness.client() as client:
            harness.poke_until_up(client, admin)
            _create_zone(client, admin, "p1b-restart-home", "p1b-restart-zone")
            delegation, _, _ = _create_runtime_delegation(
                client,
                admin,
                zone_id="p1b-restart-home",
                org_id="p1b-restart-org",
                user_id="p1b-restart-user",
                key="p1b-restart",
            )
            _create_session(
                client, admin, session_id="p1b-restart-session", zone_id="p1b-restart-home"
            )
            started = client.post(
                "/v2/runtime/start",
                headers={**admin, "X-Nexus-Zone-Delegation": delegation},
                json={"pid": "p1b-restart-pid", "session_id": "p1b-restart-session"},
            )
            assert started.status_code == 201, started.text
            task_id = started.json()["task_id"]
            attempt_id = started.json()["attempt_id"]

        harness.kill()
        harness.start()
        with harness.client() as client:
            harness.poke_until_up(client, admin)
            run = client.get("/v2/runtime/runs/p1b-restart-pid", headers=admin)
            assert run.status_code == 200, run.text
            assert run.json()["task_id"] == task_id
            assert run.json()["attempt_id"] == attempt_id
            assert run.json()["execution_zone_id"] == "p1b-restart-home"
            task = client.get(f"/v2/sessions/p1b-restart-session/tasks/{task_id}", headers=admin)
            assert task.status_code == 200, task.text
            assert task.json()["attempts"][0]["attempt_id"] == attempt_id
            assert task.json()["attempts"][0]["execution_zone_id"] == "p1b-restart-home"
            assert task.json()["spec"]["storage"]["zone_id"] == "p1b-restart-home"
    finally:
        harness.kill()
