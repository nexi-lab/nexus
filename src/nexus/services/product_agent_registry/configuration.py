"""Explicit service startup; neither GET nor normal startup creates/migrates tables."""

import json
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key

from nexus.services.product_agent_registry.signing import key_id
from nexus.services.product_agent_registry.store import ProductAgentRegistry


def configure(app, record_store) -> None:
    key_path = os.environ.get("NEXUS_AGENT_REGISTRY_SIGNING_KEY_FILE")
    if not key_path:
        return
    if not record_store:
        raise RuntimeError("Registry requires the primary RecordStore")
    if not getattr(app.state, "api_key", None) and not getattr(app.state, "auth_provider", None):
        raise RuntimeError("Registry requires authenticated server mode")
    domain_zones = json.loads(os.environ["NEXUS_AGENT_REGISTRY_DOMAIN_ZONES"])
    path = Path(key_path)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("Registry signing key must be readable only by its owner")
    key = load_pem_private_key(path.read_bytes(), password=None)
    trusted = {}
    trust_file = os.environ.get("NEXUS_AGENT_REGISTRY_TRUST_FILE")
    if trust_file:
        keys = json.loads(Path(trust_file).read_text())
        if not isinstance(keys, list) or len(keys) > 32:
            raise ValueError("REGISTRY_TRUST_CONFIGURATION_INVALID")
        for pem in keys:
            public = load_pem_public_key(pem.encode())
            if not isinstance(public, Ed25519PublicKey):
                raise ValueError("REGISTRY_TRUST_CONFIGURATION_INVALID")
            trusted[key_id(public)] = public
    app.state.product_agent_registry = ProductAgentRegistry(
        record_store.engine,
        key,
        domain_zones,
        trusted_keys=trusted,
        inventory_zone_domains=json.loads(
            os.environ.get("NEXUS_AGENT_INVENTORY_ZONE_DOMAINS", "{}")
        ),
        inventory_attestors=set(
            json.loads(os.environ.get("NEXUS_AGENT_INVENTORY_ATTESTORS", "[]"))
        ),
        inventory_enforced_zones=set(
            json.loads(os.environ.get("NEXUS_AGENT_INVENTORY_ENFORCED_ZONES", "[]"))
        ),
    )


def configure_execution(app, record_store, entity_registry, rebac_manager) -> None:
    """Optional binding profile, composed from the existing IAM brick after startup."""
    policy_file = os.environ.get("NEXUS_AGENT_EXECUTION_POLICY_FILE")
    if not policy_file:
        return
    from nexus.bricks.delegation.service import DelegationService
    from nexus.services.product_agent_registry.execution import AgentExecution

    registry = getattr(app.state, "product_agent_registry", None)
    if registry is None or rebac_manager is None or entity_registry is None or record_store is None:
        raise RuntimeError("Agent execution requires Registry and real IAM delegation services")
    # An IAM task worker is not a runtime process. Use the same IAM owner and
    # primary stores without registering phantom processes in the runtime table.
    delegation = DelegationService(record_store, rebac_manager, entity_registry)
    app.state.product_agent_execution = AgentExecution(
        registry,
        record_store,
        delegation,
        entity_registry,
        json.loads(Path(policy_file).read_text()),
    )
