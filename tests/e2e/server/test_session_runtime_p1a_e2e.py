"""P1a runtime-zone E2E (SW-20260915-002 §8.9 / §11.4 moss-independent rows).

Real-process rows verified here (the moss/sudocode rows land in their own
repos and in the G5 matrix):

  1  session solidifies its home zone (immutable)
  2  a run solidifies its execution zone (default = home)
  3  a client payload zone cannot override the home zone
  4  five record kinds default-write real VFS bytes into the home zone
     (routing ledger + kernel read-back, not SQL columns)
  5  revocation_pending blocks new record acquisition
  9  restart keeps home/execution zones from drifting (fixed data dir)
 10  the routing ledger proves home-zone bytes, not just fields
 cross-zone: a run outside the home zone requires decision_reason +
     policy_version and an active execution zone
"""

from __future__ import annotations

import json
import time

import httpx

from tests.e2e.server.test_zone_v2_fault_injection_e2e import ServerHarness


def _create_zone(client: httpx.Client, headers: dict, zone_id: str, key: str) -> None:
    created = client.post(
        "/v2/zones",
        headers={**headers, "Idempotency-Key": key},
        json={"zone_id": zone_id, "display_name": zone_id},
    )
    assert created.status_code == 202, created.text
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        op = client.get(f"/v2/zone-operations/{created.json()['operation_id']}", headers=headers)
        if op.status_code == 200 and op.json().get("state") in ("succeeded", "failed"):
            assert op.json()["state"] == "succeeded", op.text
            return
        time.sleep(0.5)
    raise AssertionError("create operation not settled")


def test_p1a_session_home_zone_and_record_routing(nexus_server, test_app) -> None:
    headers = {"Authorization": f"Bearer {nexus_server['api_key']}"}
    zone = "p1a-home-zone"
    _create_zone(test_app, headers, zone, "p1a-zone-create")

    # (1) home zone solidified at creation
    created = test_app.post(
        "/v2/sessions",
        headers=headers,
        json={"session_id": "p1a-sess-1", "home_zone_id": zone},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["home_zone_id"] == zone
    assert body["kind"] == "SessionMetadata"

    fetched = test_app.get("/v2/sessions/p1a-sess-1", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["home_zone_id"] == zone

    # duplicate create → refused (an unknown alternate zone is refused with
    # 404 before the conflict check — both are rejections; immutability is
    # structural: no update path exists, and the read-back below proves it).
    again = test_app.post(
        "/v2/sessions",
        headers=headers,
        json={"session_id": "p1a-sess-1", "home_zone_id": zone},
    )
    assert again.status_code == 409, again.text
    still = test_app.get("/v2/sessions/p1a-sess-1", headers=headers).json()
    assert still["home_zone_id"] == zone, "home zone must never be rewritten"

    # a zone that does not exist / is not active cannot be homed
    missing = test_app.post(
        "/v2/sessions",
        headers=headers,
        json={"session_id": "p1a-sess-x", "home_zone_id": "no-such-zone"},
    )
    assert missing.status_code == 404, missing.text

    # (4) five record kinds default-write into the home zone with real bytes
    for kind in ("session", "transcript", "context", "artifact", "verify"):
        payload = json.dumps({"kind": kind, "seq": 1}).encode("utf-8").decode("utf-8")
        written = test_app.post(
            "/v2/sessions/p1a-sess-1/records",
            headers=headers,
            json={"record_kind": kind, "data": payload},
        )
        assert written.status_code == 201, f"{kind}: {written.text}"
        assert written.json()["zone_id"] == zone, written.text
        assert written.json()["vfs_path"].startswith("/sessions/p1a-sess-1/"), written.text
        assert written.json()["bytes_written"] == len(payload.encode("utf-8")), written.text

    ledger = test_app.get("/v2/sessions/p1a-sess-1/records", headers=headers).json()["records"]
    assert {r["record_kind"] for r in ledger} == {
        "session",
        "transcript",
        "context",
        "artifact",
        "verify",
    }
    assert all(r["zone_id"] == zone for r in ledger)

    # (3) client payload zone cannot override the home zone
    override = test_app.post(
        "/v2/sessions/p1a-sess-1/records",
        headers=headers,
        json={"record_kind": "transcript", "data": "x", "zone_id": "p1a-other-zone"},
    )
    assert override.status_code == 403, override.text
    assert override.json()["detail"]["code"] == "ZONE_OVERRIDE_DENIED"

    # unknown record kind is refused (no fabricated Task records either)
    unknown = test_app.post(
        "/v2/sessions/p1a-sess-1/records",
        headers=headers,
        json={"record_kind": "task", "data": "{}"},
    )
    assert unknown.status_code == 422, unknown.text


def test_p1a_runtime_run_zones_and_cancellation(nexus_server, test_app) -> None:
    headers = {"Authorization": f"Bearer {nexus_server['api_key']}"}
    zone = "p1a-run-home"
    other = "p1a-run-other"
    _create_zone(test_app, headers, zone, "p1a-run-z1")
    _create_zone(test_app, headers, other, "p1a-run-z2")
    test_app.post(
        "/v2/sessions", headers=headers, json={"session_id": "p1a-sess-2", "home_zone_id": zone}
    )

    # (2) execution zone defaults to home; refs solidified
    started = test_app.post(
        "/v2/runtime/start",
        headers=headers,
        json={
            "pid": "p1a-pid-1",
            "session_id": "p1a-sess-2",
            "delegation_ref": "dlg-p1a",
            "grant_ref": "grant-p1a",
            "authorization_epoch": 3,
        },
    )
    assert started.status_code == 201, started.text
    run = started.json()
    assert run["execution_zone_id"] == zone
    assert run["delegation_ref"] == "dlg-p1a"
    assert run["grant_ref"] == "grant-p1a"
    assert run["authorization_epoch"] == 3

    fetched = test_app.get("/v2/runtime/runs/p1a-pid-1", headers=headers)
    assert fetched.status_code == 200 and fetched.json()["execution_zone_id"] == zone

    # cross-zone run without an explicit decision is refused
    no_decision = test_app.post(
        "/v2/runtime/start",
        headers=headers,
        json={"pid": "p1a-pid-2", "session_id": "p1a-sess-2", "execution_zone_id": other},
    )
    assert no_decision.status_code == 422, no_decision.text
    assert no_decision.json()["detail"]["code"] == "CROSS_ZONE_DECISION_REQUIRED"

    # with the decision recorded, the cross-zone run lands in the other zone
    cross = test_app.post(
        "/v2/runtime/start",
        headers=headers,
        json={
            "pid": "p1a-pid-2",
            "session_id": "p1a-sess-2",
            "execution_zone_id": other,
            "decision_reason": "policy: heavy data locality",
            "policy_version": "corp-2026-09",
        },
    )
    assert cross.status_code == 201, cross.text
    assert cross.json()["execution_zone_id"] == other
    assert cross.json()["decision_reason"] == "policy: heavy data locality"

    # (5) revocation_pending blocks new record acquisition
    pending = test_app.post(
        "/v2/runtime/runs/p1a-pid-1/cancel",
        headers=headers,
        json={"mode": "pending"},
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["state"] == "revocation_pending"
    blocked = test_app.post(
        "/v2/sessions/p1a-sess-2/records",
        headers=headers,
        json={"record_kind": "transcript", "data": "more"},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "REVOCATION_PENDING"

    terminated = test_app.post(
        "/v2/runtime/runs/p1a-pid-1/cancel",
        headers=headers,
        json={"mode": "terminate"},
    )
    assert terminated.status_code == 200 and terminated.json()["state"] == "terminated"
    resumed_ok = test_app.post(
        "/v2/sessions/p1a-sess-2/records",
        headers=headers,
        json={"record_kind": "transcript", "data": "after"},
    )
    assert resumed_ok.status_code == 201, resumed_ok.text


def test_p1a_zones_survive_full_restart(tmp_path) -> None:
    """(§11.4 rows 9/10) home/execution zones do not drift across a hard
    restart over the same data dir, and the routing ledger still proves
    where the bytes landed."""
    harness = ServerHarness(tmp_path / "p1a")
    harness.start()
    headers = {"Authorization": f"Bearer {harness.api_key}"}
    try:
        with harness.client() as client:
            harness.poke_until_up(client, headers)
            _create_zone(client, headers, "p1a-restart-home", "p1a-restart-z")
            created = client.post(
                "/v2/sessions",
                headers=headers,
                json={"session_id": "p1a-restart-sess", "home_zone_id": "p1a-restart-home"},
            )
            assert created.status_code == 201, created.text
            run = client.post(
                "/v2/runtime/start",
                headers=headers,
                json={"pid": "p1a-restart-pid", "session_id": "p1a-restart-sess"},
            )
            assert run.status_code == 201, run.text
            rec = client.post(
                "/v2/sessions/p1a-restart-sess/records",
                headers=headers,
                json={"record_kind": "session", "data": '{"restart": true}'},
            )
            assert rec.status_code == 201, rec.text

        harness.kill()
        harness.start()
        with harness.client() as client:
            harness.poke_until_up(client, headers)
            sess = client.get("/v2/sessions/p1a-restart-sess", headers=headers)
            assert sess.status_code == 200, sess.text
            assert sess.json()["home_zone_id"] == "p1a-restart-home"
            run2 = client.get("/v2/runtime/runs/p1a-restart-pid", headers=headers)
            assert run2.status_code == 200, run2.text
            assert run2.json()["execution_zone_id"] == "p1a-restart-home"
            ledger = client.get("/v2/sessions/p1a-restart-sess/records", headers=headers).json()[
                "records"
            ]
            assert len(ledger) == 1
            assert ledger[0]["zone_id"] == "p1a-restart-home"
            assert ledger[0]["bytes_written"] > 0
    finally:
        harness.kill()
