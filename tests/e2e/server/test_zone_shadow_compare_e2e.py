"""§10.5 shadow-compare drill — legacy /api/zones vs canonical /v2/zones.

The legacy route delegates to the one ZoneApplicationService (single writer),
so the compare's invariant is semantic equivalence of the two read surfaces
over the same store: same zone identity set, name↔display_name mapping,
status mapping (§6.8), and deprecation markers on the legacy route (the
rollback window). A mismatch is the drill's failure — exactly what a
dual-write cutover would need to detect before flipping the primary read.
"""

from __future__ import annotations

import time

import httpx

_LEGACY_TO_CANONICAL_STATUS = {
    "Active": "active",
    "active": "active",
    "Terminating": "deleting",
    "deleting": "deleting",
    "Terminated": "deleted",
    "deleted": "deleted",
    "suspended": "suspended",
}


def _wait_operation(client: httpx.Client, op_id: str, headers: dict, timeout_s: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/v2/zone-operations/{op_id}", headers=headers)
        if r.status_code == 200 and r.json().get("state") in ("succeeded", "failed"):
            return r.json()
        time.sleep(0.5)
    raise AssertionError(f"operation {op_id} not settled")


def test_shadow_compare_legacy_and_v2_reads_agree(nexus_server, test_app) -> None:
    headers = {"Authorization": f"Bearer {nexus_server['api_key']}"}

    for zone_id in ("shadow-one", "shadow-two"):
        created = test_app.post(
            "/v2/zones",
            headers={**headers, "Idempotency-Key": f"shadow-create-{zone_id}"},
            json={"zone_id": zone_id, "display_name": zone_id},
        )
        assert created.status_code == 202, created.text
        op = _wait_operation(test_app, created.json()["operation_id"], headers)
        assert op["state"] == "succeeded", op

    legacy = test_app.get("/api/zones", headers=headers)
    assert legacy.status_code == 200, legacy.text
    legacy_body = legacy.json()
    legacy_zones = legacy_body.get("zones", legacy_body) if isinstance(legacy_body, dict) else legacy_body

    v2 = test_app.get("/v2/zones", headers=headers, params={"limit": 200})
    assert v2.status_code == 200, v2.text
    v2_zones = {z["zone_id"]: z for z in v2.json().get("zones", [])}

    legacy_by_id = {}
    for item in legacy_zones:
        zid = item.get("zone_id") or item.get("id") or item.get("name")
        legacy_by_id[zid] = item

    # Identity set agreement (legacy may hide deleted; compare on the union
    # of live identities each surface exposes).
    assert {"shadow-one", "shadow-two"} <= set(v2_zones)
    for zone_id in ("shadow-one", "shadow-two"):
        assert zone_id in legacy_by_id, f"legacy surface missing {zone_id}: {legacy_by_id}"

    for zone_id, canonical in v2_zones.items():
        legacy_item = legacy_by_id.get(zone_id)
        if legacy_item is None:
            continue
        # name ↔ display_name
        legacy_name = legacy_item.get("name") or legacy_item.get("display_name")
        assert legacy_name == canonical["display_name"], (zone_id, legacy_item, canonical)
        # status mapping (§6.8)
        legacy_status = legacy_item.get("status") or legacy_item.get("phase")
        expected = _LEGACY_TO_CANONICAL_STATUS.get(str(legacy_status), str(legacy_status))
        assert canonical["status"] == expected, (zone_id, legacy_status, canonical["status"])

    # Rollback window: the legacy route is explicitly deprecated, never removed.
    assert legacy.headers.get("deprecation") == "true", legacy.headers
    assert "sunset" in {k.lower() for k in legacy.headers}
