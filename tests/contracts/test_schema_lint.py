"""Schema lint: structural rules every owned schema must satisfy, offline
$ref resolution, manifest/digest integrity and single-pin guarantees."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry

from tests.contracts.conftest import CONTRACTS_DIR, OWNED_SCHEMAS, VENDOR_DIR

CARGO_TOML = CONTRACTS_DIR.parent / "Cargo.toml"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_owned_schema_declares_draft_and_stable_id(owned_schemas: dict[str, dict]) -> None:
    for rel, doc in owned_schemas.items():
        assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema", rel
        assert doc["$id"].startswith("https://contracts.sudo.dev/schemas/"), rel
        assert doc.get("title"), rel


def test_schema_ids_are_unique(owned_schemas: dict[str, dict]) -> None:
    ids = [doc["$id"] for doc in owned_schemas.values()]
    assert len(ids) == len(set(ids))


def test_refs_resolve_offline(owned_schemas: dict[str, dict], registry: Registry) -> None:
    """$id/$ref resolution must never touch the network (§7.5 item 2)."""
    for doc in owned_schemas.values():
        validator = Draft202012Validator(doc, registry=registry)
        validator.check_schema(doc)


def test_vendored_projections_match_source_lock_digests() -> None:
    lock = _load(CONTRACTS_DIR / "source-lock.gen.json")
    for entry in lock["files"]:
        path = CONTRACTS_DIR.parent / entry["path"]
        assert path.exists(), entry["path"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == entry["sha256"], f"{entry['path']} drifted from source lock"


def test_source_lock_matches_the_single_cargo_pin() -> None:
    """The lock derives from Cargo.toml; two pins would defeat the §7.7 gate."""
    lock = _load(CONTRACTS_DIR / "source-lock.gen.json")
    text = CARGO_TOML.read_text(encoding="utf-8")
    revs = set(
        re.findall(
            r'git\s*=\s*"https://github\.com/nexi-lab/nexus-vfs"\s*,\s*rev\s*=\s*"([0-9a-f]{40})"',
            text,
        )
    )
    assert revs == {lock["rev"]}, f"cargo pins {revs} vs lock {lock['rev']}"


def test_vendor_directory_exactly_matches_lock_listing() -> None:
    lock = _load(CONTRACTS_DIR / "source-lock.gen.json")
    listed = {Path(e["path"]).name for e in lock["files"]}
    present = {p.name for p in VENDOR_DIR.glob("*.json")}
    assert listed == present


def test_manifest_covers_every_owned_schema_with_real_digests() -> None:
    manifest = _load(CONTRACTS_DIR / "manifests" / "zone-v1.manifest.gen.json")
    covered = {s["schema_path"] for s in manifest["schemas"]}
    expected = {f"contracts/{rel}" for rel in OWNED_SCHEMAS}
    assert covered == expected
    for entry in manifest["schemas"]:
        path = CONTRACTS_DIR.parent / entry["schema_path"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["schema_digest"] == f"sha256:{digest}", entry["schema_path"]
        for key in (
            "owner",
            "security_owner",
            "producers",
            "consumers",
            "data_classification",
            "compatibility",
            "secrets_allowed",
        ):
            assert key in entry, f"{entry['schema_path']} missing {key}"
        assert entry["secrets_allowed"] is False


@pytest.mark.parametrize("rel", OWNED_SCHEMAS)
def test_wire_rules_on_top_level_objects(rel: str, owned_schemas: dict[str, dict]) -> None:
    doc = owned_schemas[rel]
    props = doc.get("properties", {})
    if "api_version" in props:
        assert props["api_version"].get("const"), f"{rel}: api_version must be a const major"
        assert "api_version" in doc["required"]
        assert props["kind"].get("const"), f"{rel}: kind must be a const discriminator"
        assert "kind" in doc["required"]
    # Records accept unknown optionals; only the patch whitelist may forbid.
    if rel != "auth/v1/zone-patch-request.schema.json":
        assert doc.get("additionalProperties", True) is True, (
            f"{rel} must keep unknown optionals open"
        )
