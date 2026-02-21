"""DB-backed credential service for agent capability attestation (Issue #1753).

Orchestrates ``CapabilityIssuer`` + ``CapabilityVerifier`` + database
persistence for the full credential lifecycle: issue, verify, revoke, list.

Maintains an in-memory revocation cache (frozenset) that is periodically
refreshed from the database.  The cache is safe for federation because
revocations are immutable (a revocation cannot be undone).

References:
    - NEXUS-LEGO-ARCHITECTURE.md §3.4: Brick rules (zero core imports)
    - KERNEL-ARCHITECTURE.md §3: Service layer
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from nexus.contracts.credential_types import (
    Capability,
    CredentialClaims,
    CredentialStatus,
)
from nexus.identity.credentials import (
    CapabilityIssuer,
    CapabilityVerifier,
    DelegationChain,
    serialize_capabilities_json,
)
from nexus.storage.models.identity import AgentCredentialModel

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.orm import Session

    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    """Return current UTC time as a naive datetime (SQLite-compatible)."""
    return datetime.now(UTC).replace(tzinfo=None)


class CredentialService:
    """DB-backed credential lifecycle manager.

    Provides issue, verify, revoke, list operations with database
    persistence and an in-memory revocation cache.

    Args:
        record_store: Database access (session_factory).
        issuer: Pre-configured CapabilityIssuer (kernel signing key).
        verifier: Pre-configured CapabilityVerifier (trusted issuer keys).
        revocation_cache_ttl: Seconds between revocation cache refreshes.
    """

    def __init__(
        self,
        record_store: RecordStoreABC,
        issuer: CapabilityIssuer,
        verifier: CapabilityVerifier,
        revocation_cache_ttl: float = 30.0,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._issuer = issuer
        self._verifier = verifier
        self._delegation_chain = DelegationChain(issuer, verifier)
        self._revocation_cache_ttl = revocation_cache_ttl
        self._last_cache_refresh: float = 0.0
        self._lock = threading.Lock()

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        """Create a session with auto-commit/rollback."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def issue_credential(
        self,
        subject_agent_id: str,
        subject_did: str,
        capabilities: Sequence[Capability],
        *,
        ttl_seconds: int = 3600,
        parent_credential_id: str | None = None,
        delegation_depth: int = 0,
    ) -> CredentialClaims:
        """Issue a new capability credential and persist to database.

        Args:
            subject_agent_id: Agent ID for the credential subject.
            subject_did: DID of the subject agent.
            capabilities: Capabilities to grant.
            ttl_seconds: Credential TTL in seconds.
            parent_credential_id: Parent credential for delegations.
            delegation_depth: Delegation chain depth.

        Returns:
            Parsed CredentialClaims for the issued credential.

        Raises:
            ValueError: If validation fails.
        """
        if not subject_agent_id:
            raise ValueError("subject_agent_id must be non-empty")

        token, claims = self._issuer.issue(
            subject_did=subject_did,
            capabilities=capabilities,
            ttl_seconds=ttl_seconds,
            parent_credential_id=parent_credential_id,
            delegation_depth=delegation_depth,
        )

        # Persist to database
        now = _utcnow_naive()
        expires_at = datetime.fromtimestamp(claims.expires_at, tz=UTC).replace(tzinfo=None)

        model = AgentCredentialModel(
            credential_id=claims.credential_id,
            issuer_did=claims.issuer_did,
            subject_did=claims.subject_did,
            subject_agent_id=subject_agent_id,
            signing_key_id=self._issuer.key_id,
            jwt_token=token,
            capabilities_json=serialize_capabilities_json(capabilities),
            parent_credential_id=parent_credential_id,
            delegation_depth=delegation_depth,
            is_active=1,
            created_at=now,
            expires_at=expires_at,
        )

        with self._get_session() as session:
            session.add(model)
            session.flush()

        logger.info(
            "[VC] Persisted credential %s for agent %s",
            claims.credential_id,
            subject_agent_id,
        )

        return claims

    def verify_credential(self, token: str) -> CredentialClaims:
        """Verify a JWT-VC token.

        Refreshes the revocation cache if stale, then delegates
        to the verifier.

        Args:
            token: JWT-VC token string.

        Returns:
            Parsed CredentialClaims.

        Raises:
            ValueError: If verification fails.
        """
        self._maybe_refresh_cache()
        return self._verifier.verify(token)

    def revoke_credential(self, credential_id: str) -> bool:
        """Revoke a credential by ID.

        Args:
            credential_id: Credential ID to revoke.

        Returns:
            True if credential was found and revoked, False if not found.
        """
        now = _utcnow_naive()

        with self._get_session() as session:
            model = session.execute(
                select(AgentCredentialModel).where(
                    AgentCredentialModel.credential_id == credential_id
                )
            ).scalar_one_or_none()

            if model is None:
                return False

            if not model.is_active:
                return True  # Already revoked

            session.execute(
                update(AgentCredentialModel)
                .where(AgentCredentialModel.credential_id == credential_id)
                .values(is_active=0, revoked_at=now)
            )

        # Eagerly update cache
        self._force_refresh_cache()

        logger.info("[VC] Revoked credential %s", credential_id)
        return True

    def get_credential_status(self, credential_id: str) -> CredentialStatus | None:
        """Get status of a credential.

        Args:
            credential_id: Credential ID to query.

        Returns:
            CredentialStatus or None if not found.
        """
        with self._get_session() as session:
            model = session.execute(
                select(AgentCredentialModel).where(
                    AgentCredentialModel.credential_id == credential_id
                )
            ).scalar_one_or_none()

            if model is None:
                return None

            return self._model_to_status(model)

    def list_agent_credentials(
        self,
        agent_id: str,
        *,
        active_only: bool = True,
    ) -> list[CredentialStatus]:
        """List credentials for an agent.

        Args:
            agent_id: Agent ID to query.
            active_only: If True, only return active (non-revoked) credentials.

        Returns:
            List of CredentialStatus snapshots.
        """
        with self._get_session() as session:
            stmt = select(AgentCredentialModel).where(
                AgentCredentialModel.subject_agent_id == agent_id
            )
            if active_only:
                stmt = stmt.where(AgentCredentialModel.is_active == 1)

            stmt = stmt.order_by(AgentCredentialModel.created_at.desc())
            models = list(session.execute(stmt).scalars().all())

            return [self._model_to_status(m) for m in models]

    def revoke_by_signing_key(self, key_id: str) -> int:
        """Revoke all credentials signed by a specific key.

        Used for cascade revocation when a signing key is revoked.

        Args:
            key_id: Signing key ID.

        Returns:
            Number of credentials revoked.
        """
        now = _utcnow_naive()

        with self._get_session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(AgentCredentialModel)
                    .where(AgentCredentialModel.signing_key_id == key_id)
                    .where(AgentCredentialModel.is_active == 1)
                    .values(is_active=0, revoked_at=now)
                ),
            )
            count: int = result.rowcount or 0

        if count > 0:
            self._force_refresh_cache()
            logger.info(
                "[VC] Cascade-revoked %d credentials for signing key %s",
                count,
                key_id,
            )

        return count

    def delegate_credential(
        self,
        parent_token: str,
        delegate_agent_id: str,
        delegate_did: str,
        attenuated_capabilities: Sequence[Capability],
        *,
        ttl_seconds: int = 1800,
    ) -> CredentialClaims:
        """Delegate attenuated capabilities to another agent.

        Args:
            parent_token: JWT-VC of the parent credential.
            delegate_agent_id: Agent ID of the delegate.
            delegate_did: DID of the delegate agent.
            attenuated_capabilities: Capabilities to grant (subset of parent).
            ttl_seconds: TTL for delegated credential.

        Returns:
            Parsed CredentialClaims for the delegated credential.

        Raises:
            ValueError: If parent is invalid or attenuation fails.
        """
        self._maybe_refresh_cache()

        token, claims = self._delegation_chain.delegate(
            parent_token=parent_token,
            delegate_did=delegate_did,
            attenuated_capabilities=attenuated_capabilities,
            ttl_seconds=ttl_seconds,
        )

        # Persist the delegated credential
        now = _utcnow_naive()
        expires_at = datetime.fromtimestamp(claims.expires_at, tz=UTC).replace(tzinfo=None)

        model = AgentCredentialModel(
            credential_id=claims.credential_id,
            issuer_did=claims.issuer_did,
            subject_did=claims.subject_did,
            subject_agent_id=delegate_agent_id,
            signing_key_id=self._issuer.key_id,
            jwt_token=token,
            capabilities_json=serialize_capabilities_json(attenuated_capabilities),
            parent_credential_id=claims.parent_credential_id,
            delegation_depth=claims.delegation_depth,
            is_active=1,
            created_at=now,
            expires_at=expires_at,
        )

        with self._get_session() as session:
            session.add(model)
            session.flush()

        logger.info(
            "[VC] Delegated credential %s to agent %s (depth=%d)",
            claims.credential_id,
            delegate_agent_id,
            claims.delegation_depth,
        )

        return claims

    # ------------------------------------------------------------------
    # Revocation cache management
    # ------------------------------------------------------------------

    def _maybe_refresh_cache(self) -> None:
        """Refresh revocation cache if TTL has elapsed."""
        now = time.monotonic()
        if now - self._last_cache_refresh >= self._revocation_cache_ttl:
            self._force_refresh_cache()

    def _force_refresh_cache(self) -> None:
        """Force-refresh the in-memory revocation cache from DB."""
        with self._lock:
            with self._get_session() as session:
                stmt = select(AgentCredentialModel.credential_id).where(
                    AgentCredentialModel.is_active == 0
                )
                revoked_ids = frozenset(row[0] for row in session.execute(stmt).all())

            self._verifier.update_revocation_cache(revoked_ids)
            self._last_cache_refresh = time.monotonic()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _model_to_status(model: AgentCredentialModel) -> CredentialStatus:
        """Convert ORM model to frozen dataclass."""
        return CredentialStatus(
            credential_id=model.credential_id,
            issuer_did=model.issuer_did,
            subject_did=model.subject_did,
            subject_agent_id=model.subject_agent_id,
            is_active=bool(model.is_active),
            created_at=model.created_at,
            expires_at=model.expires_at,
            revoked_at=model.revoked_at,
            delegation_depth=model.delegation_depth,
            parent_credential_id=model.parent_credential_id,
        )
