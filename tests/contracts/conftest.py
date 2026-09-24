"""Shared fixtures for the contract conformance suite.

Loads every schema (owned + vendored nexus-vfs projections) into an offline
referencing Registry so `$ref` resolution never touches the network, and
exposes the fixture corpus as plain data for parametrized tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from nexus.contracts.zone_v1 import (
    PrincipalRef,
    ResourceRef,
    RuntimeResourceScope,
    Zone,
    ZoneCreateRequest,
    ZoneDelegation,
    ZoneDelegationIssueRequest,
    ZoneDelegationScopeRule,
    ZoneGrant,
    ZoneGrantCreateRequest,
    ZoneOperation,
    ZonePatchRequest,
)

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"

OWNED_SCHEMAS = [
    "common/v1/principal-ref.schema.json",
    "common/v1/resource-ref.schema.json",
    "auth/v1/zone.schema.json",
    "auth/v1/zone-create-request.schema.json",
    "auth/v1/zone-patch-request.schema.json",
    "auth/v1/zone-grant.schema.json",
    "auth/v1/zone-grant-create-request.schema.json",
    "auth/v1/zone-operation.schema.json",
    "auth/v1/zone-delegation-scope-rule.schema.json",
    "auth/v1/zone-delegation-issue-request.schema.json",
    "auth/v1/zone-delegation.schema.json",
    "runtime/v2/runtime-resource-scope.schema.json",
]

VENDOR_DIR = CONTRACTS_DIR / "vendor" / "nexus-vfs.gen"

#: schema relative path -> Pydantic model (the conformance-verified adapter)
SCHEMA_TO_MODEL = {
    "common/v1/principal-ref.schema.json": PrincipalRef,
    "common/v1/resource-ref.schema.json": ResourceRef,
    "auth/v1/zone.schema.json": Zone,
    "auth/v1/zone-create-request.schema.json": ZoneCreateRequest,
    "auth/v1/zone-patch-request.schema.json": ZonePatchRequest,
    "auth/v1/zone-grant.schema.json": ZoneGrant,
    "auth/v1/zone-grant-create-request.schema.json": ZoneGrantCreateRequest,
    "auth/v1/zone-operation.schema.json": ZoneOperation,
    "auth/v1/zone-delegation-scope-rule.schema.json": ZoneDelegationScopeRule,
    "auth/v1/zone-delegation-issue-request.schema.json": ZoneDelegationIssueRequest,
    "auth/v1/zone-delegation.schema.json": ZoneDelegation,
    "runtime/v2/runtime-resource-scope.schema.json": RuntimeResourceScope,
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def registry() -> Registry:
    resources: list[tuple[str, Resource]] = []
    for rel in OWNED_SCHEMAS:
        doc = _load(CONTRACTS_DIR / rel)
        resources.append(
            (doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
        )
    for vendor_file in sorted(VENDOR_DIR.glob("*.json")):
        doc = _load(vendor_file)
        resources.append(
            (doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
        )
    return Registry().with_resources(resources)


@pytest.fixture(scope="session")
def owned_schemas(registry: Registry) -> dict[str, dict[str, Any]]:
    return {rel: _load(CONTRACTS_DIR / rel) for rel in OWNED_SCHEMAS}


def load_cases(group: str, filename: str = "cases.json") -> list[dict[str, Any]]:
    raw = _load(CONTRACTS_DIR / "fixtures" / group / filename)
    return raw["cases"]


def all_fixture_cases() -> list[dict[str, Any]]:
    """Every case across every group, tagged with its group for reporting."""
    cases: list[dict[str, Any]] = []
    for group in ["valid", "invalid", "roundtrip"]:
        for case in load_cases(group):
            cases.append({"group": group, **case})
    return cases
