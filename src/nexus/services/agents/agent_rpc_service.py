"""Agent RPC Service — replaces NexusFS agent management/lifecycle facades.

Consolidates agent registration, update, listing, deletion, lifecycle
transitions, and heartbeat behind ``@rpc_expose`` methods.
Wired via ``rpc_server.register_service()`` at server startup.

Issue #2033 — Phase 2.1 of LEGO microkernel decomposition.
"""

import contextlib
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusPermissionError
from nexus.contracts.rpc import rpc_expose
from nexus.contracts.types import VFSOperations, parse_operation_context

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC

logger = logging.getLogger(__name__)


class AgentRPCService:
    """RPC surface for agent management and lifecycle operations.

    Replaces ~1,170 LOC of facades in NexusFS (lines 3032-4202).
    Each method handles context normalisation and delegates to
    underlying domain services injected via the constructor.
    """

    def __init__(
        self,
        *,
        vfs: VFSOperations,
        metastore: "MetastoreABC",
        session_factory: Any,
        record_store: Any | None = None,
        agent_registry: Any | None = None,
        entity_registry: Any | None = None,
        rebac_manager: Any | None = None,
        wallet_provisioner: Any | None = None,
        api_key_creator: Any | None = None,
        key_service: Any | None = None,
        # For rmdir with admin override and rebac facade calls
        rmdir_fn: Any | None = None,
        rebac_create_fn: Any | None = None,
        rebac_list_tuples_fn: Any | None = None,
        rebac_delete_fn: Any | None = None,
        agent_warmup_service: Any | None = None,
    ) -> None:
        self._vfs = vfs
        self._metastore = metastore
        self._session_factory = session_factory
        self._record_store = record_store
        self._agent_registry = agent_registry
        self._entity_registry = entity_registry
        self._rebac_manager = rebac_manager
        self._wallet_provisioner = wallet_provisioner
        self._api_key_creator = api_key_creator
        self._key_service = key_service
        self._rmdir_fn = rmdir_fn
        self._rebac_create_fn = rebac_create_fn
        self._rebac_list_tuples_fn = rebac_list_tuples_fn
        self._rebac_delete_fn = rebac_delete_fn
        self._agent_warmup_service = agent_warmup_service

    # ------------------------------------------------------------------
    # Context Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_zone_id(context: dict | Any | None) -> str | None:
        from nexus.contracts.agent_utils import extract_zone_id

        return extract_zone_id(context)

    @staticmethod
    def _extract_user_id(context: dict | Any | None) -> str | None:
        from nexus.contracts.agent_utils import extract_user_id

        return extract_user_id(context)

    # ------------------------------------------------------------------
    # Config Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _create_agent_config_data(
        agent_id: str,
        name: str,
        user_id: str,
        description: str | None,
        created_at: str | None,
        metadata: dict | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        from nexus.contracts.agent_utils import create_agent_config_data

        return create_agent_config_data(
            agent_id=agent_id,
            name=name,
            user_id=user_id,
            description=description,
            created_at=created_at,
            metadata=metadata,
            api_key=api_key,
        )

    async def _write_agent_config(
        self,
        config_path: str,
        config_data: dict[str, Any],
        context: dict | Any | None,
    ) -> None:
        import yaml

        config_yaml = yaml.dump(config_data, default_flow_style=False, sort_keys=False)
        ctx = parse_operation_context(context)
        self._vfs.write(config_path, config_yaml.encode("utf-8"), context=ctx)

    # ------------------------------------------------------------------
    # Directory & Permission Helpers
    # ------------------------------------------------------------------

    async def _create_agent_directory(
        self,
        agent_id: str,
        user_id: str,
        agent_dir: str,
        config_path: str,
        config_data: dict[str, Any],
        context: dict | Any | None,
    ) -> None:
        try:
            ctx = parse_operation_context(context)
            self._vfs.mkdir(agent_dir, parents=True, exist_ok=True, context=ctx)
            await self._write_agent_config(config_path, config_data, context)

            if self._rebac_manager:
                zone_id = self._extract_zone_id(context) or ROOT_ZONE_ID
                try:
                    self._rebac_manager.rebac_write(
                        subject=("agent", agent_id),
                        relation="direct_owner",
                        object=("file", agent_dir),
                        zone_id=zone_id,
                    )
                except Exception as e:
                    logger.warning("Failed to grant direct_owner to agent for %s: %s", agent_dir, e)
                try:
                    self._rebac_manager.rebac_write(
                        subject=("user", user_id),
                        relation="direct_owner",
                        object=("file", agent_dir),
                        zone_id=zone_id,
                    )
                except Exception as e:
                    logger.warning("Failed to grant owner to user for %s: %s", agent_dir, e)
        except Exception as e:
            logger.warning("Failed to create agent directory or config: %s", e)

    def _grant_agent_self_permission(
        self,
        agent_id: str,
        agent_dir: str,
        zone_id: str,
        context: dict | None,
        _logger: logging.Logger,
    ) -> None:
        if self._rebac_create_fn is None:
            return
        try:
            self._rebac_create_fn(
                subject=("agent", agent_id),
                relation="viewer",
                object=("file", agent_dir),
                zone_id=zone_id,
                context=context,
            )
            _logger.info("Granted viewer permission to agent %s on %s", agent_id, agent_dir)
        except Exception as e:
            _logger.warning("Failed to grant viewer permission to agent: %s", e)

    # ------------------------------------------------------------------
    # Identity / Wallet / API Key Helpers
    # ------------------------------------------------------------------

    def _provision_agent_identity(
        self,
        agent_id: str,
        agent: dict,
        _logger: logging.Logger,
    ) -> str | None:
        from nexus.contracts.agent_utils import provision_agent_identity

        return provision_agent_identity(agent_id, agent, self._key_service, _logger)

    def _provision_agent_wallet(
        self,
        agent_id: str,
        zone_id: str,
        _logger: logging.Logger,
    ) -> None:
        from nexus.contracts.agent_utils import provision_agent_wallet

        provision_agent_wallet(agent_id, zone_id, self._wallet_provisioner, _logger)

    def _determine_agent_key_expiration(self, user_id: str, session: Any) -> datetime:
        from nexus.contracts.agent_utils import determine_agent_key_expiration

        return determine_agent_key_expiration(user_id, session)

    def _create_agent_api_key(self, agent_id: str, user_id: str, context: dict | Any | None) -> str:
        if self._api_key_creator is None:
            raise RuntimeError("API key creator not injected.")
        zone_id = self._extract_zone_id(context)
        session = self._session_factory()
        try:
            expires_at = self._determine_agent_key_expiration(user_id, session)
            _key_id, raw_key = self._api_key_creator.create_key(
                session,
                user_id=user_id,
                name=agent_id,
                subject_type="agent",
                subject_id=agent_id,
                zone_id=zone_id,
                expires_at=expires_at,
            )
            session.commit()
            return str(raw_key)
        finally:
            session.close()

    async def _provision_agent_api_key(
        self,
        agent_id: str,
        user_id: str,
        name: str,
        description: str | None,
        metadata: dict | None,
        agent: dict,
        config_path: str,
        context: dict | None,
        _logger: logging.Logger,
    ) -> None:
        try:
            raw_key = self._create_agent_api_key(agent_id, user_id, context)
            agent["api_key"] = raw_key
            agent["has_api_key"] = True
            try:
                updated = self._create_agent_config_data(
                    agent_id,
                    name,
                    user_id,
                    description,
                    agent.get("created_at"),
                    metadata,
                    raw_key,
                )
                await self._write_agent_config(config_path, updated, context)
            except Exception as e:
                _logger.warning("Failed to update config with API key: %s", e)
        except Exception as e:
            _logger.error("Failed to create API key for agent: %s", e)
            raise

    async def _write_agent_identity_document(
        self,
        agent_id: str,
        agent_did: str,
        agent_dir: str,
        context: dict | None,
        _logger: logging.Logger,
    ) -> None:
        try:
            import importlib as _il

            create_did_document = _il.import_module("nexus.bricks.identity.did").create_did_document

            assert self._key_service is not None
            key_record = self._key_service.get_active_keys(agent_id)[0]
            public_key = self._key_service._crypto.public_key_from_bytes(
                key_record.public_key_bytes
            )
            did_doc = create_did_document(agent_did, public_key)
            identity_dir = f"{agent_dir}/.identity"
            ctx = parse_operation_context(context)
            self._vfs.mkdir(identity_dir, parents=True, exist_ok=True, context=ctx)
            self._vfs.write(f"{identity_dir}/did.json", json.dumps(did_doc, indent=2), context=ctx)
            _logger.info("[KYA] Wrote DID document to %s/did.json", identity_dir)
        except Exception as e:
            _logger.warning("[KYA] Failed to write DID document: %s", e)

    # ------------------------------------------------------------------
    # Registry Helpers
    # ------------------------------------------------------------------

    def _ensure_agent_registry(self) -> None:
        if self._agent_registry is None:
            raise RuntimeError("AgentRegistry not available")

    def _check_agent_not_exists(self, agent_id: str, user_id: str, zone_id: str) -> None:
        agent_name_part = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        config_path = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}/config.yaml"
        try:
            existing_meta = self._metastore.get(config_path)
            if existing_meta:
                raise ValueError(
                    f"Agent already exists at {config_path}. "
                    "Cannot re-register. Delete the agent first."
                )
        except FileNotFoundError:
            pass

    def _ensure_entity_registry(self) -> None:
        if self._entity_registry is None:
            raise RuntimeError("EntityRegistry not available")

    # ------------------------------------------------------------------
    # Public RPC Methods — Agent Management
    # ------------------------------------------------------------------

    @rpc_expose(description="Register an AI agent")
    async def register_agent(
        self,
        agent_id: str,
        name: str,
        description: str | None = None,
        generate_api_key: bool = False,
        metadata: dict | None = None,
        capabilities: list[str] | None = None,
        context: dict | None = None,
    ) -> dict:
        """Register an AI agent."""
        user_id = self._extract_user_id(context)
        if not user_id:
            raise ValueError("user_id required in context to register agent")
        zone_id = self._extract_zone_id(context) or ROOT_ZONE_ID

        agent_name_part = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        agent_dir = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"

        self._check_agent_not_exists(agent_id, user_id, zone_id)
        self._ensure_agent_registry()
        assert self._agent_registry is not None

        desc = self._agent_registry.register_external(
            name=name,
            owner_id=user_id,
            zone_id=zone_id,
            connection_id=agent_id,
            labels={"capabilities": ",".join(capabilities or [])},
        )
        agent = {
            "agent_id": agent_id,
            "owner_id": user_id,
            "zone_id": zone_id,
            "name": name,
            "state": str(desc.state),
            "generation": desc.generation,
            "created_at": desc.created_at.isoformat(),
            "updated_at": desc.updated_at.isoformat(),
        }

        agent_did = self._provision_agent_identity(agent_id, agent, logger)
        self._provision_agent_wallet(agent_id, zone_id, logger)

        config_path = f"{agent_dir}/config.yaml"
        config_data = self._create_agent_config_data(
            agent_id,
            name,
            user_id,
            description,
            agent.get("created_at"),
            metadata,
        )
        await self._create_agent_directory(
            agent_id, user_id, agent_dir, config_path, config_data, context
        )
        agent["config_path"] = config_path

        self._grant_agent_self_permission(agent_id, agent_dir, zone_id, context, logger)

        if agent_did:
            await self._write_agent_identity_document(
                agent_id, agent_did, agent_dir, context, logger
            )

        if generate_api_key:
            await self._provision_agent_api_key(
                agent_id,
                user_id,
                name,
                description,
                metadata,
                agent,
                config_path,
                context,
                logger,
            )
        else:
            agent["has_api_key"] = False

        if capabilities:
            agent["capabilities"] = list(capabilities)
        return dict(agent)

    @rpc_expose(description="Update agent configuration")
    async def update_agent(
        self,
        agent_id: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        """Update an existing agent's configuration."""
        import yaml

        user_id = self._extract_user_id(context)
        if not user_id:
            raise ValueError("user_id required in context to update agent")
        zone_id = self._extract_zone_id(context) or ROOT_ZONE_ID

        agent_name_part = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        agent_dir = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"
        config_path = f"{agent_dir}/config.yaml"

        try:
            existing_meta = self._metastore.get(config_path)
            if not existing_meta:
                raise ValueError(f"Agent not found at {config_path}")
        except FileNotFoundError as e:
            raise ValueError(f"Agent not found: {agent_id}") from e

        ctx = parse_operation_context(context)
        existing_content = self._vfs.sys_read(config_path, context=ctx)
        if isinstance(existing_content, dict):
            existing_config = existing_content
        else:
            existing_config = yaml.safe_load(existing_content.decode("utf-8"))

        if name is not None:
            existing_config["name"] = name
        if description is not None:
            existing_config["description"] = description
        if metadata is not None:
            if "metadata" not in existing_config:
                existing_config["metadata"] = {}
            existing_config["metadata"].update(metadata)

        updated_yaml = yaml.dump(existing_config, default_flow_style=False, sort_keys=False)
        self._vfs.write(config_path, updated_yaml.encode("utf-8"), context=ctx)

        if self._entity_registry and (name is not None or description is not None):
            entity = self._entity_registry.get_entity("agent", agent_id)
            if entity and entity.entity_metadata:
                try:
                    entity_meta = json.loads(entity.entity_metadata)
                    if name is not None:
                        entity_meta["name"] = name
                    if description is not None:
                        entity_meta["description"] = description
                    from sqlalchemy import update

                    from nexus.storage.models import EntityRegistryModel

                    with self._entity_registry._get_session() as session:
                        stmt = (
                            update(EntityRegistryModel)
                            .where(
                                EntityRegistryModel.entity_type == "agent",
                                EntityRegistryModel.entity_id == agent_id,
                            )
                            .values(entity_metadata=json.dumps(entity_meta))
                        )
                        session.execute(stmt)
                        session.commit()
                except Exception as e:
                    logger.warning("Failed to update entity registry: %s", e)

        return {
            "agent_id": agent_id,
            "user_id": user_id,
            "name": existing_config.get("name"),
            "description": existing_config.get("description"),
            "metadata": existing_config.get("metadata", {}),
            "config_path": config_path,
        }

    @rpc_expose(description="List all registered agents")
    def list_agents(self, _context: dict | None = None) -> list[dict]:
        """List all registered agents."""
        self._ensure_entity_registry()
        assert self._entity_registry is not None
        entities = self._entity_registry.get_entities_by_type("agent")

        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        session = self._session_factory()
        try:
            agent_keys = {
                key.subject_id: key
                for key in session.scalars(
                    select(APIKeyModel).where(
                        APIKeyModel.subject_type == "agent", APIKeyModel.revoked == 0
                    )
                ).all()
            }
        finally:
            session.close()

        result = []
        for e in entities:
            meta: dict = {}
            if e.entity_metadata:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    meta = json.loads(e.entity_metadata)

            info: dict[str, Any] = {
                "agent_id": e.entity_id,
                "user_id": e.parent_id,
                "name": meta.get("name", e.entity_id),
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            if "description" in meta:
                info["description"] = meta["description"]

            agent_key = agent_keys.get(e.entity_id)
            if agent_key:
                info["has_api_key"] = True
                info["inherit_permissions"] = bool(agent_key.inherit_permissions)
            else:
                info["has_api_key"] = False
                inherit_perms = self._read_config_field(
                    e.entity_id, "inherit_permissions", _context
                )
                info["inherit_permissions"] = (
                    bool(inherit_perms) if inherit_perms is not None else True
                )

            result.append(info)
        return result

    @rpc_expose(description="Get agent information")
    async def get_agent(self, agent_id: str, _context: dict | None = None) -> dict | None:
        """Get information about a registered agent."""
        self._ensure_entity_registry()
        assert self._entity_registry is not None
        entity = self._entity_registry.get_entity("agent", agent_id)
        if not entity:
            return None

        meta: dict = {}
        if entity.entity_metadata:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                meta = json.loads(entity.entity_metadata)

        info: dict[str, Any] = {
            "agent_id": entity.entity_id,
            "user_id": entity.parent_id,
            "name": meta.get("name", entity.entity_id),
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
        }
        if "description" in meta:
            info["description"] = meta["description"]

        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        session = self._session_factory()
        try:
            agent_key = session.scalar(
                select(APIKeyModel).where(
                    APIKeyModel.subject_type == "agent",
                    APIKeyModel.subject_id == agent_id,
                    APIKeyModel.revoked == 0,
                )
            )
            if agent_key:
                info["has_api_key"] = True
                info["inherit_permissions"] = bool(agent_key.inherit_permissions)
            else:
                info["has_api_key"] = False
                inherit_perms = self._read_config_field(
                    entity.entity_id, "inherit_permissions", _context
                )
                info["inherit_permissions"] = (
                    bool(inherit_perms) if inherit_perms is not None else True
                )

            # Enrich from config.yaml
            await self._enrich_from_config(entity, info, _context, has_api_key=bool(agent_key))
        finally:
            session.close()
        return info

    @rpc_expose(description="Delete an agent")
    async def delete_agent(self, agent_id: str, _context: dict | None = None) -> bool:
        """Delete a registered agent."""
        # Ownership check: caller must own the agent or be admin
        ctx = parse_operation_context(_context)
        if "," in agent_id:
            owner_user_id = agent_id.split(",", 1)[0]
            if ctx.user_id and ctx.user_id != owner_user_id and not ctx.is_admin:
                raise NexusPermissionError(
                    f"Permission denied: only the agent owner or an admin can delete agent {agent_id}"
                )

        try:
            if "," in agent_id:
                user_id, agent_name_part = agent_id.split(",", 1)
                zone_id = self._extract_zone_id(_context) or ROOT_ZONE_ID
                agent_dir = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"

                # Delete directory
                if self._rmdir_fn:
                    try:
                        ctx = parse_operation_context(_context)
                        if self._vfs.access(agent_dir, context=ctx):
                            self._rmdir_fn(agent_dir, recursive=True, context=ctx, is_admin=True)
                    except Exception as e:
                        logger.warning("Failed to delete agent directory %s: %s", agent_dir, e)

                # Revoke API keys
                session = self._session_factory()
                try:
                    from sqlalchemy import update as sa_update

                    from nexus.storage.models import APIKeyModel

                    result = session.execute(
                        sa_update(APIKeyModel)
                        .where(
                            APIKeyModel.subject_type == "agent",
                            APIKeyModel.subject_id == agent_id,
                            APIKeyModel.revoked == 0,
                        )
                        .values(revoked=1)
                    )
                    session.commit()
                    if hasattr(result, "rowcount") and result.rowcount > 0:
                        logger.info("Revoked %d API key(s) for agent %s", result.rowcount, agent_id)
                except Exception as e:
                    logger.warning("Failed to revoke API keys for agent %s: %s", agent_id, e)
                    session.rollback()
                finally:
                    session.close()

                # Delete ReBAC permissions
                if self._rebac_list_tuples_fn and self._rebac_delete_fn:
                    try:
                        tuples = self._rebac_list_tuples_fn(subject=("agent", agent_id))
                        for t in tuples:
                            tid = t.get("tuple_id")
                            if tid:
                                try:
                                    self._rebac_delete_fn(tuple_id=tid)
                                except Exception as e:
                                    logger.warning("Failed to delete ReBAC tuple: %s", e)
                    except Exception as e:
                        logger.warning(
                            "Failed to delete ReBAC tuples for agent %s: %s", agent_id, e
                        )

                    try:
                        user_tuples = self._rebac_list_tuples_fn(
                            subject=("user", user_id),
                            object=("file", agent_dir),
                        )
                        for t in user_tuples:
                            tid = t.get("tuple_id")
                            if tid:
                                try:
                                    self._rebac_delete_fn(tuple_id=tid)
                                except Exception as e:
                                    logger.warning("Failed to delete user permission tuple: %s", e)
                    except Exception as e:
                        logger.warning("Failed to revoke user permissions: %s", e)
        except Exception as e:
            logger.warning("Failed to cleanup agent resources: %s", e)

        # Wallet cleanup
        if self._wallet_provisioner is not None:
            zone_id_for_wallet = self._extract_zone_id(_context) or ROOT_ZONE_ID
            try:
                cleanup_fn = getattr(self._wallet_provisioner, "cleanup", None)
                if cleanup_fn is not None:
                    cleanup_fn(agent_id, zone_id_for_wallet)
            except Exception as e:
                logger.warning("[WALLET] Failed to cleanup wallet for agent %s: %s", agent_id, e)

        self._ensure_agent_registry()
        assert self._agent_registry is not None
        try:
            self._agent_registry.unregister_external(agent_id)
            return True
        except Exception:
            logger.warning("Failed to unregister process %s", agent_id)
            return False

    # ------------------------------------------------------------------
    # Public RPC Methods — Agent Lifecycle
    # ------------------------------------------------------------------

    @rpc_expose(description="Transition agent lifecycle state")
    async def agent_transition(
        self,
        agent_id: str,
        target_state: str,
        expected_generation: int | None = None,
        context: dict | None = None,  # noqa: ARG002
    ) -> dict:
        """Transition an agent's lifecycle state with optimistic locking."""
        if not self._agent_registry:
            raise ValueError("AgentRegistry not available")
        from nexus.contracts.process_types import (
            AgentSignal,
            AgentState,
            InvalidTransitionError,
        )

        # Map legacy state names to signals
        _STATE_TO_SIGNAL = {
            "CONNECTED": AgentSignal.SIGCONT,
            "IDLE": AgentSignal.SIGSTOP,
            "SUSPENDED": AgentSignal.SIGSTOP,
        }
        sig = _STATE_TO_SIGNAL.get(target_state.upper())
        if sig is None:
            raise ValueError(
                f"Invalid target state '{target_state}'. Valid: CONNECTED, IDLE, SUSPENDED"
            )

        current = None
        if expected_generation is not None:
            current = self._agent_registry.get(agent_id)
            if current is None:
                raise ValueError(f"Agent '{agent_id}' not found")
            if current.generation != expected_generation:
                raise InvalidTransitionError(
                    f"stale generation for {agent_id}: expected {expected_generation}, got {current.generation}"
                )
        elif target_state.upper() == "CONNECTED":
            current = self._agent_registry.get(agent_id)
            if current is None:
                raise ValueError(f"Agent '{agent_id}' not found")

        if (
            target_state.upper() == "CONNECTED"
            and current is not None
            and current.state is AgentState.REGISTERED
        ):
            if self._agent_warmup_service is None:
                raise ValueError("AgentWarmupService not available")

            result = await self._agent_warmup_service.warmup(agent_id)
            if not result.success:
                raise InvalidTransitionError(result.error or f"warmup failed for {agent_id}")

            desc = self._agent_registry.get(agent_id)
            if desc is None:
                raise ValueError(f"Agent '{agent_id}' not found")
        else:
            desc = self._agent_registry.signal(agent_id, sig)
        return {
            "agent_id": desc.pid,
            "state": str(desc.state),
            "generation": desc.generation,
        }

    @rpc_expose(description="Record agent heartbeat")
    def agent_heartbeat(self, agent_id: str, context: dict | None = None) -> dict:  # noqa: ARG002
        """Record a heartbeat for an active agent."""
        if not self._agent_registry:
            raise ValueError("AgentRegistry not available")
        self._agent_registry.heartbeat(agent_id)
        return {"ok": True}

    @rpc_expose(description="List agents in a zone")
    def agent_list_by_zone(
        self,
        zone_id: str,
        state: str | None = None,
        context: dict | None = None,  # noqa: ARG002
    ) -> list[dict]:
        """List agents in a zone, optionally filtered by state."""
        if not self._agent_registry:
            raise ValueError("AgentRegistry not available")

        state_enum = None
        if state:
            from nexus.contracts.process_types import AgentState

            # Map legacy state names
            _STATE_MAP = {
                "CONNECTED": AgentState.BUSY,
                "IDLE": AgentState.READY,
                "SUSPENDED": AgentState.SUSPENDED,
            }
            state_enum = _STATE_MAP.get(state.upper())
            if state_enum is None:
                try:
                    state_enum = AgentState(state.lower())
                except ValueError as err:
                    raise ValueError(f"Invalid state filter '{state}'") from err

        records = self._agent_registry.list_processes(zone_id=zone_id, state=state_enum)
        return [
            {
                "agent_id": r.pid,
                "owner_id": r.owner_id,
                "zone_id": r.zone_id,
                "name": r.name,
                "state": str(r.state),
                "generation": r.generation,
                "last_heartbeat": (
                    r.external_info.last_heartbeat.isoformat()
                    if r.external_info and r.external_info.last_heartbeat
                    else None
                ),
                "created_at": r.created_at.isoformat(),
                "updated_at": r.updated_at.isoformat(),
            }
            for r in records
        ]

    # ------------------------------------------------------------------
    # Private helpers for config enrichment
    # ------------------------------------------------------------------

    async def _read_config_field(
        self,
        entity_id: str,
        field: str,
        context: dict | None,
    ) -> Any:
        """Read a single field from agent config.yaml. Returns None on failure."""
        try:
            if "," not in entity_id:
                return None
            user_id, agent_name = entity_id.split(",", 1)
            zone_id = self._extract_zone_id(context) or ROOT_ZONE_ID
            config_path = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name}/config.yaml"
            ctx = parse_operation_context(context)
            content = self._vfs.sys_read(config_path, context=ctx)
            import yaml

            if isinstance(content, bytes):
                data = yaml.safe_load(content.decode("utf-8"))
                return data.get(field)
        except Exception as exc:
            logger.debug(
                "Failed to read config field '%s' for entity '%s': %s", field, entity_id, exc
            )
        return None

    async def _enrich_from_config(
        self,
        entity: Any,
        info: dict[str, Any],
        context: dict | None,
        *,
        has_api_key: bool,
    ) -> None:
        """Enrich agent info dict from config.yaml fields."""
        try:
            if "," not in entity.entity_id:
                return
            user_id, agent_name = entity.entity_id.split(",", 1)
            zone_id = self._extract_zone_id(context) or ROOT_ZONE_ID
            config_path = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name}/config.yaml"
            ctx = parse_operation_context(context)
            content = self._vfs.sys_read(config_path, context=ctx)
            import yaml

            if not isinstance(content, bytes):
                return
            config_data = yaml.safe_load(content.decode("utf-8"))
            if not config_data:
                return

            if has_api_key and config_data.get("api_key"):
                info["api_key"] = config_data["api_key"]

            meta = config_data.get("metadata", {})
            if isinstance(meta, dict):
                if meta.get("platform"):
                    info["platform"] = meta["platform"]
                if meta.get("endpoint_url"):
                    info["endpoint_url"] = meta["endpoint_url"]
                if meta.get("agent_id"):
                    info["config_agent_id"] = meta["agent_id"]

            if not info.get("platform") and config_data.get("platform"):
                info["platform"] = config_data["platform"]
            if not info.get("endpoint_url") and config_data.get("endpoint_url"):
                info["endpoint_url"] = config_data["endpoint_url"]
            if (
                not info.get("config_agent_id")
                and config_data.get("agent_id")
                and config_data["agent_id"] != entity.entity_id
            ):
                info["config_agent_id"] = config_data["agent_id"]

            if config_data.get("system_prompt"):
                info["system_prompt"] = config_data["system_prompt"]
            if config_data.get("tools"):
                info["tools"] = config_data["tools"]

            if not has_api_key:
                inherit_perms = config_data.get("inherit_permissions")
                info["inherit_permissions"] = (
                    bool(inherit_perms) if inherit_perms is not None else True
                )
        except Exception as exc:
            logger.debug(
                "Failed to enrich agent info from config for '%s': %s", entity.entity_id, exc
            )
