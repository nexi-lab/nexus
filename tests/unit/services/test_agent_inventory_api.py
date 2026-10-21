"""Inventory HTTP authentication, tenant isolation, input limits and read-only behavior."""

from sqlalchemy import event

from tests.unit.services.test_agent_inventory import cmd, values
from tests.unit.services.test_product_agent_registry import registry as registry
from tests.unit.services.test_product_agent_registry_api import BASE, HEADERS
from tests.unit.services.test_product_agent_registry_api import client as client

URL = BASE + "/inventory"


def test_inventory_http_flow_and_reads_do_not_publish(client, registry):
    request = cmd("SaveAsset", metadata=values()).model_dump()
    created = client.post(URL + "/commands", headers=HEADERS, json=request)
    assert created.status_code == 200
    assert created.json()["actual_level"] == "L0"
    assert client.post(URL + "/commands", headers=HEADERS, json=request).json() == created.json()
    statements = []

    def capture(_con, _cursor, sql, *_args):
        statements.append(sql.strip().split()[0].upper())

    event.listen(registry.engine, "before_cursor_execute", capture)
    try:
        listed = client.get(URL + "/assets", headers=HEADERS)
        assert listed.status_code == 200 and len(listed.json()["items"]) == 1
        assert client.get(URL + "/assets/asset-a", headers=HEADERS).json() == created.json()
        assert (
            len(client.get(URL + "/assets/asset-a/history", headers=HEADERS).json()["events"]) == 1
        )
    finally:
        event.remove(registry.engine, "before_cursor_execute", capture)
    assert not {"INSERT", "UPDATE", "DELETE", "ALTER", "CREATE"} & set(statements)


def test_inventory_member_and_zone_spoofing_rejected(client):
    request = cmd("SaveAsset", metadata=values()).model_dump()
    assert client.post(URL + "/commands", headers=HEADERS, json=request).status_code == 200
    member = {
        "Authorization": "Bearer sk-test-member",
        "X-Nexus-Zone-ID": "zone-a",
        "X-Nexus-Subject": "user:admin",
    }
    assert client.post(URL + "/commands", headers=member, json=request).status_code == 403
    for path in ["/assets", "/assets/asset-a", "/assets/asset-a/history"]:
        assert client.get(URL + path, headers=member).status_code == 403
        assert client.get(URL + path).status_code == 401
    assert (
        client.get(
            URL + "/assets/asset-a", headers={**HEADERS, "X-Nexus-Zone-ID": "zone-b"}
        ).status_code
        == 404
    )
    assert (
        client.get(URL + "/assets", headers={**member, "X-Nexus-Zone-ID": "zone-b"}).status_code
        == 401
    )


def test_inventory_body_limit_strict_fields_and_untrusted_acceptance(client):
    for body in [
        b'{"kind":"SaveAsset","kind":"RecordAcceptance"}',
        b'{"secret":"do-not-echo"}',
        b"[]",
    ]:
        result = client.post(URL + "/commands", headers=HEADERS, content=body)
        assert result.status_code == 400 and "do-not-echo" not in result.text
    assert client.post(URL + "/commands", headers=HEADERS, content=b"x" * 65537).status_code == 413
    request = cmd("SaveAsset", metadata=values()).model_dump()
    for patch in [{"actual_level": "L3"}, {"is_admin": True}, {"owner_zone": "zone-b"}]:
        assert (
            client.post(URL + "/commands", headers=HEADERS, json={**request, **patch}).status_code
            == 400
        )
    created = client.post(URL + "/commands", headers=HEADERS, json=request).json()
    from nexus.contracts.agent_inventory import AssetRecord
    from tests.unit.services.test_agent_inventory import report

    acceptance = cmd(
        "RecordAcceptance", created["revision"], report=report(AssetRecord.model_validate(created))
    ).model_dump()
    assert client.post(URL + "/commands", headers=HEADERS, json=acceptance).status_code == 403
