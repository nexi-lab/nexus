"""Immutable dataset versions and owner API authorization, no execution or grade changes."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event

from nexus.contracts.agent_evaluation import EvaluationDatasetInput, RegisterEvaluationDataset
from nexus.services.product_agent_registry.store import RegistryActor, RegistryError
from tests.unit.services.test_agent_inventory import cmd, save
from tests.unit.services.test_product_agent_registry import ADMIN, MEMBER
from tests.unit.services.test_product_agent_registry import registry as registry
from tests.unit.services.test_product_agent_registry_api import BASE, HEADERS
from tests.unit.services.test_product_agent_registry_api import client as client


def request(registry):
    asset = save(registry, hosting="internal_hosted", requested_level="L2", model_paths=["m"])
    return RegisterEvaluationDataset(
        request_id=uuid4().hex,
        asset_id=asset.id,
        metadata_digest=asset.metadata_digest,
        initiated_by="alice",
        dataset=EvaluationDatasetInput(
            name="Arithmetic",
            version="1",
            cases=[{"id": "one", "query": "6 * 7", "expected": "42"}],
        ),
    )


def test_versioned_dataset_replay_scoping_and_readonly_get(registry):
    req = request(registry)
    result = registry.evaluation.register(ADMIN, req)
    assert registry.evaluation.register(ADMIN, req) == result
    assert registry.evaluation.list(ADMIN, req.asset_id).items == [result]
    assert registry.inventory.get(ADMIN, req.asset_id).actual_level == "L0"
    assert result.dataset_digest != result.rubric_digest
    with pytest.raises(RegistryError):
        registry.evaluation.get(MEMBER, result.id)
    with pytest.raises(RegistryError, match="EVALUATION_DATASET_NOT_FOUND"):
        registry.evaluation.get(
            RegistryActor(subject=ADMIN.subject, zone="zone-b", is_admin=True), result.id
        )
    sql = []

    def capture(_con, _cursor, statement, *_args):
        sql.append(statement.strip().split()[0].upper())

    event.listen(registry.engine, "before_cursor_execute", capture)
    try:
        registry.evaluation.get(ADMIN, result.id)
        registry.evaluation.list(ADMIN, req.asset_id)
    finally:
        event.remove(registry.engine, "before_cursor_execute", capture)
    assert not set(sql) & {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"}


def test_dataset_version_is_not_overwritable(registry):
    req = request(registry)
    original = registry.evaluation.register(ADMIN, req)
    with pytest.raises(RegistryError, match="EVALUATION_IDEMPOTENCY_CONFLICT"):
        registry.evaluation.register(ADMIN, req.model_copy(update={"initiated_by": "someone"}))
    changed = req.model_copy(update={"request_id": uuid4().hex})
    with pytest.raises(RegistryError, match="EVALUATION_VERSION_CONFLICT"):
        registry.evaluation.register(ADMIN, changed)
    next_version = changed.model_copy(
        update={"dataset": changed.dataset.model_copy(update={"version": "2"})}
    )
    assert (
        registry.evaluation.register(ADMIN, next_version).dataset_digest
        != original.dataset_digest
    )


def test_changed_metadata_requires_new_dataset_binding(registry):
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
    with pytest.raises(RegistryError, match="EVALUATION_ASSET_NOT_ADMITTED"):
        registry.evaluation.register(ADMIN, req)


def test_concurrent_identical_registration_is_one_version(registry):
    req = request(registry)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: registry.evaluation.register(ADMIN, req), range(4)))
    assert len({row.id for row in rows}) == 1


def test_duplicate_cases_and_unbounded_or_non_synthetic_input_are_rejected():
    one = {"id": "one", "query": "q", "expected": "a"}
    for fields in [
        {"cases": [one, one]},
        {"cases": []},
        {"cases": [one], "input_scope": "production"},
        {"cases": [one], "rubric": "execute-python"},
    ]:
        with pytest.raises(ValidationError):
            EvaluationDatasetInput(name="Dataset", version="1", **fields)


def test_dataset_api_and_authentication(client, registry):
    req = request(registry)
    url = BASE + "/hosted/datasets"
    response = client.post(url, headers=HEADERS, json=req.model_dump())
    assert response.status_code == 200, response.text
    result = response.json()
    assert client.get(url + "/" + result["id"], headers=HEADERS).json() == result
    assert client.get(url + "?asset_id=" + req.asset_id, headers=HEADERS).json()["items"] == [
        result
    ]
    assert client.get(url + "/" + result["id"]).status_code == 401
    assert (
        client.post(
            url, headers={"Authorization": "Bearer sk-test-member"}, json=req.model_dump()
        ).status_code
        == 403
    )
    assert (
        client.get(
            url + "/" + result["id"], headers={**HEADERS, "X-Nexus-Zone-ID": "zone-b"}
        ).status_code
        == 404
    )
