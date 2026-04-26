"""Top-level agent registration service (Issue #3130).

Orchestrates the multi-step registration of a top-level agent:
1. Register in entity_registry (persistent agent identity, DB)
2. Register in AgentRegistry (runtime liveness, in-memory)
3. Create ReBAC permission tuples for grants
4. Create permanent API key (no TTL)
5. Provision IPC directories (inbox/outbox/processed/dead_letter)
6. Optionally store a client-supplied Ed25519 public key

Steps 1-2 are compensated on failure (delete entity + unregister process).
Step 3 is also compensated (rebac_delete_by_subject) — rebac_write_batch()
persists immediately and must be explicitly cleaned up on rollback.
Step 5 (IPC filesystem) delegates to AgentRegistry.provision(), which calls
the injected AgentProvisioner. Runs after DB commit; failure is non-fatal.
Step 6 (public key) is non-fatal; agent can register a key later.

Design follows the DelegationService saga pattern: ordered steps with
manual compensation via try/except.
"""

import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.grant_helpers import GrantInput, grants_to_rebac_tuples

if TYPE_CHECKING:
    from nexus.contracts.protocols.entity_registry import EntityRegistryProtocol
    from nexus.services.agents.agent_registry import AgentRegistry
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class AgentAlreadyExistsError(Exception):
    """Raised when attempting to register an agent_id that already exists."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent '{agent_id}' already exists")


@dataclass(frozen=True)
class RegistrationResult:
    """Immutable result of a successful agent registration."""

    agent_id: str
    api_key: str
    key_id: str
    owner_id: str
    zone_id: str
    grants_created: int
    ipc_provisioned: bool
    ipc_inbox: str | None
    public_key_registered: bool


class AgentRegistrationService:
    """Orchestrates top-level agent registration (admin-only).

    Dependencies are injected at construction time. All state mutations
    go through the injected services — no direct DB access.

    Args:
        record_store: RecordStoreABC for session creation (API key step).
        entity_registry: EntityRegistry for persistent agent identity.
        agent_registry: AgentRegistry for runtime agent liveness + IPC provisioning.
        rebac_manager: EnhancedReBACManager for permission tuples.
        key_service: Optional KeyService for Ed25519 public key storage.
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        entity_registry: "EntityRegistryProtocol | None" = None,
        agent_registry: "AgentRegistry | None" = None,
        rebac_manager: Any = None,
        key_service: Any = None,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._entity_registry = entity_registry
        self._agent_registry = agent_registry
        self._rebac_manager = rebac_manager
        self._key_service = key_service
        logger.info("[AgentRegistration] Initialized")

    async def register(
        self,
        agent_id: str,
        name: str,
        owner_id: str,
        zone_id: str | None = None,
        grants: list[GrantInput] | None = None,
        ipc: bool = True,
        public_key_hex: str | None = None,
    ) -> RegistrationResult:
        """Register a top-level agent with identity, key, grants, and IPC.

        Steps (in order):
            1. Register in entity_registry (persistent identity, DB).
               409 if agent_id already exists.
            2. Register in AgentRegistry (runtime liveness, in-memory).
            3. Create ReBAC permission tuples (if grants provided).
            4. Create permanent API key (no TTL, subject_type="agent").
            5. Provision IPC directories (if ipc=True, async filesystem).
            6. Register Ed25519 public key (if public_key_hex provided).

        Steps 1-4 are compensated on failure. Steps 5-6 are non-fatal.

        Args:
            agent_id: Unique agent identifier.
            name: Human-readable agent name.
            owner_id: User ID of the admin registering the agent.
            zone_id: Zone scope (defaults to ROOT_ZONE_ID).
            grants: Optional list of path+role grants.
            ipc: Whether to provision IPC directories (default True).
            public_key_hex: Optional hex-encoded Ed25519 public key.

        Returns:
            RegistrationResult with agent_id, api_key, and metadata.

        Raises:
            AgentAlreadyExistsError: If agent_id is already registered.
            ValueError: If grants are invalid.
        """
        effective_zone = zone_id or ROOT_ZONE_ID
        grants = grants or []
        grants_created = 0

        # ── Step 1: Persistent identity in entity_registry ───────────
        if self._entity_registry is not None:
            existing = self._entity_registry.get_entity("agent", agent_id)
            if existing is not None:
                raise AgentAlreadyExistsError(agent_id)

            self._entity_registry.register_entity(
                entity_type="agent",
                entity_id=agent_id,
                parent_type="user",
                parent_id=owner_id,
                entity_metadata={"name": name, "zone_id": effective_zone},
            )

        # ── Step 2: Runtime liveness in AgentRegistry ─────────────────
        if self._agent_registry is not None:
            self._agent_registry.register_external(
                name,
                owner_id,
                effective_zone,
                connection_id=agent_id,
                labels={"registered_by": "admin", "agent_id": agent_id},
            )

        try:
            # ── Step 3: Create ReBAC grants ──────────────────────────
            if grants and self._rebac_manager is not None:
                tuples = grants_to_rebac_tuples(grants, agent_id, effective_zone)
                grants_created = self._rebac_manager.rebac_write_batch(tuples)
                logger.info(
                    "[AgentRegistration] Created %d ReBAC grants for agent %s",
                    grants_created,
                    agent_id,
                )

            # ── Step 4: Create permanent API key ─────────────────────
            from nexus.storage.api_key_ops import create_agent_api_key

            session = self._session_factory()
            try:
                key_id, raw_key = create_agent_api_key(
                    session,
                    agent_id=agent_id,
                    agent_name=name,
                    owner_id=owner_id,
                    zone_id=effective_zone,
                    expires_at=None,  # Permanent key for top-level agents
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        except Exception:
            # Compensation: remove identity + process + ReBAC grants.
            # ReBAC write_batch() persists immediately so must be cleaned up.
            logger.warning(
                "[AgentRegistration] Rolling back agent %s after step 2/3/4 failure",
                agent_id,
            )
            if self._rebac_manager is not None and grants_created > 0:
                try:
                    deleted = self._rebac_manager.rebac_delete_by_subject(
                        subject_type="agent",
                        subject_id=agent_id,
                        zone_id=effective_zone,
                    )
                    logger.info(
                        "[AgentRegistration] Cleaned up %d ReBAC tuples for agent %s",
                        deleted,
                        agent_id,
                    )
                except Exception as cleanup_exc:
                    logger.error(
                        "[AgentRegistration] Failed to clean up ReBAC tuples for %s: %s",
                        agent_id,
                        cleanup_exc,
                    )
            if self._agent_registry is not None:
                with contextlib.suppress(Exception):
                    self._agent_registry.unregister_external(agent_id)
            if self._entity_registry is not None:
                with contextlib.suppress(Exception):
                    self._entity_registry.delete_entity("agent", agent_id)
            raise

        # ── Step 5: Provision IPC (filesystem, after DB commit) ──────
        # Provisioning is delegated to AgentRegistry.provision() which
        # calls the injected AgentProvisioner (if configured).
        ipc_provisioned = False
        ipc_inbox: str | None = None
        if ipc and self._agent_registry is not None:
            t0 = time.monotonic()
            ipc_provisioned = await self._agent_registry.provision(agent_id, name=name)
            elapsed_ms = (time.monotonic() - t0) * 1000
            if ipc_provisioned:
                ipc_inbox = f"/ipc/{agent_id}/inbox/"
                logger.info(
                    "[AgentRegistration] IPC provisioned for %s (%.1fms)",
                    agent_id,
                    elapsed_ms,
                )

        # ── Step 6: Register Ed25519 public key (optional) ───────────
        public_key_registered = False
        if public_key_hex and self._key_service is not None:
            try:
                self._register_public_key(agent_id, public_key_hex)
                public_key_registered = True
                logger.info(
                    "[AgentRegistration] Public key registered for agent %s",
                    agent_id,
                )
            except Exception as exc:
                # Non-fatal — agent can register a key later via identity endpoint
                logger.warning(
                    "[AgentRegistration] Public key registration failed for %s: %s",
                    agent_id,
                    exc,
                )

        logger.info(
            "[AgentRegistration] Registered agent %s (owner=%s, grants=%d, ipc=%s, key=%s)",
            agent_id,
            owner_id,
            grants_created,
            ipc_provisioned,
            public_key_registered,
        )

        return RegistrationResult(
            agent_id=agent_id,
            api_key=raw_key,
            key_id=key_id,
            owner_id=owner_id,
            zone_id=effective_zone,
            grants_created=grants_created,
            ipc_provisioned=ipc_provisioned,
            ipc_inbox=ipc_inbox if ipc_provisioned else None,
            public_key_registered=public_key_registered,
        )

    def _register_public_key(self, agent_id: str, public_key_hex: str) -> None:
        """Store a client-supplied Ed25519 public key via KeyService.

        Delegates to KeyService.register_public_key() which owns the
        CLIENT_HELD sentinel logic and ensures ensure_keypair() /
        decrypt_private_key() behave correctly for client-held keys.
        """
        pub_bytes = bytes.fromhex(public_key_hex)
        self._key_service.register_public_key(agent_id, pub_bytes)
