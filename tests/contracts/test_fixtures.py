"""Fixture conformance: the shared fixtures are the final judge. Every case is
judged twice — by the JSON Schema (with offline $ref registry) and by the
owner-local Pydantic adapter — and the two verdicts must agree."""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError
from referencing import Registry

from tests.contracts.conftest import (
    CONTRACTS_DIR,
    OWNED_SCHEMAS,
    SCHEMA_TO_MODEL,
    load_cases,
)


def _validator_for(
    schema_rel: str, registry: Registry, schemas: dict[str, dict]
) -> Draft202012Validator:
    return Draft202012Validator(schemas[schema_rel], registry=registry)


def _schema_jsonschema_ok(schema_rel: str, payload: Any, registry: Registry, schemas: dict) -> bool:
    return _validator_for(schema_rel, registry, schemas).is_valid(payload)


def _model_for(schema_rel: str) -> type[BaseModel]:
    return SCHEMA_TO_MODEL[schema_rel]


def _model_ok(schema_rel: str, payload: Any) -> bool:
    try:
        _model_for(schema_rel).model_validate(payload)
    except ValidationError:
        return False
    return True


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict]:
    return {
        rel: json.loads((CONTRACTS_DIR / rel).read_text(encoding="utf-8")) for rel in OWNED_SCHEMAS
    }


@pytest.fixture(scope="module")
def registry() -> Registry:
    from referencing import Resource
    from referencing.jsonschema import DRAFT202012

    from tests.contracts.conftest import VENDOR_DIR

    resources = []
    for doc in (json.loads((CONTRACTS_DIR / r).read_text(encoding="utf-8")) for r in OWNED_SCHEMAS):
        resources.append(
            (doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
        )
    for vf in sorted(VENDOR_DIR.glob("*.json")):
        doc = json.loads(vf.read_text(encoding="utf-8"))
        resources.append(
            (doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
        )
    return Registry().with_resources(resources)


@pytest.mark.parametrize("case", load_cases("valid"), ids=lambda c: c.get("name", ""))
def test_valid_fixtures_accepted_by_schema_and_model(
    case: dict, schemas: dict, registry: Registry
) -> None:
    assert _schema_jsonschema_ok(case["schema"], case["payload"], registry, schemas)
    assert _model_ok(case["schema"], case["payload"])


@pytest.mark.parametrize("case", load_cases("invalid"), ids=lambda c: c.get("name", ""))
def test_invalid_fixtures_rejected_by_schema_and_model(
    case: dict, schemas: dict, registry: Registry
) -> None:
    assert not _schema_jsonschema_ok(case["schema"], case["payload"], registry, schemas)
    assert not _model_ok(case["schema"], case["payload"])


@pytest.mark.parametrize("case", load_cases("roundtrip"), ids=lambda c: c.get("name", ""))
def test_roundtrip_semantic_equivalence(case: dict, schemas: dict, registry: Registry) -> None:
    model = _model_for(case["schema"])
    first = model.model_validate(case["payload"])
    dumped = first.model_dump(mode="json")
    second = model.model_validate(dumped)
    assert second.model_dump(mode="json") == dumped


def test_unknown_optional_accepted_and_dropped_from_model(
    schemas: dict, registry: Registry
) -> None:
    for case in load_cases("valid"):
        payload = case["payload"]
        unknown = [
            k for k in payload if k.startswith("x_future") or k in ("api_key", "token", "password")
        ]
        if not unknown:
            continue
        model = _model_for(case["schema"]).model_validate(payload)
        dumped = model.model_dump(mode="json")
        for key in unknown:
            assert key not in dumped, f"{case['name']}: {key} leaked into the model output"


def test_secret_negative_two_layer_semantics(schemas: dict, registry: Registry) -> None:
    doc = json.loads(
        (CONTRACTS_DIR / "fixtures" / "secret-negative" / "cases.json").read_text(encoding="utf-8")
    )
    styled = set(doc["secret_style_field_names"])
    # (a) no contract field is a secret carrier.
    for rel in OWNED_SCHEMAS:
        props = json.loads((CONTRACTS_DIR / rel).read_text(encoding="utf-8")).get("properties", {})
        assert not (styled & set(props)), f"{rel} declares a secret-styled field"
    # (b) secret-styled unknown optionals are accepted at the wire layer and
    #     dropped by the object model.
    for case in doc["cases"]:
        if case.get("expect") != "accepted-and-dropped":
            continue
        assert _schema_jsonschema_ok(case["schema"], case["payload"], registry, schemas)
        model = _model_for(case["schema"]).model_validate(case["payload"])
        dumped = model.model_dump(mode="json")
        for key in case["drop_fields"]:
            assert key not in dumped


def test_path_traversal_rejected(schemas: dict, registry: Registry) -> None:
    doc = json.loads(
        (CONTRACTS_DIR / "fixtures" / "path-traversal" / "cases.json").read_text(encoding="utf-8")
    )
    assert doc["cases"], "path-traversal corpus must not be empty"
    for case in doc["cases"]:
        assert not _schema_jsonschema_ok(case["schema"], case["payload"], registry, schemas), case[
            "name"
        ]
        assert not _model_ok(case["schema"], case["payload"]), case["name"]


def test_compatibility_previous_minor_verdicts_hold(schemas: dict, registry: Registry) -> None:
    doc = json.loads(
        (CONTRACTS_DIR / "fixtures" / "compatibility" / "previous-minor.json").read_text(
            encoding="utf-8"
        )
    )
    for case in doc["cases"]:
        schema_ok = _schema_jsonschema_ok(case["schema"], case["payload"], registry, schemas)
        model_ok = _model_ok(case["schema"], case["payload"])
        expected = case["expected"] == "valid"
        assert schema_ok is expected, case["name"]
        assert model_ok is expected, case["name"]


@pytest.mark.parametrize("group", ["valid", "invalid"])
def test_schema_and_model_verdicts_agree_on_everything(
    group: str, schemas: dict, registry: Registry
) -> None:
    """The conformance contract: both judges must always agree, case by case."""
    for case in load_cases(group):
        s = _schema_jsonschema_ok(case["schema"], case["payload"], registry, schemas)
        m = _model_ok(case["schema"], case["payload"])
        assert s == m, f"{group}/{case['name']}: schema={s} model={m}"
