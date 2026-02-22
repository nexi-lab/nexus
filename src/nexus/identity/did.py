"""DID (Decentralized Identifier) generation and resolution (Issue #1355, Decision #1D).

Supports two DID methods:
- did:key: (primary) — Self-certifying, no resolver needed. Ed25519 public key IS the ID.
  Encoding: multicodec(0xed01, raw_32_bytes) → multibase(base58btc, 'z' prefix)
  Example: did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK

- did:web: (opt-in) — DNS-based, for public-facing/federated agents.
  Example: did:web:nexus.sudorouter.ai:agents:agent123

Multicodec prefix for Ed25519 public key: 0xed (two bytes: 0xed, 0x01)
Multibase prefix for base58btc: 'z'

No external dependencies — uses base58 encoding implemented inline
(avoiding `multiformats` dependency for a 20-line encoding).

References:
    - W3C DID Core: https://www.w3.org/TR/did-core/
    - did:key Method: https://w3c-ccg.github.io/did-method-key/
    - Multicodec: https://github.com/multiformats/multicodec
"""

from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from nexus.identity.crypto import IdentityCrypto

# Multicodec prefix for Ed25519 public key (varint-encoded 0xed)
_ED25519_MULTICODEC_PREFIX = bytes([0xED, 0x01])

# Base58btc alphabet (Bitcoin alphabet)
_BASE58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_encode(data: bytes) -> str:
    """Encode bytes to base58btc string (Bitcoin alphabet).

    Args:
        data: Bytes to encode.

    Returns:
        Base58btc encoded string.
    """
    # Count leading zero bytes
    num_leading_zeros = 0
    for byte in data:
        if byte == 0:
            num_leading_zeros += 1
        else:
            break

    # Convert bytes to integer
    n = int.from_bytes(data, byteorder="big")

    # Convert integer to base58
    result = bytearray()
    while n > 0:
        n, remainder = divmod(n, 58)
        result.append(_BASE58_ALPHABET[remainder])

    # Add leading '1's for leading zero bytes
    for _ in range(num_leading_zeros):
        result.append(_BASE58_ALPHABET[0])

    result.reverse()
    return result.decode("ascii")


def _base58_decode(encoded: str) -> bytes:
    """Decode a base58btc string to bytes.

    Args:
        encoded: Base58btc encoded string.

    Returns:
        Decoded bytes.

    Raises:
        ValueError: If the string contains invalid characters.
    """
    # Count leading '1' characters (represent zero bytes)
    num_leading_ones = 0
    for char in encoded:
        if char == "1":
            num_leading_ones += 1
        else:
            break

    # Convert base58 to integer
    n = 0
    for char in encoded:
        digit = _BASE58_ALPHABET.find(char.encode("ascii"))
        if digit == -1:
            raise ValueError(f"Invalid base58 character: {char!r}")
        n = n * 58 + digit

    # Convert integer to bytes
    result = b"" if n == 0 else n.to_bytes((n.bit_length() + 7) // 8, byteorder="big")

    # Add leading zero bytes
    return b"\x00" * num_leading_ones + result


def create_did_key(public_key: Ed25519PublicKey) -> str:
    """Create a did:key identifier from an Ed25519 public key.

    Encoding: multicodec(0xed01) + raw_32_bytes → base58btc with 'z' prefix.

    Args:
        public_key: Ed25519 public key.

    Returns:
        DID string, e.g. "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
    """
    raw_bytes = IdentityCrypto.public_key_to_bytes(public_key)
    multicodec_bytes = _ED25519_MULTICODEC_PREFIX + raw_bytes
    encoded = _base58_encode(multicodec_bytes)
    return f"did:key:z{encoded}"


def resolve_did_key(did: str) -> Ed25519PublicKey:
    """Resolve a did:key identifier to an Ed25519 public key.

    Args:
        did: DID string starting with "did:key:z".

    Returns:
        Ed25519PublicKey instance.

    Raises:
        ValueError: If the DID is malformed or not an Ed25519 key.
    """
    if not did.startswith("did:key:z"):
        raise ValueError(f"Invalid did:key format: must start with 'did:key:z', got {did!r}")

    # Strip "did:key:z" prefix and decode base58btc
    encoded = did[len("did:key:z") :]
    if not encoded:
        raise ValueError("Empty did:key identifier")

    decoded = _base58_decode(encoded)

    # Verify multicodec prefix
    if len(decoded) < 2:
        raise ValueError(f"Decoded did:key too short: {len(decoded)} bytes")

    if decoded[:2] != _ED25519_MULTICODEC_PREFIX:
        raise ValueError(
            f"Unexpected multicodec prefix: {decoded[:2].hex()}, "
            f"expected {_ED25519_MULTICODEC_PREFIX.hex()} (Ed25519)"
        )

    # Extract raw public key bytes
    raw_key_bytes = decoded[2:]
    if len(raw_key_bytes) != 32:
        raise ValueError(f"Ed25519 public key must be 32 bytes, got {len(raw_key_bytes)}")

    return IdentityCrypto.public_key_from_bytes(raw_key_bytes)


def create_did_web(domain: str, agent_id: str) -> str:
    """Create a did:web identifier for a public-facing agent.

    Example: did:web:nexus.sudorouter.ai:agents:agent123

    Args:
        domain: Domain name (e.g., "nexus.sudorouter.ai").
        agent_id: Agent identifier.

    Returns:
        DID string.

    Raises:
        ValueError: If domain or agent_id is empty.
    """
    if not domain:
        raise ValueError("domain is required for did:web")
    if not agent_id:
        raise ValueError("agent_id is required for did:web")

    # URL-encode colons in agent_id (replace , with -)
    safe_agent_id = agent_id.replace(",", "-").replace("/", "-")
    return f"did:web:{domain}:agents:{safe_agent_id}"


def create_did_document(
    did: str,
    public_key: Ed25519PublicKey,
    service_endpoints: dict[str, str] | None = None,
    controller: str | None = None,
) -> dict[str, Any]:
    """Create a W3C DID Document (JSON-serializable dict).

    Args:
        did: The DID this document describes.
        public_key: Ed25519 public key for verification.
        service_endpoints: Optional map of service type to URL.
        controller: Optional controller DID (e.g., owner's DID). Defaults to self.

    Returns:
        DID Document as a dict, ready for JSON serialization.
    """
    raw_bytes = IdentityCrypto.public_key_to_bytes(public_key)

    # Verification method ID: DID + fragment
    verification_method_id = f"{did}#key-1"

    document: dict[str, Any] = {
        "@context": [
            "https://www.w3.org/ns/did/v1",
            "https://w3id.org/security/suites/ed25519-2020/v1",
        ],
        "id": did,
        "controller": controller or did,
        "verificationMethod": [
            {
                "id": verification_method_id,
                "type": "Ed25519VerificationKey2020",
                "controller": controller or did,
                "publicKeyMultibase": f"z{_base58_encode(raw_bytes)}",
            }
        ],
        "authentication": [verification_method_id],
        "assertionMethod": [verification_method_id],
    }

    if service_endpoints:
        document["service"] = [
            {
                "id": f"{did}#service-{i}",
                "type": service_type,
                "serviceEndpoint": url,
            }
            for i, (service_type, url) in enumerate(service_endpoints.items())
        ]

    return document
