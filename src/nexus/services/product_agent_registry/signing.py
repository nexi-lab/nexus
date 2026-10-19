"""Verify registration metadata against an operator-configured key ring."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from nexus.contracts.product_agent_registry import SignedVersion, VersionManifest, canonical_bytes


def key_id(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def verify_version(signed: SignedVersion, trusted_keys: Mapping[str, Ed25519PublicKey]) -> None:
    """This attests registration metadata, never the referenced artifact or provider identity."""
    try:
        key = trusted_keys[signed.key_id]
        if key_id(key) != signed.key_id:
            raise ValueError("key identity mismatch")
        payload = base64.b64decode(signed.payload_base64, validate=True)
        if len(payload) > 65536 or signed.digest != "sha256:" + hashlib.sha256(payload).hexdigest():
            raise ValueError("payload digest mismatch")
        key.verify(base64.b64decode(signed.signature_base64, validate=True), payload)
        manifest = VersionManifest.model_validate_json(payload)
        # Require the exact canonical owner bytes and a matching redundant wire projection.
        if canonical_bytes(manifest.model_dump()) != payload or manifest != signed.manifest:
            raise ValueError("manifest mismatch")
        if json.loads(payload)["evidence"] != "signed_registration_only":
            raise ValueError("unsupported evidence")
    except (ValueError, KeyError, InvalidSignature, TypeError) as error:
        raise ValueError("REGISTRY_SIGNATURE_INVALID") from error
