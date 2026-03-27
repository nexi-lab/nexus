"""JWT-VC credential issuance and verification (Issue #1753).

Provides:
    - ``CapabilityIssuer``: Issues JWT-VC capability credentials signed with EdDSA.
    - ``CapabilityVerifier``: Verifies JWT-VC tokens, checks expiry and revocation.
    - ``DelegationChain``: Delegates attenuated capabilities with depth enforcement.

Format follows the W3C VC-JOSE-COSE profile: a standard JWT with a ``vc`` claim
containing the W3C Verifiable Credential structure.  Signing uses EdDSA (Ed25519)
via PyJWT + cryptography.

References:
    - W3C VC Data Model 2.0: https://www.w3.org/TR/vc-data-model-2.0/
    - W3C VC-JOSE-COSE: https://www.w3.org/TR/vc-jose-cose/
    - UCAN Specification: https://ucan.xyz/specification/
"""

import hashlib
import json
import logging
import time
from collections.abc import Sequence
from typing import Any

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from nexus.contracts.credential_types import (
    DEFAULT_CREDENTIAL_TTL,
    MAX_BACKEND_FEATURES_PER_CREDENTIAL,
    MAX_CREDENTIAL_TTL,
    MAX_DELEGATION_DEPTH,
    MIN_CREDENTIAL_TTL,
    VC_CONTEXT,
    VC_TYPES,
    Ability,
    Capability,
    CredentialClaims,
)

logger = logging.getLogger(__name__)

# JWT algorithm for Ed25519
_JWT_ALGORITHM = "EdDSA"


def _deterministic_credential_id(subject_did: str, timestamp: float) -> str:
    """Generate a deterministic credential ID as a URN UUID.

    Args:
        subject_did: Subject DID for uniqueness.
        timestamp: Unix timestamp for uniqueness.

    Returns:
        URN UUID string (e.g. ``urn:uuid:abc123...``).
    """
    data = f"{subject_did}:{timestamp}".encode()
    hex_digest = hashlib.sha256(data).hexdigest()[:32]
    # Format as UUID v4-like: 8-4-4-4-12
    return (
        f"urn:uuid:{hex_digest[:8]}-{hex_digest[8:12]}-"
        f"{hex_digest[12:16]}-{hex_digest[16:20]}-{hex_digest[20:32]}"
    )


class CapabilityIssuer:
    """Issues JWT-VC capability credentials signed with EdDSA (Ed25519).

    The issuer holds a private key and produces signed JWT tokens containing
    W3C VC-compliant capability claims.

    Args:
        issuer_did: DID of the issuing entity (kernel or delegating agent).
        signing_key: Ed25519 private key for JWT signing.
        key_id: Key identifier (UUID) for the ``kid`` JWT header.
    """

    def __init__(
        self,
        issuer_did: str,
        signing_key: Ed25519PrivateKey,
        key_id: str,
    ) -> None:
        self._issuer_did = issuer_did
        self._signing_key = signing_key
        self._key_id = key_id

    @property
    def issuer_did(self) -> str:
        """The DID of this issuer."""
        return self._issuer_did

    @property
    def key_id(self) -> str:
        """The signing key ID."""
        return self._key_id

    def issue(
        self,
        subject_did: str,
        capabilities: Sequence[Capability],
        *,
        ttl_seconds: int = DEFAULT_CREDENTIAL_TTL,
        parent_credential_id: str | None = None,
        delegation_depth: int = 0,
    ) -> tuple[str, CredentialClaims]:
        """Issue a JWT-VC capability credential.

        Args:
            subject_did: DID of the agent receiving the credential.
            capabilities: Capabilities to grant.
            ttl_seconds: Time-to-live in seconds (clamped to MIN/MAX).
            parent_credential_id: Parent credential ID for delegations.
            delegation_depth: Depth in delegation chain (0 = root).

        Returns:
            Tuple of (jwt_token, parsed_claims).

        Raises:
            ValueError: If validation fails (too many capabilities, invalid TTL, etc.).
        """
        # Validate inputs
        if not subject_did:
            raise ValueError("subject_did must be non-empty")
        if not capabilities:
            raise ValueError("At least one capability is required")
        if len(capabilities) > MAX_BACKEND_FEATURES_PER_CREDENTIAL:
            raise ValueError(
                f"Too many capabilities: {len(capabilities)} "
                f"(max {MAX_BACKEND_FEATURES_PER_CREDENTIAL})"
            )
        if delegation_depth > MAX_DELEGATION_DEPTH:
            raise ValueError(
                f"Delegation depth {delegation_depth} exceeds max {MAX_DELEGATION_DEPTH}"
            )

        # Clamp TTL
        ttl_seconds = max(MIN_CREDENTIAL_TTL, min(ttl_seconds, MAX_CREDENTIAL_TTL))

        now = time.time()
        credential_id = _deterministic_credential_id(subject_did, now)

        # Build JWT claims with W3C VC structure
        vc_claim: dict[str, Any] = {
            "@context": list(VC_CONTEXT),
            "type": list(VC_TYPES),
            "credentialSubject": {
                "id": subject_did,
                "backend_features": [cap.to_dict() for cap in capabilities],
            },
        }
        if parent_credential_id is not None:
            vc_claim["parentCredential"] = parent_credential_id
            vc_claim["delegationDepth"] = delegation_depth

        payload: dict[str, Any] = {
            "iss": self._issuer_did,
            "sub": subject_did,
            "iat": int(now),
            "exp": int(now + ttl_seconds),
            "jti": credential_id,
            "vc": vc_claim,
        }

        headers: dict[str, str] = {
            "alg": _JWT_ALGORITHM,
            "typ": "JWT",
            "kid": self._key_id,
        }

        token: str = pyjwt.encode(
            payload,
            self._signing_key,
            algorithm=_JWT_ALGORITHM,
            headers=headers,
        )

        claims = CredentialClaims(
            issuer_did=self._issuer_did,
            subject_did=subject_did,
            credential_id=credential_id,
            capabilities=tuple(capabilities),
            issued_at=now,
            expires_at=now + ttl_seconds,
            parent_credential_id=parent_credential_id,
            delegation_depth=delegation_depth,
        )

        logger.info(
            "[VC] Issued credential %s for %s (%d capabilities, TTL=%ds, depth=%d)",
            credential_id,
            subject_did,
            len(capabilities),
            ttl_seconds,
            delegation_depth,
        )

        return token, claims


class CapabilityVerifier:
    """Verifies JWT-VC capability credentials.

    Maintains a set of trusted issuer public keys and an in-memory
    revocation cache.  The revocation cache is updated externally
    (by the CredentialService) via ``update_revocation_cache()``.

    Thread-safety: the revocation set is replaced atomically (Python GIL
    ensures ``set`` assignment is atomic).  Trusted issuers are typically
    set once at startup and not modified afterward.
    """

    def __init__(self) -> None:
        self._trusted_issuers: dict[str, Ed25519PublicKey] = {}
        self._revoked_ids: frozenset[str] = frozenset()

    def trust_issuer(self, did: str, public_key: Ed25519PublicKey) -> None:
        """Register a trusted issuer's public key.

        Args:
            did: Issuer DID.
            public_key: Ed25519 public key for verification.
        """
        self._trusted_issuers[did] = public_key

    def update_revocation_cache(self, revoked_ids: frozenset[str]) -> None:
        """Atomically replace the in-memory revocation cache.

        Args:
            revoked_ids: Frozenset of revoked credential IDs.
        """
        self._revoked_ids = revoked_ids

    def verify(self, token: str) -> CredentialClaims:
        """Verify a JWT-VC token and return parsed claims.

        Performs:
        1. Decode JWT header to find issuer
        2. Look up trusted issuer public key
        3. Verify JWT signature with EdDSA
        4. Check expiration
        5. Check revocation cache
        6. Parse capability claims

        Args:
            token: JWT-VC token string.

        Returns:
            Parsed CredentialClaims.

        Raises:
            ValueError: If verification fails for any reason.
        """
        # Step 1: Peek at unverified claims to find issuer
        try:
            unverified = pyjwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=[_JWT_ALGORITHM],
            )
        except pyjwt.exceptions.DecodeError as exc:
            raise ValueError(f"Invalid JWT format: {exc}") from exc

        issuer_did = unverified.get("iss")
        if not issuer_did:
            raise ValueError("JWT missing 'iss' claim")

        # Step 2: Find trusted issuer key
        public_key = self._trusted_issuers.get(issuer_did)
        if public_key is None:
            raise ValueError(f"Untrusted issuer: {issuer_did}")

        # Step 3: Verify signature + decode
        try:
            claims = pyjwt.decode(
                token,
                public_key,
                algorithms=[_JWT_ALGORITHM],
                options={
                    "verify_exp": True,
                    "verify_iat": True,
                    "require": ["iss", "sub", "iat", "exp", "jti", "vc"],
                },
            )
        except pyjwt.exceptions.ExpiredSignatureError as exc:
            raise ValueError("Credential expired") from exc
        except pyjwt.exceptions.InvalidTokenError as exc:
            raise ValueError(f"Invalid credential: {exc}") from exc

        # Step 4: Check future iat (clock skew)
        iat = claims.get("iat", 0)
        if iat > time.time() + 60:  # 60s leeway
            raise ValueError(f"Credential issued in the future: iat={iat}")

        # Step 5: Check revocation
        credential_id = claims.get("jti", "")
        if credential_id in self._revoked_ids:
            raise ValueError(f"Credential revoked: {credential_id}")

        # Step 6: Parse VC claims
        vc = claims.get("vc", {})
        subject = vc.get("credentialSubject", {})
        raw_capabilities = subject.get("backend_features", [])

        capabilities = tuple(Capability.from_dict(cap_dict) for cap_dict in raw_capabilities)

        return CredentialClaims(
            issuer_did=claims["iss"],
            subject_did=claims["sub"],
            credential_id=credential_id,
            capabilities=capabilities,
            issued_at=claims["iat"],
            expires_at=claims["exp"],
            parent_credential_id=vc.get("parentCredential"),
            delegation_depth=vc.get("delegationDepth", 0),
        )

    def check_capability(
        self,
        token: str,
        resource: str,
        ability: Ability,
    ) -> bool:
        """Check if a credential grants a specific capability.

        Convenience method combining verify + capability check.

        Args:
            token: JWT-VC token string.
            resource: Resource URI to check.
            ability: Required ability.

        Returns:
            True if the credential is valid and grants the capability.
        """
        try:
            claims = self.verify(token)
        except ValueError:
            return False

        for cap in claims.capabilities:
            if (cap.resource == resource or cap.resource == "*") and (
                Ability.ADMIN in cap.abilities or ability in cap.abilities
            ):
                return True
        return False


class DelegationChain:
    """Manages capability delegation with attenuation.

    Allows an agent to delegate a subset of its capabilities to another
    agent, creating a verifiable chain of authority.  The delegated
    capabilities MUST be a subset of the parent's capabilities, and
    the delegation depth MUST not exceed ``MAX_DELEGATION_DEPTH``.

    Args:
        issuer: CapabilityIssuer for signing delegated credentials.
        verifier: CapabilityVerifier for validating parent credentials.
    """

    def __init__(
        self,
        issuer: CapabilityIssuer,
        verifier: CapabilityVerifier,
    ) -> None:
        self._issuer = issuer
        self._verifier = verifier

    def delegate(
        self,
        parent_token: str,
        delegate_did: str,
        attenuated_capabilities: Sequence[Capability],
        *,
        ttl_seconds: int = 1800,
    ) -> tuple[str, CredentialClaims]:
        """Delegate a subset of capabilities to another agent.

        Args:
            parent_token: JWT-VC of the parent credential.
            delegate_did: DID of the agent receiving delegated capabilities.
            attenuated_capabilities: Capabilities to grant (must be subset of parent).
            ttl_seconds: TTL for the delegated credential (capped at parent TTL).

        Returns:
            Tuple of (jwt_token, parsed_claims) for the delegated credential.

        Raises:
            ValueError: If parent is invalid, capabilities aren't a subset,
                or depth would exceed the maximum.
        """
        # Verify parent credential
        parent_claims = self._verifier.verify(parent_token)

        # Check delegation depth
        new_depth = parent_claims.delegation_depth + 1
        if new_depth > MAX_DELEGATION_DEPTH:
            raise ValueError(
                f"Delegation depth {new_depth} would exceed max {MAX_DELEGATION_DEPTH}"
            )

        # Validate attenuation: each delegated cap must be subset of parent
        for new_cap in attenuated_capabilities:
            if not any(
                new_cap.is_subset_of(parent_cap) for parent_cap in parent_claims.capabilities
            ):
                raise ValueError(
                    f"Cannot delegate capability '{new_cap.resource}' with "
                    f"abilities {[a.value for a in new_cap.abilities]} — "
                    f"not present in parent credential"
                )

        # Cap TTL at parent's remaining time
        remaining_ttl = max(0, int(parent_claims.expires_at - time.time()))
        effective_ttl = min(ttl_seconds, remaining_ttl)
        if effective_ttl < MIN_CREDENTIAL_TTL:
            raise ValueError(
                f"Parent credential expires too soon for delegation "
                f"(remaining TTL: {remaining_ttl}s, min: {MIN_CREDENTIAL_TTL}s)"
            )

        return self._issuer.issue(
            subject_did=delegate_did,
            capabilities=attenuated_capabilities,
            ttl_seconds=effective_ttl,
            parent_credential_id=parent_claims.credential_id,
            delegation_depth=new_depth,
        )


def parse_capabilities_json(raw: str) -> tuple[Capability, ...]:
    """Parse a JSON string of capabilities into a tuple.

    Used to deserialize ``capabilities_json`` from the DB model.

    Args:
        raw: JSON string containing an array of capability dicts.

    Returns:
        Tuple of Capability instances.
    """
    data = json.loads(raw)
    return tuple(Capability.from_dict(cap) for cap in data)


def serialize_capabilities_json(capabilities: Sequence[Capability]) -> str:
    """Serialize capabilities to a compact JSON string.

    Args:
        capabilities: Capabilities to serialize.

    Returns:
        JSON string (compact, sorted keys).
    """
    return json.dumps(
        [cap.to_dict() for cap in capabilities],
        separators=(",", ":"),
        sort_keys=True,
    )
