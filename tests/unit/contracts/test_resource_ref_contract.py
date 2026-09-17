from __future__ import annotations

import hashlib
import json
from importlib import resources
from typing import Any

import pytest

from nexus.contracts.resource_ref import (
    MAX_EXTENSION_DEPTH,
    MAX_EXTENSION_ITEMS,
    MAX_EXTENSION_PROPERTIES,
    MAX_EXTENSION_STRING_LENGTH,
    MAX_RESOURCE_REF_JSON_BYTES,
    MAX_RESOURCE_REF_PROPERTIES,
    MAX_SAFE_JSON_INTEGER,
    MIN_SAFE_JSON_INTEGER,
    ResourceRef,
    ResourceRefValidationError,
    load_nexus_vfs_source_lock,
    load_resource_ref_manifest,
    load_resource_ref_schema,
    resource_ref_validator,
    validate_resource_ref,
)

CONTRACT_ROOT = resources.files("nexus.contracts").joinpath("schemas/common/v1")


def _load_fixture(relative_path: str) -> tuple[str, dict[str, Any]]:
    text = CONTRACT_ROOT.joinpath(relative_path).read_text(encoding="utf-8")
    return text, json.loads(text)


def _manifest_cases() -> list[dict[str, Any]]:
    manifest = load_resource_ref_manifest()
    return list(manifest["fixtures"]["cases"])


@pytest.mark.parametrize("case", _manifest_cases(), ids=lambda case: case["case_id"])
def test_schema_and_adapter_agree_on_indexed_fixture(case: dict[str, Any]) -> None:
    text, payload = _load_fixture(case["path"])
    schema_errors = tuple(resource_ref_validator().iter_errors(payload))
    adapter_issues = validate_resource_ref(payload)

    schema_accepts = not schema_errors
    adapter_accepts = not adapter_issues
    assert schema_accepts is (case["schema_expected"] == "accept")
    assert adapter_accepts is (case["adapter_expected"] == "accept")

    if adapter_accepts:
        parsed = ResourceRef.from_json(text)
        reparsed = ResourceRef.from_json(parsed.to_json())
        assert reparsed.to_dict() == payload
    else:
        expected = case["expected_issue"]
        assert any(
            issue.category == expected["category"]
            and issue.path == expected["path"]
            and ("keyword" not in expected or issue.keyword == expected["keyword"])
            for issue in adapter_issues
        )
        with pytest.raises(ResourceRefValidationError):
            ResourceRef.from_json(text)


def test_every_fixture_is_indexed_once_with_exact_digest() -> None:
    manifest = load_resource_ref_manifest()
    cases = manifest["fixtures"]["cases"]
    indexed_paths = [case["path"] for case in cases]
    disk_paths = sorted(
        f"fixtures/{directory.name}/{path.name}"
        for directory in CONTRACT_ROOT.joinpath("fixtures").iterdir()
        for path in directory.iterdir()
        if path.name.endswith(".json")
    )

    assert len(indexed_paths) == len(set(indexed_paths))
    assert sorted(indexed_paths) == disk_paths
    for case in cases:
        resource = CONTRACT_ROOT.joinpath(case["path"])
        assert hashlib.sha256(resource.read_bytes()).hexdigest() == case["sha256"]

    canonical_index = json.dumps(cases, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical_index).hexdigest() == manifest["fixtures"]["index_sha256"]


def test_manifest_and_schema_digest_are_truthful_and_draft_frozen() -> None:
    manifest = load_resource_ref_manifest()
    schema_resource = CONTRACT_ROOT.joinpath("resource-ref.schema.json")

    assert (
        manifest["definition"]["source_sha256"]
        == hashlib.sha256(schema_resource.read_bytes()).hexdigest()
    )
    assert manifest["lifecycle"]["contract_baseline"] == "draft-frozen"
    assert manifest["lifecycle"]["owner_source_status"] == "immutable_owner_revision"
    assert manifest["lifecycle"]["freeze"]["state"] == "ready"
    assert manifest["lifecycle"]["artifact_publication"] == "unpublished"
    assert manifest["lifecycle"]["deployment_evidence"] == "not_deployed"
    assert manifest["definition"]["owner_revision"] == ("5139e2019d7f46d4dde3f73ee6cd21bb094a7dc1")
    assert manifest["definition"]["provenance_activation_revision"] is None
    assert manifest["conformance"]["primitive_conformance"] == "active_exact_owner_revision"
    assert manifest["conformance"]["draft_freeze"] == "ready"
    assert manifest["roles"]["actual_producers"] == []
    assert manifest["roles"]["actual_consumers"] == []
    assert manifest["roles"]["resource_ref_runtime_writer"] == "none_confirmed"
    assert manifest["roles"]["resource_ref_canonical_runtime_store"] == "none_confirmed"


def test_primitive_references_use_exact_source_lock() -> None:
    schema = load_resource_ref_schema()
    manifest = load_resource_ref_manifest()
    source_lock = load_nexus_vfs_source_lock()

    assert schema["x-sudo-max-extension-depth"] == MAX_EXTENSION_DEPTH
    assert schema["properties"]["zone_id"]["$ref"] == "urn:sudo:nexus-vfs:zone-id:v1"
    assert schema["properties"]["path"]["$ref"] == "urn:sudo:nexus-vfs:zone-path:v1"
    assert schema["x-sudo-primitive-validation"]["owner_revision"] == (
        "24f6730ec90fab8a035b2f5400ed313bda343fbe"
    )

    assert source_lock["owner_revision"] == "24f6730ec90fab8a035b2f5400ed313bda343fbe"
    assert source_lock["owner_parent_revision"] == ("557f0b9ba7c1847ae0eaadb1535ac6ea6d9099e1")
    assert manifest["owner_references"]["owner_revision"] == source_lock["owner_revision"]
    lock_resource = CONTRACT_ROOT.joinpath("nexus-vfs.source-lock.json")
    assert (
        manifest["owner_references"]["source_lock_sha256"]
        == hashlib.sha256(lock_resource.read_bytes()).hexdigest()
    )
    mappings = manifest["owner_references"]["wire_mappings"]
    assert mappings["zone_id"]["contract_id"] == "urn:sudo:nexus-vfs:zone-id:v1"
    assert mappings["zone_path"]["assertion_keyword"] == "sudoZonePath"
    assert manifest["fixtures"]["primitive_cases"]["state"] == ("integrated_exact_owner_revision")
    assert manifest["fixtures"]["primitive_cases"]["zone_id_case_count"] == 12
    assert manifest["fixtures"]["primitive_cases"]["zone_path_case_count"] == 23


def test_runtime_limits_match_canonical_schema() -> None:
    schema = load_resource_ref_schema()

    assert schema["x-sudo-max-json-bytes"] == MAX_RESOURCE_REF_JSON_BYTES
    assert schema["x-sudo-max-extension-depth"] == MAX_EXTENSION_DEPTH
    assert schema["maxProperties"] == MAX_RESOURCE_REF_PROPERTIES
    for level in range(MAX_EXTENSION_DEPTH + 1):
        definition = schema["$defs"][f"safe_extension_value_{level}"]
        assert definition["maxLength"] == MAX_EXTENSION_STRING_LENGTH
        assert definition["minimum"] == MIN_SAFE_JSON_INTEGER
        assert definition["maximum"] == MAX_SAFE_JSON_INTEGER
        if level < MAX_EXTENSION_DEPTH:
            assert definition["maxItems"] == MAX_EXTENSION_ITEMS
            assert definition["maxProperties"] == MAX_EXTENSION_PROPERTIES


def test_vendored_owner_artifacts_match_source_lock() -> None:
    source_lock = load_nexus_vfs_source_lock()
    artifact_roles = (
        "canonical_source",
        "projection",
        "fixtures",
        "meta_schema",
        "validator_lock",
    )
    checked = 0
    for contract in source_lock["contracts"].values():
        for role in artifact_roles:
            artifact = contract.get(role)
            if artifact is None:
                continue
            resource = CONTRACT_ROOT.joinpath(artifact["vendored_path"])
            data = resource.read_bytes()
            assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
            assert b"\r" not in data
            checked += 1
    assert checked == 8


def _assert_owner_cases(*, bundle_path: str, manifest_key: str, wire_field: str) -> None:
    manifest = load_resource_ref_manifest()
    bundle = json.loads(CONTRACT_ROOT.joinpath(bundle_path).read_text(encoding="utf-8"))
    indexed_cases = {
        case["owner_case_id"]: case
        for case in manifest["fixtures"]["primitive_cases"][manifest_key]
    }
    assert bundle["cases"]
    assert {case["expected"] for case in bundle["cases"]} == {"accept", "reject"}
    assert set(indexed_cases) == {case["id"] for case in bundle["cases"]}

    for case in bundle["cases"]:
        indexed = indexed_cases[case["id"]]
        canonical_case = json.dumps(
            case, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        assert hashlib.sha256(canonical_case).hexdigest() == indexed["case_sha256"]
        payload = {
            "api_version": "common.sudo.dev/v1",
            "kind": "ResourceRef",
            "zone_id": "workspace-main",
            "path": "/artifact",
            wire_field: case[indexed["value_field"]],
        }
        schema_issues = tuple(resource_ref_validator().iter_errors(payload))
        adapter_issues = validate_resource_ref(payload)
        expected = case["expected"] == "accept"
        assert (not schema_issues) is expected, case["id"]
        assert (not adapter_issues) is expected, case["id"]
        assert indexed["schema_expected"] == case["expected"]
        assert indexed["adapter_expected"] == case["expected"]
        if not expected:
            expected_issue = indexed["expected_issue"]
            assert any(
                issue.category == expected_issue["category"]
                and issue.path == expected_issue["path"]
                for issue in adapter_issues
            ), case["id"]


def test_zone_id_owner_vectors_match_schema_and_adapter() -> None:
    _assert_owner_cases(
        bundle_path="vendor/nexus-vfs/zone-id.vectors.source.json",
        manifest_key="zone_id_cases",
        wire_field="zone_id",
    )


def test_zone_path_owner_cases_match_schema_and_adapter() -> None:
    _assert_owner_cases(
        bundle_path="vendor/nexus-vfs/zone-path.cases.source.json",
        manifest_key="zone_path_cases",
        wire_field="path",
    )


@pytest.mark.parametrize("path", ["/" + chr(0xD800), "/ok/" + chr(0xDFFF)])
def test_zone_path_vocabulary_rejects_lone_surrogates(path: str) -> None:
    payload = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": path,
    }

    assert tuple(resource_ref_validator().iter_errors(payload))
    assert any(issue.category == "invalid_zone_path" for issue in validate_resource_ref(payload))


def test_resolution_policy_is_explicitly_deferred() -> None:
    policy = load_resource_ref_manifest()["resolution_policy"]

    assert policy["owner"] == "Nexus product services"
    assert policy["state"] == "deferred_separate_design"
    assert policy["zero_matching_mount_aliases"] == "deferred"
    assert policy["multiple_matching_mount_aliases"] == "deferred"
    assert policy["root_fallback"] == "deferred"
    assert policy["permission_evaluation"] == "deferred"
    assert "before authorization, routing, or storage" in policy["constraint"]


def test_manifest_distinguishes_definition_runtime_and_distribution_roles() -> None:
    roles = load_resource_ref_manifest()["roles"]

    assert roles["semantic_owner"] == "Nexus data/context/security plane"
    assert roles["authoritative_definition_writer"] == "Nexus owner maintainers"
    definition_store = roles["canonical_definition_store"]
    assert definition_store["revision"] == "5139e2019d7f46d4dde3f73ee6cd21bb094a7dc1"
    assert definition_store["path"] == (
        "src/nexus/contracts/schemas/common/v1/resource-ref.schema.json"
    )
    assert definition_store["sha256"] == (
        "15ce4b2ed53f1278a40dc313a837e170a77fb4ea026022898479dd0135b798b7"
    )
    assert roles["assembly_and_distribution_owner"] == "sudostack"
    assert roles["referenced_resource_writers_and_stores"] == (
        "unchanged_existing_domain_boundaries"
    )
    assert roles["migration_and_rollback_owner"] is None


def test_contract_resources_have_no_sibling_checkout_dependency() -> None:
    schema_text = CONTRACT_ROOT.joinpath("resource-ref.schema.json").read_text(encoding="utf-8")
    manifest_text = CONTRACT_ROOT.joinpath("resource-ref.manifest.json").read_text(encoding="utf-8")

    for forbidden in ("SUDOSTACK_REPO", "/Volumes/", "../sudostack", "../nexus-vfs"):
        assert forbidden not in schema_text
        assert forbidden not in manifest_text
