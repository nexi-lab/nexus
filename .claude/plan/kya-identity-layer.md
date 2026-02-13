# KYA Identity Layer — Implementation Plan

**Issue**: #1355 (feat: KYA — Agent Identity Layer)
**Parent Epic**: #1354 (Agent Exchange Kernel)
**Date**: 2026-02-11

## Decisions Summary (All Approved)

| # | Decision | Choice |
|---|----------|--------|
| 1 | DID Method | `did:key:` primary + `did:web:` opt-in for public agents |
| 2 | Key Storage | Separate `AgentKeyModel` table, Fernet-encrypted private keys |
| 3 | Credential Format | JWT-VC only (JWS + Ed25519) |
| 4 | Module Boundaries | Hybrid `src/nexus/identity/` package + hooks in existing code |
| 5 | JSON DRY | Helper functions for metadata serialization |
| 6 | Dual-Write | Transactional wrapper with compensating action |
| 7 | Crypto Organization | `identity/crypto.py` cohesive module, reuse OAuthCrypto |
| 8 | Edge Cases | Idempotent `ensure_keypair()`, `/.identity/` for public metadata only |
| 9 | Crypto Tests | Hypothesis property-based + unit tests |
| 10 | Performance | pytest-benchmark in `tests/benchmark/` |
| 11 | Security Tests | Explicit negative test suite |
| 12 | Integration | Full golden-path registration-to-verification test |
| 13 | Registration Latency | Synchronous key generation (~5-15ms delta) |
| 14 | Signature Verification | TTL cache for public keys (60s, ~0.7ms per request) |
| 15 | Passport Verification | Cached revocation list (<1ms check) |
| 16 | Key Rotation | RFC 9421 `keyid` parameter + fallback chain |

## New Dependencies

```toml
# pyproject.toml additions
"PyJWT>=2.8.0",          # JWT encoding/decoding with Ed25519 support
"multiformats>=0.3.0",   # did:key multicodec + multibase encoding
"hypothesis>=6.0.0",     # Property-based testing (dev dependency)
```

Note: `cryptography>=41.0.0` already a dependency (used for Fernet). Ed25519 is built-in.

## File Structure

```
src/nexus/identity/
├── __init__.py              # Public API: generate_keypair, create_did, create_passport, etc.
├── crypto.py                # Ed25519 keypair gen, JWS signing/verification, Fernet key encryption
├── did.py                   # DID generation: did:key (default) + did:web (opt-in)
├── credentials.py           # JWT-VC issuance and verification
├── passport.py              # Digital Agent Passport (DAP) bundle
├── signing.py               # RFC 9421 HTTP message signing (outbound)
├── verification.py          # RFC 9421 signature verification middleware (inbound)
├── models.py                # AgentKeyModel, AgentCredentialModel (SQLAlchemy)
└── key_cache.py             # TTL cache for public keys + revocation set

tests/unit/identity/
├── test_crypto.py           # Ed25519 gen, sign, verify + Hypothesis property tests
├── test_did.py              # DID encoding/decoding + Hypothesis roundtrip tests
├── test_credentials.py      # JWT-VC issuance, verification, expiry
├── test_passport.py         # DAP creation, validation, tamper detection
├── test_signing.py          # RFC 9421 outbound signing
├── test_verification.py     # RFC 9421 inbound verification
├── test_identity_security.py # Negative tests: expired keys, impersonation, replay, etc.
├── test_key_cache.py        # Cache behavior, invalidation, rotation

tests/integration/identity/
├── test_registration_flow.py # Golden path: register → keys → DID → passport → verify

tests/benchmark/identity/
├── bench_crypto.py          # Key gen <10ms, sign <5ms, verify <10ms
├── bench_passport.py        # Passport gen <50ms, verify <75ms
```

## Phase 1: Agent Identity Registry (Foundation)

### Step 1.1: Create `identity/models.py` — AgentKeyModel

```python
class AgentKeyModel(Base):
    """Ed25519 signing keys for agent identity."""
    __tablename__ = "agent_keys"

    key_id: str               # UUID, referenced in RFC 9421 keyid parameter
    agent_id: str             # FK to agent_records.agent_id
    algorithm: str            # "Ed25519" (extensible for future algorithms)
    public_key_bytes: bytes   # Raw 32-byte Ed25519 public key
    encrypted_private_key: str # Fernet-encrypted private key (base64)
    did: str                  # did:key:z6Mk... derived from public key
    is_active: bool           # True = can sign/verify. False = revoked
    expires_at: datetime|None # Null = no expiry. Set during key rotation
    created_at: datetime
    revoked_at: datetime|None

    # Indexes
    __table_args__ = (
        Index("idx_agent_keys_agent_active", "agent_id", "is_active"),
        Index("idx_agent_keys_did", "did", unique=True),
    )
```

Files to modify:
- Create: `src/nexus/identity/models.py`
- Modify: `src/nexus/storage/models.py` (import + register in Base.metadata)
- Create: Alembic migration for `agent_keys` table

### Step 1.2: Create `identity/crypto.py` — Core Crypto Operations

```python
class IdentityCrypto:
    """Ed25519 + Fernet crypto for agent identity."""

    def __init__(self, fernet_key: bytes | None = None, oauth_crypto: OAuthCrypto | None = None):
        """Reuse existing OAuthCrypto Fernet instance for key encryption."""

    def generate_keypair(self) -> tuple[Ed25519PrivateKey, Ed25519PublicKey]: ...
    def encrypt_private_key(self, private_key: Ed25519PrivateKey) -> str: ...
    def decrypt_private_key(self, encrypted: str) -> Ed25519PrivateKey: ...
    def sign(self, message: bytes, private_key: Ed25519PrivateKey) -> bytes: ...
    def verify(self, message: bytes, signature: bytes, public_key: Ed25519PublicKey) -> bool: ...
    def public_key_bytes(self, public_key: Ed25519PublicKey) -> bytes: ...
```

Files to create: `src/nexus/identity/crypto.py`
Dependencies: `cryptography.hazmat.primitives.asymmetric.ed25519`

### Step 1.3: Create `identity/did.py` — DID Generation

```python
def create_did_key(public_key: Ed25519PublicKey) -> str:
    """Create did:key:z6Mk... from Ed25519 public key.

    Encoding: multicodec(0xed, raw_public_key) → multibase(base58btc)
    """

def resolve_did_key(did: str) -> Ed25519PublicKey:
    """Resolve did:key: to Ed25519 public key."""

def create_did_web(domain: str, agent_id: str) -> str:
    """Create did:web:domain:agents:agent_id for public-facing agents."""

def create_did_document(did: str, public_key: Ed25519PublicKey, service_endpoints: dict) -> dict:
    """Create W3C DID Document (JSON)."""
```

Files to create: `src/nexus/identity/did.py`
Dependencies: `multiformats` (multicodec + multibase)

### Step 1.4: Create `identity/key_service.py` — Idempotent Key Management

```python
class KeyService:
    """Manages agent signing keys with idempotent provisioning."""

    def __init__(self, session_factory, crypto: IdentityCrypto, cache_ttl: int = 60): ...

    def ensure_keypair(self, agent_id: str) -> AgentKeyRecord:
        """Idempotent: generate keypair if not exists, return existing if already provisioned."""

    def get_active_keys(self, agent_id: str) -> list[AgentKeyRecord]:
        """Get all active keys for an agent (newest first). Uses TTL cache."""

    def get_public_key(self, key_id: str) -> Ed25519PublicKey | None:
        """Lookup by key_id (for RFC 9421 keyid parameter). Cached."""

    def rotate_key(self, agent_id: str, grace_period_hours: int = 24) -> AgentKeyRecord:
        """Generate new key, mark old key with expires_at = now + grace_period."""

    def revoke_key(self, key_id: str) -> None:
        """Immediately revoke a key. Invalidate cache."""

    def is_revoked(self, key_id: str) -> bool:
        """Check revocation status. Uses cached revocation set."""
```

Files to create: `src/nexus/identity/key_service.py`

### Step 1.5: Integrate with Agent Registration

Modify `nexus_fs.py:register_agent()` to:
1. After AgentRegistry dual-write, call `key_service.ensure_keypair(agent_id)`
2. Store DID in AgentRecord metadata (or add `did` column — defer to step 1.6)
3. Write public identity metadata to `/.identity/did.json` in agent namespace

Modify `agent_registry.py:register()` to:
1. Accept optional `did` parameter
2. Include DID in returned AgentRecord

Add `did` field to `AgentRecord` frozen dataclass and `AgentRecordModel`.

### Step 1.6: Verification Endpoint

Create `POST /agents/{id}/verify` endpoint:
- Input: agent_id
- Output: `{ verified: bool, did: str, public_key: str, active_keys: int }`
- Checks: agent exists, has active keys, keys are not expired/revoked

Files to modify: `src/nexus/server/fastapi_server.py` (add endpoint)

### Step 1.7: Tests for Phase 1

- `test_crypto.py`: Hypothesis roundtrip (generate → encrypt → decrypt → sign → verify)
- `test_did.py`: Hypothesis roundtrip (public_key → did:key → resolve → same public_key)
- `test_key_service.py`: ensure_keypair idempotency, rotation, revocation
- `test_identity_security.py`: expired key rejection, revoked key rejection
- `bench_crypto.py`: key gen <10ms, sign <5ms, verify <10ms

## Phase 2: Web Bot Auth (RFC 9421)

### Step 2.1: Create `identity/signing.py` — Outbound HTTP Signing

```python
class HTTPMessageSigner:
    """RFC 9421 HTTP message signing for outbound agent requests."""

    def __init__(self, key_service: KeyService, crypto: IdentityCrypto): ...

    def sign_request(
        self,
        method: str,
        url: str,
        headers: dict,
        body: bytes | None,
        agent_id: str,
        key_id: str | None = None,  # Uses newest active key if None
    ) -> dict:
        """Returns headers dict with Signature and Signature-Input added.

        Signature base components (per RFC 9421):
        - @method, @target-uri, @authority
        - content-type, content-digest (if body present)
        - created timestamp, keyid, algorithm (ed25519)
        """
```

Files to create: `src/nexus/identity/signing.py`
Reference: RFC 9421 §2 (Creating a Signature), Cloudflare Web Bot Auth spec

### Step 2.2: Create `identity/verification.py` — Inbound Verification Middleware

```python
class SignatureVerificationMiddleware:
    """FastAPI middleware for RFC 9421 signature verification on inbound agent requests."""

    def __init__(self, key_service: KeyService, crypto: IdentityCrypto):
        """Key lookups via KeyService TTL cache (Decision #14B)."""

    async def __call__(self, request: Request, call_next):
        """Verify signature if Signature header present.

        1. Parse Signature-Input header
        2. Extract keyid → look up public key (cache hit ~0.001ms)
        3. If no keyid → try active keys newest-first (Decision #16C)
        4. Reconstruct signature base
        5. Ed25519 verify (~0.5ms)
        6. Reject if: expired key, revoked key, invalid signature
        7. Add verified agent_id to request.state
        """
```

Files to create: `src/nexus/identity/verification.py`
Files to modify: `src/nexus/server/fastapi_server.py` (add middleware)

### Step 2.3: Key Rotation Endpoint

Create `POST /agents/{id}/keys/rotate`:
- Input: `{ grace_period_hours: int }` (default: 24)
- Output: `{ new_key_id: str, old_key_id: str, old_expires_at: datetime }`
- Auth: agent owner only

Files to modify: `src/nexus/server/fastapi_server.py`

### Step 2.4: Tests for Phase 2

- `test_signing.py`: Sign request, verify components match RFC 9421 format
- `test_verification.py`: Valid signature passes, invalid fails, expired key rejects
- `test_identity_security.py` additions: replay attack (reused timestamp), cross-agent impersonation
- `bench_crypto.py` additions: sign+verify roundtrip, middleware overhead

## Phase 3: Digital Agent Passport (DAP)

### Step 3.1: Create `identity/credentials.py` — JWT-VC Issuance

```python
class CredentialIssuer:
    """JWT-VC issuance for agent capabilities and provenance."""

    def __init__(self, key_service: KeyService, crypto: IdentityCrypto): ...

    def issue_credential(
        self,
        subject_did: str,
        issuer_did: str,
        credential_type: str,  # "AgentCapability", "AgentProvenance", "AgentTrust"
        claims: dict,
        valid_for: timedelta = timedelta(hours=1),
        signing_key_id: str | None = None,
    ) -> str:
        """Issue a JWT-VC. Returns compact JWS string.

        JWT Header: { alg: "EdDSA", typ: "JWT", kid: key_id }
        JWT Payload: {
            iss: issuer_did,
            sub: subject_did,
            iat: now,
            exp: now + valid_for,
            vc: {
                "@context": ["https://www.w3.org/2018/credentials/v1"],
                type: ["VerifiableCredential", credential_type],
                credentialSubject: claims
            }
        }
        """

    def verify_credential(self, jwt_token: str) -> VerifiedCredential:
        """Verify JWT-VC. Returns parsed claims or raises InvalidCredentialError.

        Checks: signature, expiry, issuer DID resolution, revocation status.
        """
```

Files to create: `src/nexus/identity/credentials.py`
Dependencies: `PyJWT` with `cryptography` backend for EdDSA

### Step 3.2: Create `identity/passport.py` — DAP Bundle

```python
@dataclass(frozen=True)
class DigitalAgentPassport:
    """Tamper-proof credential bundle for agent-to-agent handshake."""
    agent_did: str
    owner_did: str | None
    provenance_vc: str       # JWT-VC: who created this agent, when, what version
    permissions_vc: str      # JWT-VC: ReBAC permission snapshot hash
    telemetry_hash: str      # SHA-256 of recent telemetry (integrity proof)
    capabilities: list[str]  # Declared capabilities
    issued_at: datetime
    expires_at: datetime
    signature: str           # JWS over the entire bundle

class PassportService:
    """Creates and verifies Digital Agent Passports."""

    def __init__(self, key_service, credential_issuer, rebac_service): ...

    def create_passport(self, agent_id: str, valid_for: timedelta = timedelta(hours=1)) -> DigitalAgentPassport:
        """Create a DAP by:
        1. Fetch agent record + DID
        2. Issue provenance VC (agent metadata, creation date, platform)
        3. Issue permissions VC (hash of current ReBAC tuples)
        4. Hash recent telemetry
        5. Sign entire bundle with agent's key
        """

    def verify_passport(self, passport_jwt: str) -> VerifiedPassport:
        """Verify DAP:
        1. Verify outer JWS signature
        2. Verify each embedded VC
        3. Check expiry
        4. Check revocation status of signing key
        """

    def exchange_passports(self, local_agent_id: str, remote_passport_jwt: str) -> HandshakeResult:
        """Agent-to-agent passport exchange during handshake."""
```

Files to create: `src/nexus/identity/passport.py`

### Step 3.3: Integrate with A2A Agent Card

Modify `a2a/agent_card.py:build_agent_card()`:
- Include agent DID in the card
- Include JWS signature over the card (signed with agent's key)
- Add `identity` field with DID, public key, and passport endpoint

### Step 3.4: Tests for Phase 3

- `test_credentials.py`: Issue VC, verify VC, expired VC rejection, tampered VC detection
- `test_passport.py`: Create passport, verify passport, handshake exchange
- `test_identity_security.py` additions: tampered passport, mismatched DID in VC
- `bench_passport.py`: passport gen <50ms, verify <75ms
- `test_registration_flow.py`: Full golden-path integration

## Phase 4: ERC-8004 Bridge (Optional)

### Step 4.1: ERC-8004 Reader

```python
class ERC8004Bridge:
    """Read-only bridge to ERC-8004 on-chain agent identity registry."""

    def __init__(self, rpc_url: str, identity_registry_address: str): ...

    def resolve_agent(self, on_chain_agent_id: str) -> OnChainAgentRecord | None:
        """Read agent identity from ERC-8004 Identity Registry (ERC-721)."""

    def get_reputation(self, on_chain_agent_id: str) -> ReputationScore:
        """Read reputation score from ERC-8004 Reputation Registry."""

    def map_to_nexus(self, on_chain_record: OnChainAgentRecord) -> dict:
        """Map on-chain identity fields to Nexus AgentRecord metadata."""
```

Files to create: `src/nexus/identity/erc8004.py`
Dependencies: `web3` (optional dependency, only for ERC-8004 bridge)
Note: This phase is marked optional in the issue. Defer if not needed immediately.

## Implementation Order

```
Phase 1 (4-5 sessions):
  1.1 AgentKeyModel + migration
  1.2 identity/crypto.py + tests
  1.3 identity/did.py + tests
  1.4 identity/key_service.py + tests
  1.5 Integration with register_agent + transactional wrapper
  1.6 POST /agents/{id}/verify endpoint
  1.7 Benchmark tests

Phase 2 (3-4 sessions):
  2.1 identity/signing.py + tests
  2.2 identity/verification.py middleware + tests
  2.3 Key rotation endpoint
  2.4 Security tests (replay, impersonation)

Phase 3 (3-4 sessions):
  3.1 identity/credentials.py + tests
  3.2 identity/passport.py + tests
  3.3 A2A Agent Card integration
  3.4 Golden-path integration test + benchmarks

Phase 4 (1-2 sessions, optional):
  4.1 ERC-8004 bridge (read-only)
```

## Shared Utilities (Created in Phase 1, Used Throughout)

### JSON Metadata Helpers (Decision #5B)

```python
# src/nexus/identity/utils.py (or src/nexus/storage/json_utils.py)
def parse_metadata(raw: str | None) -> MappingProxyType[str, Any]:
    """Safely parse JSON metadata string to immutable dict."""

def serialize_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Serialize metadata dict to JSON string."""
```

### Transactional Registration Wrapper (Decision #6C)

```python
# Added to nexus_fs.py:register_agent or extracted to identity/provisioning.py
def _register_agent_with_identity(self, ...):
    """Coordinated registration: entity + agent registry + keypair.
    All succeed or all fail. Compensating rollback on partial failure."""
```

## Performance Targets (Verified by Benchmarks)

| Operation | Target | Expected |
|-----------|--------|----------|
| Ed25519 key generation | <10ms | ~0.1ms |
| DID encoding | <1ms | ~0.01ms |
| JWS signing | <5ms | ~0.5ms |
| JWS verification | <10ms | ~0.5ms |
| HTTP signature creation | <10ms | ~1ms |
| HTTP signature verification | <10ms | ~0.7ms (cached) |
| Passport creation | <50ms | ~5ms |
| Passport verification | <75ms | ~2ms |

## References

- [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004) — On-chain agent registry
- [RFC 9421](https://www.rfc-editor.org/rfc/rfc9421.html) — HTTP Message Signatures
- [Cloudflare Web Bot Auth](https://blog.cloudflare.com/web-bot-auth)
- [Visa TAP](https://developer.visa.com/capabilities/trusted-agent-protocol/overview)
- [Project NANDA KYA 1.0](https://www.media.mit.edu/projects/mit-nanda/overview/)
- [W3C DID Core](https://www.w3.org/TR/did-core/)
- [W3C VC Data Model](https://www.w3.org/TR/vc-data-model/)
- [OWASP Agentic Top 10](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
- [Zero-Trust Identity Framework for Agentic AI](https://arxiv.org/html/2505.19301v1)
