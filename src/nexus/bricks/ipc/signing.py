"""IPC message signing and verification via DID (#1729).

Provides Ed25519 signing on send and verification on receive,
with zone-level enforcement policy.

Classes:
    SigningMode: Three-state enforcement (off/verify_only/enforce).
    MessageSigner: Signs IPC envelopes using agent's Ed25519 key.
    MessageVerifier: Verifies IPC envelope signatures.
    VerifyResult: Frozen result of a verification attempt.
"""

import base64
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nexus.bricks.ipc.envelope import MessageEnvelope
from nexus.bricks.ipc.protocols import CryptoProtocol, KeyServiceProtocol
from nexus.storage.zone_settings import SigningMode

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

# Re-export SigningMode so callers can import from ipc.signing
__all__ = ["MessageSigner", "MessageVerifier", "SigningMode", "VerifyResult"]


@dataclass(frozen=True)
class VerifyResult:
    """Frozen result of a signature verification attempt."""

    valid: bool
    detail: str = ""


class MessageSigner:
    """Signs IPC envelopes using agent's Ed25519 key.

    Auto-provisions a keypair on first sign via ``ensure_keypair()``.
    Caches the decrypted private key to avoid repeated decryption.

    Args:
        key_service: Key management protocol for provisioning and lookup.
        crypto: Cryptographic operations protocol for signing.
        agent_id: The agent whose identity is used for signing.
    """

    def __init__(
        self,
        key_service: KeyServiceProtocol,
        crypto: CryptoProtocol,
        agent_id: str,
    ) -> None:
        self._key_service = key_service
        self._crypto = crypto
        self._agent_id = agent_id
        self._cached_key_id: str | None = None
        self._cached_private_key: Any = None
        self._cached_did: str | None = None

    def sign(self, envelope: MessageEnvelope) -> MessageEnvelope:
        """Return a new envelope with signature fields populated.

        The original envelope is NOT mutated (frozen Pydantic model).

        Args:
            envelope: The envelope to sign.

        Returns:
            A new MessageEnvelope with signature, signer_did, signer_key_id set.
        """
        self._ensure_key()

        assert self._cached_private_key is not None  # noqa: S101
        assert self._cached_key_id is not None  # noqa: S101
        assert self._cached_did is not None  # noqa: S101

        signing_data = envelope.signing_bytes()
        raw_signature = self._crypto.sign(signing_data, self._cached_private_key)
        encoded_signature = base64.b64encode(raw_signature).decode("ascii")

        return envelope.model_copy(
            update={
                "signature": encoded_signature,
                "signer_did": self._cached_did,
                "signer_key_id": self._cached_key_id,
            }
        )

    def _ensure_key(self) -> None:
        """Auto-provision + cache key on first call."""
        if self._cached_private_key is not None:
            return

        record = self._key_service.ensure_keypair(self._agent_id)
        self._cached_key_id = record.key_id
        self._cached_did = record.did
        self._cached_private_key = self._key_service.decrypt_private_key(record.key_id)


class MessageVerifier:
    """Verifies IPC envelope signatures.

    Uses key service protocol for public key lookup.

    Args:
        key_service: Key management protocol for public key lookup.
        crypto: Cryptographic operations protocol for verification.
    """

    def __init__(self, key_service: KeyServiceProtocol, crypto: CryptoProtocol) -> None:
        self._key_service = key_service
        self._crypto = crypto

    def verify(self, envelope: MessageEnvelope) -> VerifyResult:
        """Verify envelope signature.

        Checks:
        1. Signature fields present
        2. Key lookup succeeds
        3. Key is active (not revoked, not expired)
        4. Ed25519 signature is valid

        Args:
            envelope: The envelope to verify.

        Returns:
            VerifyResult with valid=True/False and detail message.
        """
        if envelope.signature is None or envelope.signer_key_id is None:
            return VerifyResult(valid=False, detail="No signature present (unsigned message)")

        # Lookup public key record
        record = self._key_service.get_public_key(envelope.signer_key_id)
        if record is None:
            return VerifyResult(
                valid=False,
                detail=f"Key not found for key_id={envelope.signer_key_id}",
            )

        # Check key status
        if not record.is_active:
            return VerifyResult(
                valid=False,
                detail=f"Key {record.key_id} is not active (revoked or disabled)",
            )

        if record.revoked_at is not None:
            return VerifyResult(
                valid=False,
                detail=f"Key {record.key_id} was revoked at {record.revoked_at}",
            )

        # Check key expiration
        expires_at = getattr(record, "expires_at", None)
        if expires_at is not None:
            from datetime import UTC, datetime

            if datetime.now(UTC) >= expires_at:
                return VerifyResult(
                    valid=False,
                    detail=f"Key {record.key_id} expired at {expires_at}",
                )

        # Decode signature
        try:
            raw_signature = base64.b64decode(envelope.signature)
        except Exception as exc:
            return VerifyResult(
                valid=False,
                detail=f"Failed to decode signature: {exc}",
            )

        # Reconstruct public key and verify
        public_key = self._crypto.public_key_from_bytes(record.public_key_bytes)
        signing_data = envelope.signing_bytes()
        is_valid = self._crypto.verify(signing_data, raw_signature, public_key)

        if not is_valid:
            return VerifyResult(
                valid=False,
                detail="Invalid signature: Ed25519 verification failed",
            )

        return VerifyResult(valid=True, detail="Signature verified")
