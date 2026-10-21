"""Deterministic owner artifacts. Consumer repositories copy these exact committed bytes."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import TypeAdapter

from nexus.contracts.agent_evaluation import (
    CompleteHostedEvaluationCase,
    CreateMigrationPolicy,
    EvaluationCaseLease,
    EvaluationDataset,
    EvaluationDatasetPage,
    HostedEvaluationCase,
    HostedEvaluationCasePage,
    HostedEvaluationClaim,
    HostedEvaluationPage,
    HostedEvaluationRun,
    MigrationComparison,
    MigrationPolicy,
    MigrationPolicyPage,
    RegisterEvaluationDataset,
    StartHostedEvaluation,
)
from nexus.contracts.agent_execution import (
    CheckExecutionGrant,
    ExecutionAuthorization,
    ExecutionGrant,
    ExecutionGrantPage,
    ExecutionGrantResult,
    IssueExecutionGrant,
)
from nexus.contracts.agent_hosted import (
    DifyTextPlan,
    HostedArtifact,
    HostedArtifactPage,
    HostedImage,
    RegisterHostedArtifact,
)
from nexus.contracts.agent_inventory import (
    AssetCommand,
    AssetHistory,
    AssetPage,
    AssetRecord,
    PlatformDiscoveryPage,
)
from nexus.contracts.product_agent_registry import (
    API,
    CommandResult,
    RegistryCommand,
    Resolution,
    SignedVersion,
    VersionItem,
    VersionManifest,
    VersionsPage,
    canonical_bytes,
)
from nexus.contracts.resource_ref import load_resource_ref_schema
from nexus.services.product_agent_registry.signing import key_id

ROOT = Path(__file__).resolve().parents[2] / "contracts" / "schemas" / "agent-registry" / "v1alpha1"


def artifacts() -> dict[str, str]:
    wire = TypeAdapter(
        RegistryCommand
        | CommandResult
        | Resolution
        | VersionsPage
        | VersionItem
        | AssetCommand
        | AssetRecord
        | AssetPage
        | AssetHistory
        | PlatformDiscoveryPage
        | IssueExecutionGrant
        | ExecutionGrant
        | ExecutionGrantResult
        | ExecutionGrantPage
        | CheckExecutionGrant
        | ExecutionAuthorization
        | DifyTextPlan
        | HostedImage
        | RegisterHostedArtifact
        | HostedArtifact
        | HostedArtifactPage
        | EvaluationDataset
        | EvaluationDatasetPage
        | RegisterEvaluationDataset
        | EvaluationCaseLease
        | CompleteHostedEvaluationCase
        | HostedEvaluationRun
        | HostedEvaluationCase
        | HostedEvaluationPage
        | HostedEvaluationCasePage
        | HostedEvaluationClaim
        | StartHostedEvaluation
        | CreateMigrationPolicy
        | MigrationPolicy
        | MigrationPolicyPage
        | MigrationComparison
    )
    schema = wire.json_schema()

    def resource_refs(node):
        if isinstance(node, dict):
            if "x-sudo-resource-ref" in node:
                return {"$ref": node["x-sudo-resource-ref"]}
            return {name: resource_refs(value) for name, value in node.items()}
        if isinstance(node, list):
            return [resource_refs(value) for value in node]
        return node

    schema = resource_refs(schema)
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://schemas.sudo.dev/experimental/agent-registry/v1alpha1",
        }
    )

    def ts(node):
        if "$ref" in node:
            reference = node["$ref"]
            if reference == load_resource_ref_schema()["$id"]:
                return "ResourceReference"
            if not reference.startswith("#/$defs/"):
                raise ValueError("unsupported contract reference")
            return reference.split("/")[-1]
        if "const" in node:
            return json.dumps(node["const"])
        if "enum" in node:
            return " | ".join(json.dumps(value) for value in node["enum"])
        for union in ("anyOf", "oneOf"):
            if union in node:
                return "(" + " | ".join(ts(value) for value in node[union]) + ")"
        kind = node.get("type")
        if kind == "object":
            if "properties" not in node:
                return "Record<string, unknown>"
            return (
                "{ "
                + " ".join(
                    f"{name}{'' if name in node.get('required', []) else '?'}: {ts(value)};"
                    for name, value in node["properties"].items()
                )
                + " }"
            )
        if kind == "array":
            return "Array<" + ts(node["items"]) + ">"
        if kind in ("string", "boolean", "null"):
            return kind
        if kind in ("integer", "number"):
            return "number"
        raise ValueError("unsupported schema shape")

    types = [
        "// Generated from Nexus product_agent_registry.py. DO NOT EDIT.",
        f"export const AGENT_REGISTRY_API = {json.dumps(API)} as const",
        "/** Runtime validation uses the existing ResourceRef owner schema and adapter. */",
        "export type ResourceReference = Record<string, unknown>",
    ]
    types.extend(
        f"export type {name} = {ts(value)}" for name, value in sorted(schema["$defs"].items())
    )
    types.append(
        "export type RegistryWire = "
        + ts(
            TypeAdapter(
                RegistryCommand | CommandResult | Resolution | VersionsPage | VersionItem
            ).json_schema()
        )
    )
    types.append(
        "export type InventoryWire = "
        + ts(TypeAdapter(AssetCommand | AssetRecord | AssetPage | AssetHistory).json_schema())
    )
    types.append(
        "export type ExecutionWire = "
        + ts(
            TypeAdapter(
                IssueExecutionGrant
                | ExecutionGrant
                | ExecutionGrantResult
                | ExecutionGrantPage
                | CheckExecutionGrant
                | ExecutionAuthorization
            ).json_schema()
        )
    )

    # Public deterministic conformance key. Never use this fixture key in a deployment.
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    manifest = VersionManifest(
        principal={"trust_domain": "fixture.example", "agent_name": "assistant"},
        version="v1",
        spec_ref={
            "api_version": "common.sudo.dev/v1",
            "kind": "ResourceRef",
            "zone_id": "fixture-zone",
            "path": "/agents/spec.json",
            "digest": "sha256:" + "a" * 64,
        },
        spec_digest="sha256:" + "a" * 64,
        source_evidence_refs=[],
        created_by="user:fixture",
        created_at=1000,
    )
    payload = canonical_bytes(manifest.model_dump())
    signed = SignedVersion(
        manifest=manifest,
        payload_base64=base64.b64encode(payload).decode(),
        digest="sha256:" + hashlib.sha256(payload).hexdigest(),
        key_id=key_id(key.public_key()),
        signature_base64=base64.b64encode(key.sign(payload)).decode(),
    )
    fixture = {
        "purpose": "public_test_key_only",
        "public_key_pem": key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
        "version": signed.model_dump(),
    }
    return {
        "schema.json": json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
        "contract.gen.ts": "\n".join(types) + "\n",
        "signed-version.fixture.json": json.dumps(fixture, indent=2) + "\n",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, content in artifacts().items():
        path = ROOT / name
        if args.check:
            if path.read_text() != content:
                raise ValueError(f"STALE_REGISTRY_ARTIFACT: {name}")
        else:
            ROOT.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


if __name__ == "__main__":
    main()
