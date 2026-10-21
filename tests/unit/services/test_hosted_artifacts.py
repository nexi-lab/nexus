"""Real primary-store imports, replay, authorization and no implicit publication."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import event

from nexus.contracts.agent_hosted import HostedImage, RegisterHostedArtifact
from nexus.services.product_agent_registry.hosted_artifacts import artifacts
from nexus.services.product_agent_registry.store import RegistryActor, RegistryError
from tests.unit.services.test_agent_inventory import cmd, save
from tests.unit.services.test_dify_import import SOURCE
from tests.unit.services.test_product_agent_registry import ADMIN, MEMBER
from tests.unit.services.test_product_agent_registry import registry as registry
from tests.unit.services.test_product_agent_registry_api import BASE, HEADERS
from tests.unit.services.test_product_agent_registry_api import client as client


def request(registry):
    asset = save(registry, hosting="internal_hosted", requested_level="L2", model_paths=["m"])
    return RegisterHostedArtifact(
        request_id=uuid4().hex,
        asset_id=asset.id,
        metadata_digest=asset.metadata_digest,
        dify_source=SOURCE.read_text(),
        initiated_by="alice",
        image=HostedImage(
            profile="dify-0.6.0-linear-text-v1",
            image_id="sha256:" + "a" * 64,
            source_digest="sha256:" + hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
            dependency_lock_digest="sha256:" + "b" * 64,
            model_resource="m",
            model_name="gemini-3-flash-preview",
        ),
    )


def test_import_is_immutable_scoped_and_does_not_publish(registry):
    req = request(registry)
    result = registry.hosted.register(ADMIN, req)
    assert result.evidence == "registered_only"
    assert result.dify_plan.source_digest == req.image.source_digest
    assert registry.hosted.register(ADMIN, req) == result
    assert registry.hosted.list(ADMIN, req.asset_id).items == [result]
    assert registry.inventory.get(ADMIN, req.asset_id).actual_level == "L0"
    with pytest.raises(RegistryError, match="HOSTED_IDEMPOTENCY_CONFLICT"):
        registry.hosted.register(ADMIN, req.model_copy(update={"initiated_by": "someone-else"}))
    with pytest.raises(RegistryError):
        registry.hosted.get(MEMBER, result.id)
    other = RegistryActor(subject=ADMIN.subject, zone="zone-b", is_admin=True)
    with pytest.raises(RegistryError, match="HOSTED_ARTIFACT_NOT_FOUND"):
        registry.hosted.get(other, result.id)
    # Reads issue no publication or schema mutations.
    sql = []

    def capture(_con, _cursor, statement, *_args):
        sql.append(statement.strip().split()[0].upper())

    event.listen(registry.engine, "before_cursor_execute", capture)
    try:
        registry.hosted.get(ADMIN, result.id)
        registry.hosted.list(ADMIN, req.asset_id)
    finally:
        event.remove(registry.engine, "before_cursor_execute", capture)
    assert not set(sql) & {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"}


@pytest.mark.parametrize(
    "change",
    [
        {"source_digest": "sha256:" + "f" * 64},
        {"model_name": "different"},
        {"max_output_tokens": 32},
        {"image_id": "sha256:" + "0" * 64},
    ],
)
def test_mismatched_source_is_never_registered(registry, change):
    req = request(registry)
    req = req.model_copy(update={"image": req.image.model_copy(update=change)})
    with pytest.raises(RegistryError):
        registry.hosted.register(ADMIN, req)
    assert registry.hosted.list(ADMIN, req.asset_id).items == []


def test_changed_asset_requires_new_matching_import(registry):
    req = request(registry)
    asset = registry.inventory.get(ADMIN, req.asset_id)
    registry.inventory.command(
        ADMIN,
        cmd(
            "SaveAsset",
            asset.revision,
            metadata={**asset.metadata.model_dump(), "purpose": "changed"},
        ),
    )
    with pytest.raises(RegistryError, match="HOSTED_ASSET_NOT_ADMITTED"):
        registry.hosted.register(ADMIN, req)


def test_concurrent_exact_import_has_one_record(registry):
    req = request(registry)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: registry.hosted.register(ADMIN, req), range(4)))
    assert len({r.id for r in rows}) == 1
    assert len(registry.hosted.list(ADMIN, req.asset_id).items) == 1


def test_hosted_http_inspection_import_and_denials(client, registry):
    req = request(registry)
    url = BASE + "/hosted"
    assert (
        client.post(url + "/dify/inspect", headers=HEADERS, content=SOURCE.read_bytes()).status_code
        == 200
    )
    response = client.post(url + "/artifacts", headers=HEADERS, json=req.model_dump())
    assert response.status_code == 200, response.text
    result = response.json()
    assert client.get(url + "/artifacts?asset_id=" + req.asset_id, headers=HEADERS).json()[
        "items"
    ] == [result]
    assert client.get(url + "/artifacts/" + result["id"], headers=HEADERS).json() == result
    for path in ["/artifacts/" + result["id"], "/artifacts?asset_id=" + req.asset_id]:
        assert client.get(url + path).status_code == 401
        assert (
            client.get(url + path, headers={"Authorization": "Bearer sk-test-member"}).status_code
            == 403
        )
        assert (
            client.get(url + path, headers={**HEADERS, "X-Nexus-Zone-ID": "zone-b"}).status_code
            == 404
        )
    assert (
        client.post(
            url + "/artifacts", headers=HEADERS, json={**req.model_dump(), "actual_level": "L2"}
        ).status_code
        == 400
    )
    assert (
        client.post(url + "/dify/inspect", headers=HEADERS, content=b"x" * 256001).status_code
        == 413
    )
    error = client.post(url + "/dify/inspect", headers=HEADERS, content=b"key: secret-do-not-echo")
    assert error.status_code == 400 and "secret-do-not-echo" not in error.text


def test_missing_schema_fails_without_recreating_it(registry):
    req = request(registry)
    artifacts.drop(registry.engine)
    with pytest.raises(RegistryError, match="HOSTED_SCHEMA_REQUIRED"):
        registry.hosted.list(ADMIN, req.asset_id)
