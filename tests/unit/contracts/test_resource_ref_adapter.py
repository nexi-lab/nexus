from __future__ import annotations

import json
import subprocess
import sys
import traceback
from types import MappingProxyType

import pytest

from nexus.contracts.resource_ref import (
    MAX_RESOURCE_REF_JSON_BYTES,
    FrozenJsonValue,
    JsonValue,
    ResourceRef,
    ResourceRefValidationError,
    load_resource_ref_schema,
    require_resource_ref_draft_freeze,
    require_resource_ref_primitive_validation,
    resource_ref_validator,
    validate_resource_ref,
)


def test_unknown_optional_fields_survive_real_json_roundtrip_and_are_immutable() -> None:
    trace_input: dict[str, JsonValue] = {
        "source": "future-producer",
        "tags": ["finance", "reviewed"],
        "note": None,
    }
    source: dict[str, JsonValue] = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/reports/quarterly.pdf",
        "trace_context": trace_input,
    }

    resource_ref = ResourceRef.from_dict(source)
    trace_input["source"] = "mutated-after-parse"

    trace_context = resource_ref.additional_properties["trace_context"]
    assert isinstance(trace_context, MappingProxyType)
    assert trace_context["source"] == "future-producer"
    assert trace_context["tags"] == ("finance", "reviewed")
    assert not hasattr(trace_context, "__setitem__")

    reparsed = ResourceRef.from_json(resource_ref.to_json())
    assert reparsed.to_dict()["trace_context"] == {
        "source": "future-producer",
        "tags": ["finance", "reviewed"],
        "note": None,
    }


def test_direct_constructor_copies_nested_mapping_proxies() -> None:
    backing: dict[str, FrozenJsonValue] = {"note": "safe"}
    nested = MappingProxyType(backing)
    additional: dict[str, FrozenJsonValue] = {"vendor_metadata": nested}

    resource_ref = ResourceRef(
        zone_id="workspace-main",
        path="/reports/file.txt",
        additional_properties=MappingProxyType(additional),
    )
    backing["api_key"] = "added-after-validation"

    assert resource_ref.to_dict()["vendor_metadata"] == {"note": "safe"}


def test_absent_known_optionals_remain_absent_not_null() -> None:
    resource_ref = ResourceRef(zone_id="workspace-main", path="/reports/minimal.txt")

    assert resource_ref.version is None
    assert resource_ref.digest is None
    assert resource_ref.media_type is None
    assert resource_ref.size_bytes is None
    assert resource_ref.to_dict() == {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/reports/minimal.txt",
    }


def test_decimal_size_is_not_limited_by_machine_integer_width() -> None:
    size = "184467440737095516160000000000000000000"
    resource_ref = ResourceRef(
        zone_id="workspace-main",
        path="/large/sparse.bin",
        size_bytes=size,
    )

    assert resource_ref.to_dict()["size_bytes"] == size
    assert json.loads(resource_ref.to_json())["size_bytes"] == size


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "rev\n"),
        ("digest", "sha256:abcd\n"),
        ("media_type", "text/plain\n"),
        ("size_bytes", "1\n"),
    ],
)
def test_canonical_fields_reject_terminal_newline(field: str, value: str) -> None:
    payload: dict[str, JsonValue] = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/reports/file.txt",
        field: value,
    }

    with pytest.raises(ResourceRefValidationError):
        ResourceRef.from_dict(payload)


def test_direct_construction_cannot_bypass_validation() -> None:
    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef(
            zone_id="workspace-main",
            path="/reports/file.txt",
            version="",
        )

    assert exc_info.value.issues[0].category == "invalid_version"


def test_document_root_issue_uses_empty_json_pointer() -> None:
    issues = validate_resource_ref({})

    assert issues
    assert all(issue.path == "" for issue in issues)


def test_parser_rejects_duplicate_properties() -> None:
    payload = (
        '{"api_version":"common.sudo.dev/v1","kind":"ResourceRef",'
        '"zone_id":"one","zone_id":"two","path":"/file"}'
    )

    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef.from_json(payload)

    assert [issue.category for issue in exc_info.value.issues] == ["duplicate_property"]


def test_parser_rejects_non_finite_numbers() -> None:
    payload = (
        '{"api_version":"common.sudo.dev/v1","kind":"ResourceRef",'
        '"zone_id":"workspace-main","path":"/file","future_value":NaN}'
    )

    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef.from_json(payload)

    assert [issue.category for issue in exc_info.value.issues] == ["non_finite_number"]


@pytest.mark.parametrize("number", ["1.0000000000000001", "1e-1000"])
def test_parser_rejects_fractional_numbers_before_precision_loss(number: str) -> None:
    payload = (
        '{"api_version":"common.sudo.dev/v1","kind":"ResourceRef",'
        f'"zone_id":"workspace-main","path":"/file","future_value":{number}}}'
    )

    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef.from_json(payload)

    assert [issue.category for issue in exc_info.value.issues] == ["fractional_extension_number"]
    assert number not in "".join(traceback.format_exception(exc_info.value))


def test_parser_wraps_unsafe_integer_as_validation_error() -> None:
    payload = (
        '{"api_version":"common.sudo.dev/v1","kind":"ResourceRef",'
        f'"zone_id":"workspace-main","path":"/file","future_value":{("9" * 5000)}}}'
    )

    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef.from_json(payload)

    assert [issue.category for issue in exc_info.value.issues] == ["unsafe_extension_integer"]


def test_parser_wraps_extreme_exponent_as_validation_error() -> None:
    payload = (
        '{"api_version":"common.sudo.dev/v1","kind":"ResourceRef",'
        '"zone_id":"workspace-main","path":"/file",'
        '"future_value":1e9999999999999999999}'
    )

    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef.from_json(payload)

    assert [issue.category for issue in exc_info.value.issues] == ["unsafe_extension_integer"]


def test_direct_input_wraps_unsafe_integer_as_validation_error() -> None:
    payload: dict[str, JsonValue] = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/file",
        "future_value": 10**5000,
    }

    assert [issue.category for issue in validate_resource_ref(payload)] == [
        "unsafe_extension_integer"
    ]
    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef.from_dict(payload)
    assert [issue.category for issue in exc_info.value.issues] == ["unsafe_extension_integer"]


def test_parser_rejects_oversized_document_before_decoding_values() -> None:
    payload = b"{" + b" " * MAX_RESOURCE_REF_JSON_BYTES + b"}"

    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef.from_json(payload)

    assert [issue.category for issue in exc_info.value.issues] == ["document_too_large"]


def test_public_validator_applies_document_size_policy() -> None:
    payload: dict[str, JsonValue] = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/file",
        "future_metadata": ["a" * 4096] * 16,
    }

    assert [issue.category for issue in validate_resource_ref(payload)] == ["document_too_large"]


def test_shared_container_expansion_is_bounded_before_serialization() -> None:
    nested: JsonValue = 0
    for _ in range(8):
        nested = [nested] * 64
    payload: dict[str, JsonValue] = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/file",
        "future_metadata": nested,
    }

    assert [issue.category for issue in validate_resource_ref(payload)] == ["document_too_large"]


def test_expanded_string_bytes_are_bounded_before_schema_validation() -> None:
    leaf = "a" * 4096
    row: JsonValue = [leaf] * 64
    payload: dict[str, JsonValue] = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/file",
        "future_metadata": [row] * 16,
    }

    assert [issue.category for issue in validate_resource_ref(payload)] == ["document_too_large"]

    frozen_row: FrozenJsonValue = (leaf,) * 64
    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef(
            zone_id="workspace-main",
            path="/file",
            additional_properties={"future_metadata": (frozen_row,) * 16},
        )
    assert [issue.category for issue in exc_info.value.issues] == ["document_too_large"]


def test_root_property_count_is_rejected_before_extension_traversal() -> None:
    payload: dict[str, JsonValue] = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/file",
    }
    payload.update({f"extra_{index}": None for index in range(29)})

    assert [issue.category for issue in validate_resource_ref(payload)] == ["too_many_properties"]
    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef(
            zone_id="workspace-main",
            path="/file",
            additional_properties={f"extra_{index}": None for index in range(29)},
        )
    assert [issue.category for issue in exc_info.value.issues] == ["too_many_properties"]


def test_public_validator_registers_zone_path_dialect_on_first_use() -> None:
    script = """
from nexus.contracts.resource_ref import resource_ref_validator
payload = {
    "api_version": "common.sudo.dev/v1",
    "kind": "ResourceRef",
    "zone_id": "workspace-main",
    "path": "/../escape",
}
assert not resource_ref_validator().is_valid(payload)
"""
    subprocess.run(
        [sys.executable, "-B", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )


def test_external_validator_mutation_does_not_change_adapter_state() -> None:
    validator = resource_ref_validator()
    validator.schema["properties"]["path"] = {"type": "string"}
    public_registry = validator._registry
    public_zone_id = public_registry.contents("urn:sudo:nexus-vfs:zone-id:v1")
    public_zone_id["minLength"] = 1

    assert load_resource_ref_schema()["properties"]["path"]["$ref"] == (
        "urn:sudo:nexus-vfs:zone-path:v1"
    )
    fresh_registry = resource_ref_validator()._registry
    assert fresh_registry.contents("urn:sudo:nexus-vfs:zone-id:v1")["minLength"] == 3

    path_issues = validate_resource_ref(
        {
            "api_version": "common.sudo.dev/v1",
            "kind": "ResourceRef",
            "zone_id": "workspace-main",
            "path": "/../escape",
        }
    )
    zone_id_issues = validate_resource_ref(
        {
            "api_version": "common.sudo.dev/v1",
            "kind": "ResourceRef",
            "zone_id": "a",
            "path": "/file",
        }
    )
    assert [issue.category for issue in path_issues] == ["invalid_zone_path"]
    assert [issue.category for issue in zone_id_issues] == ["invalid_zone_id"]


def test_invalid_property_name_has_stable_category() -> None:
    payload: dict[str, JsonValue] = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/file",
        "Bad-Field": 0,
    }

    issues = validate_resource_ref(payload)
    assert [(issue.category, issue.path, issue.keyword) for issue in issues] == [
        ("invalid_property_name", "", "propertyNames")
    ]


def test_rejected_property_name_is_not_reflected_in_diagnostics() -> None:
    rejected_name = "\n[INFO] access_token=not-for-logs"
    payload: dict[str, JsonValue] = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/file",
        "future_metadata": {rejected_name: "x" * 4097},
    }

    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef.from_dict(payload)
    assert rejected_name not in str(exc_info.value)
    assert exc_info.value.issues[0].path == "/future_metadata"
    assert exc_info.value.issues[0].category == "secret_like_extension_key"


def test_extension_depth_is_bounded_before_schema_recursion() -> None:
    nested: JsonValue = "leaf"
    for index in range(200):
        nested = {f"level_{index}": nested}
    payload: dict[str, JsonValue] = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/file",
        "future_metadata": nested,
    }

    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef.from_dict(payload)

    assert [issue.category for issue in exc_info.value.issues] == ["extension_too_deep"]


def test_direct_constructor_depth_is_bounded_before_thawing() -> None:
    nested: FrozenJsonValue = "leaf"
    for _ in range(1_100):
        nested = (nested,)

    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef(
            zone_id="workspace-main",
            path="/file",
            additional_properties={"future_metadata": nested},
        )
    assert [issue.category for issue in exc_info.value.issues] == ["extension_too_deep"]


def test_secret_values_are_not_reflected_in_validation_errors() -> None:
    secret = "do-not-reflect-this-value"
    payload: dict[str, JsonValue] = {
        "api_version": "common.sudo.dev/v1",
        "kind": "ResourceRef",
        "zone_id": "workspace-main",
        "path": "/file",
        "vendor_metadata": {"api_key": secret},
    }

    with pytest.raises(ResourceRefValidationError) as exc_info:
        ResourceRef.from_dict(payload)

    assert secret not in str(exc_info.value)
    assert all(secret not in issue.message for issue in exc_info.value.issues)


def test_primitive_validation_and_draft_freeze_are_available() -> None:
    require_resource_ref_primitive_validation()
    require_resource_ref_draft_freeze()
