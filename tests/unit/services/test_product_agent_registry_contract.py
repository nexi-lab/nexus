"""The public wire artifacts are derived from the executable owner models."""

import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from nexus.contracts.product_agent_registry import SignedVersion, parse_command
from nexus.contracts.resource_ref import load_resource_ref_schema
from nexus.server.api.v2.routers.product_agent_registry import router
from nexus.services.product_agent_registry.configuration import configure
from nexus.services.product_agent_registry.export_contract import ROOT, artifacts
from nexus.services.product_agent_registry.signing import key_id, verify_version
from tests.unit.services.test_product_agent_registry import command
from tests.unit.services.test_product_agent_registry import registry as registry


def test_artifacts_reproduce_exactly_and_fixture_signature_verifies():
    for name, content in artifacts().items():
        assert (ROOT / name).read_text() == content
    fixture = json.loads((ROOT / "signed-version.fixture.json").read_text())
    public = serialization.load_pem_public_key(fixture["public_key_pem"].encode())
    verify_version(SignedVersion.model_validate(fixture["version"]), {key_id(public): public})


def test_owner_command_schema_and_adapter_agree():
    schema = json.loads((ROOT / "schema.json").read_text())
    validator = Draft202012Validator(schema)
    body = command("RegisterPrincipal", display_name="Assistant").model_dump()
    assert validator.is_valid(body)
    for invalid in [
        {**body, "is_admin": True},
        {**body, "api_version": "agent.sudo.dev/v1"},
        {**body, "principal": {"trust_domain": "localhost", "agent_name": "x"}},
    ]:
        assert not validator.is_valid(invalid)
        with pytest.raises(ValueError):
            parse_command(json.dumps(invalid).encode())
    # ResourceRef is a reference to the existing definition, never a copied schema.
    assert schema["$defs"]["PublishVersion"]["properties"]["spec_ref"] == {
        "$ref": load_resource_ref_schema()["$id"]
    }


def test_openapi_generation_is_available():
    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()
    assert "/api/v2/product-agents/commands" in schema["paths"]


def test_service_configuration_is_explicit_and_retains_key_security(
    registry, tmp_path, monkeypatch
):
    app = FastAPI()
    monkeypatch.delenv("NEXUS_AGENT_REGISTRY_SIGNING_KEY_FILE", raising=False)
    configure(app, None)
    assert not hasattr(app.state, "product_agent_registry")
    key_file = tmp_path / "signing.pem"
    key_file.write_bytes(
        registry.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    monkeypatch.setenv("NEXUS_AGENT_REGISTRY_SIGNING_KEY_FILE", str(key_file))
    monkeypatch.setenv("NEXUS_AGENT_REGISTRY_DOMAIN_ZONES", json.dumps(registry.domain_zones))
    monkeypatch.delenv("NEXUS_AGENT_REGISTRY_TRUST_FILE", raising=False)
    record_store = SimpleNamespace(engine=registry.engine)
    with pytest.raises(RuntimeError, match="authenticated server mode"):
        configure(app, record_store)
    app.state.api_key = "test-operator-key"
    configure(app, record_store)
    assert app.state.product_agent_registry.key_id == registry.key_id
    key_file.chmod(0o644)
    with pytest.raises(RuntimeError, match="only by its owner"):
        configure(app, record_store)
