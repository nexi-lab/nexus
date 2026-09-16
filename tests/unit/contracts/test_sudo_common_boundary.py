"""ADR-005 common/v1 consumer-boundary checks.

These tests deliberately read the shared sudostack schemas and fixtures instead
of restating the contract as Nexus-local dataclasses. Nexus may keep internal
types, but cross-repo/product payloads have to validate against the shared
wire contract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve()
NEXUS_REPO = HERE.parents[3]
SUDOSTACK_REPO = Path(os.environ.get("SUDOSTACK_REPO", NEXUS_REPO.parent / "sudostack"))
EXPECTED_SUDOSTACK_SHA = "65904eb0a0991366767095b707f5a86835089a1e"
SCHEMA_ROOT = SUDOSTACK_REPO / "schemas" / "common" / "v1"
FIXTURE_ROOT = SUDOSTACK_REPO / "fixtures"
SECRET_KEY_RE = re.compile(
    r"secret|credential|password|private[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key",
    re.IGNORECASE,
)


def test_common_v1_valid_fixtures_are_accepted() -> None:
    assert_sudostack_sha()
    for path in fixture_files("valid"):
        validate_common(read_json(path))


def test_common_v1_invalid_fixtures_are_rejected() -> None:
    assert_sudostack_sha()
    for path in fixture_files("invalid"):
        try:
            validate_common(read_json(path))
        except ValueError:
            continue
        raise AssertionError(f"{path.relative_to(SUDOSTACK_REPO)} should be rejected")


def test_common_v1_roundtrip_fixtures_keep_unknown_optional_fields() -> None:
    assert_sudostack_sha()
    for path in fixture_files("roundtrip"):
        value = read_json(path)
        validate_common(json.loads(json.dumps(value)))
        assert json.loads(json.dumps(value)) == value


def validate_common(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("expected object")
    reject_secret_like_keys(value)
    schema = schema_for(value)
    try:
        validator_for(schema).validate(value)
    except Exception as exc:  # jsonschema raises several ValidationError subclasses
        raise ValueError(str(exc)) from exc


def schema_for(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("api_version") != "common.sudo.dev/v1":
        raise ValueError("unsupported api_version")
    kind = value.get("kind")
    if kind == "ResourceRef":
        return read_json(SCHEMA_ROOT / "resource-ref.schema.json")
    if kind == "ErrorInfo":
        return read_json(SCHEMA_ROOT / "error-info.schema.json")
    raise ValueError(f"unsupported kind {kind!r}")


def validator_for(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(inline_refs(schema))


def inline_refs(value: Any) -> Any:
    if isinstance(value, dict):
        if "$ref" in value:
            ref = value["$ref"]
            if not isinstance(ref, str) or ref.startswith(("http://", "https://")):
                raise ValueError(f"unsupported schema ref {ref!r}")
            return inline_refs(read_json(SCHEMA_ROOT / ref))
        return {key: inline_refs(child) for key, child in value.items()}
    if isinstance(value, list):
        return [inline_refs(child) for child in value]
    return value


def reject_secret_like_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_KEY_RE.search(key):
                raise ValueError(f"forbidden inline secret-like field {key}")
            reject_secret_like_keys(child)
    elif isinstance(value, list):
        for child in value:
            reject_secret_like_keys(child)


def fixture_files(kind: str) -> list[Path]:
    return sorted((FIXTURE_ROOT / kind / "common" / "v1").rglob("*.json"))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def assert_sudostack_sha() -> None:
    actual = subprocess.check_output(["git", "-C", str(SUDOSTACK_REPO), "rev-parse", "HEAD"], text=True).strip()
    assert actual == EXPECTED_SUDOSTACK_SHA
