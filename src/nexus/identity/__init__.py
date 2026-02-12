"""KYA (Know Your Agent) Identity Layer — Issue #1355.

Provides cryptographic agent identity, verifiable credentials,
HTTP message signing, and Digital Agent Passports.

Architecture:
    - crypto.py: Ed25519 keypair generation, JWS signing, Fernet key encryption
    - did.py: DID generation (did:key primary, did:web opt-in)
    - credentials.py: JWT-VC issuance and verification
    - passport.py: Digital Agent Passport (DAP) bundle
    - signing.py: RFC 9421 HTTP message signing (outbound)
    - verification.py: RFC 9421 signature verification (inbound middleware)
    - models.py: AgentKeyModel (SQLAlchemy)
    - key_service.py: Idempotent key management with TTL cache
    - utils.py: Shared helpers (JSON metadata serialization)

References:
    - ERC-8004: On-chain agent registry
    - RFC 9421: HTTP Message Signatures
    - W3C DID Core: Decentralized Identifiers
    - W3C VC Data Model: Verifiable Credentials
"""

from nexus.identity.crypto import IdentityCrypto
from nexus.identity.did import create_did_key, resolve_did_key
from nexus.identity.key_service import AgentKeyRecord, KeyService
from nexus.identity.utils import parse_metadata, serialize_metadata

__all__ = [
    "AgentKeyRecord",
    "IdentityCrypto",
    "KeyService",
    "create_did_key",
    "resolve_did_key",
    "parse_metadata",
    "serialize_metadata",
]
