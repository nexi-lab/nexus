"""Agent management service — extracted from NexusFS kernel (Task #634).

ARCHITECTURAL DECISION (KERNEL-ARCHITECTURE.md §3):
    "Services depend on kernel interfaces, never the reverse."

    Agent CRUD, provisioning (identity, wallet, API key, directory, permissions),
    and lifecycle (transition, heartbeat, zone listing) are service-layer business
    logic. This module lives at ``nexus/services/agents/agent_service.py`` by design.

    The NexusFS kernel has **zero** agent management code after this extraction.
    The server registers AgentService as an additional RPC source so that the
    existing ``@rpc_expose`` methods continue to work identically.
"""

import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusPermissionError
from nexus.contracts.types import OperationContext
from nexus.lib.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Standalone context helpers (no NexusFS dependency)
# ---------------------------------------------------------------------------


def _parse_context(context: OperationContext | dict | None = None) -> OperationContext:
    """Parse context dict or OperationContext into OperationContext."""
    if isinstance(context, OperationContext):
        return context
    if context is None:
        context = {}
    return OperationContext(
        user_id=context.get("user_id", "system"),
        groups=context.get("groups", []),
        zone_id=context.get("zone_id"),
        agent_id=context.get("agent_id"),
        is_admin=context.get("is_admin", False),
        is_system=context.get("is_system", False),
    )


def _extract_user_id(context: dict | Any | None) -> str | None:
    """Extract user_id from context (dict or OperationContext)."""
    if not context:
        return None
    if isinstance(context, dict):
        return context.get("user_id")
    return getattr(context, "user_id", None)


def _extract_zone_id(context: dict | Any | None) -> str | None:
    """Extract zone_id from context (dict or OperationContext)."""
    if not context:
        return None
    if isinstance(context, dict):
        return context.get("zone_id")
    return getattr(context, "zone_id", None)


# ---------------------------------------------------------------------------
# AgentService
# ---------------------------------------------------------------------------


class AgentService:
    """Agent management service with explicit dependency injection.

    Takes individual dependencies — never the whole NexusFS kernel.

    Args:
        fs: VFS interface (read, write, mkdir, rmdir, exists, metadata).
        agent_registry: AgentRegistry instance (lifecycle state machine).
        session_factory: SQLAlchemy SessionLocal callable.
        entity_registry: EntityRegistry for entity lookups.
        rebac_manager: ReBAC permission manager (rebac_write, rebac_list_tuples, rebac_delete).
        key_service: KeyService for DID/keypair provisioning.
        wallet_provisioner: Callable(agent_id, zone_id) for wallet auto-provisioning.
        api_key_creator: Protocol with create_key() for API key generation.
        record_store: RecordStoreABC for lazy AgentRegistry creation.
    """

    def __init__(
        self,
        fs: Any,
        agent_registry: Any | None = None,
        session_factory: Any | None = None,
        entity_registry: Any | None = None,
        rebac_manager: Any | None = None,
        key_service: Any | None = None,
        wallet_provisioner: "Callable | None" = None,
        api_key_creator: Any | None = None,
        record_store: Any | None = None,
    ) -> None:
        self._fs = fs
        self._agent_registry = agent_registry
        self._session_factory = session_factory
        self._entity_registry = entity_registry
        self._rebac_manager = rebac_manager
        self._key_service = key_service
        self._wallet_provisioner = wallet_provisioner
        self._api_key_creator = api_key_creator
        self._record_store = record_store

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_agent_registry(self) -> None:
        """Lazily create AgentRegistry if not already set."""
        if self._agent_registry is not None:
            return

        if self._record_store is None:
            raise RuntimeError(
                "AgentRegistry not initialized and no record_store available "
                "to create one. Provide a record_store when constructing AgentService."
            )

        from nexus.system_services.agents.agent_registry import AgentRegistry

        self._agent_registry = AgentRegistry(
            record_store=self._record_store,
            entity_registry=self._entity_registry,
        )

    def _ensure_entity_registry(self) -> None:
        """Lazily create EntityRegistry if not already set."""
        if self._entity_registry is not None:
            return

        if self._record_store is None:
            raise RuntimeError("EntityRegistry requires record_store")

        import importlib as _il

        EntityRegistry = _il.import_module("nexus.bricks.rebac.entity_registry").EntityRegistry
        self._entity_registry = EntityRegistry(self._record_store)

    def _create_agent_config_data(
        self,
        agent_id: str,
        name: str,
        user_id: str,
        description: str | None,
        created_at: str | None,
        metadata: dict | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Create agent config.yaml data structure."""
        config_data: dict[str, Any] = {
            "agent_id": agent_id,
            "name": name,
            "user_id": user_id,
            "description": description,
            "created_at": created_at,
        }
        if metadata:
            config_data["metadata"] = metadata.copy()
        if api_key is not None:
            config_data["api_key"] = api_key
        return config_data

    def _write_agent_config(
        self,
        config_path: str,
        config_data: dict[str, Any],
        context: dict | Any | None,
    ) -> None:
        """Write agent config.yaml file."""
        import yaml

        config_yaml = yaml.dump(config_data, default_flow_style=False, sort_keys=False)
        ctx = _parse_context(context)
        self._fs.sys_write(config_path, config_yaml.encode("utf-8"), context=ctx)

    def _create_agent_directory(
        self,
        agent_id: str,
        user_id: str,
        agent_dir: str,
        config_path: str,
        config_data: dict[str, Any],
        context: dict | Any | None,
    ) -> None:
        """Create agent directory, config file, and grant ReBAC permissions."""
        try:
            ctx = _parse_context(context)
            self._fs.sys_mkdir(agent_dir, parents=True, exist_ok=True, context=ctx)
            self._write_agent_config(config_path, config_data, context)

            if self._rebac_manager:
                zone_id = _extract_zone_id(context) or ROOT_ZONE_ID

                try:
                    logger.debug(
                        "register_agent: Granting direct_owner to agent %s for %s",
                        agent_id,
                        agent_dir,
                    )
                    self._rebac_manager.rebac_write(
                        subject=("agent", agent_id),
                        relation="direct_owner",
                        object=("file", agent_dir),
                        zone_id=zone_id,
                    )
                    logger.debug("register_agent: Granted direct_owner to agent %s", agent_id)
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
                    logger.warning(
                        "Failed to grant owner permission to user for %s: %s", agent_dir, e
                    )

        except Exception as e:
            logger.warning("Failed to create agent directory or config: %s", e)

    def _determine_agent_key_expiration(
        self,
        user_id: str,
        session: Any,
    ) -> datetime:
        """Determine expiration date for agent API key based on owner's key."""
        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        stmt = (
            select(APIKeyModel)
            .where(
                APIKeyModel.user_id == user_id,
                APIKeyModel.revoked == 0,
                APIKeyModel.subject_type != "agent",
            )
            .order_by(APIKeyModel.created_at.desc())
        )
        owner_key = session.scalar(stmt)

        if owner_key and owner_key.expires_at:
            now = datetime.now(UTC)
            owner_expires: datetime = owner_key.expires_at
            if owner_expires.tzinfo is None:
                owner_expires = owner_expires.replace(tzinfo=UTC)

            if owner_expires > now:
                return owner_expires
            else:
                raise ValueError(
                    f"Cannot generate API key for agent: Your API key has expired on "
                    f"{owner_expires.isoformat()}. "
                    "Please renew your API key before creating agent API keys."
                )
        else:
            return datetime.now(UTC) + timedelta(days=365)

    def _create_agent_api_key(
        self,
        agent_id: str,
        user_id: str,
        context: dict | Any | None,
    ) -> str:
        """Create API key for agent and return the raw key."""
        if self._api_key_creator is None:
            raise RuntimeError(
                "API key creator not injected. "
                "Use factory.create_nexus_services() to wire auth services."
            )

        zone_id = _extract_zone_id(context)
        assert self._session_factory is not None
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
            return cast(str, raw_key)
        finally:
            session.close()

    def _check_agent_not_exists(
        self,
        agent_id: str,
        user_id: str,
        zone_id: str,
    ) -> None:
        """Raise ValueError if agent config already exists on the filesystem."""
        agent_name_part = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        agent_dir = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"
        config_path = f"{agent_dir}/config.yaml"
        try:
            existing_meta = self._fs.metadata.get(config_path)
            if existing_meta:
                raise ValueError(
                    f"Agent already exists at {config_path}. "
                    f"Cannot re-register existing agent. "
                    f"Delete the agent first if you want to recreate it."
                )
        except FileNotFoundError:
            pass

    def _provision_agent_identity(
        self,
        agent_id: str,
        agent: dict,
        _logger: logging.Logger,
    ) -> str | None:
        """Provision Ed25519 keypair + DID for the agent (Issue #1355)."""
        if not self._key_service:
            return None
        try:
            key_record = self._key_service.ensure_keypair(agent_id)
            agent_did = key_record.did
            agent["did"] = agent_did
            agent["key_id"] = key_record.key_id
            _logger.info(
                "[KYA] Provisioned identity for agent %s (did=%s)",
                agent_id,
                agent_did,
            )
            return cast(str | None, agent_did)
        except Exception as kya_err:
            _logger.warning(
                "[KYA] Failed to provision identity for agent %s: %s",
                agent_id,
                kya_err,
            )
            return None

    def _provision_agent_wallet(
        self,
        agent_id: str,
        zone_id: str,
        _logger: logging.Logger,
    ) -> None:
        """Auto-provision a TigerBeetle wallet for the agent (Issue #1210)."""
        if self._wallet_provisioner is None:
            return
        try:
            self._wallet_provisioner(agent_id, zone_id)
            _logger.info("[WALLET] Provisioned wallet for agent %s", agent_id)
        except Exception as wallet_err:
            _logger.warning(
                "[WALLET] Failed to provision wallet for agent %s: %s",
                agent_id,
                wallet_err,
            )

    def _grant_agent_self_permission(
        self,
        agent_id: str,
        agent_dir: str,
        zone_id: str,
        _context: dict | None,
        _logger: logging.Logger,
    ) -> None:
        """Grant the agent viewer permission on its own config directory."""
        if not self._rebac_manager:
            return
        try:
            self._rebac_manager.rebac_write(
                subject=("agent", agent_id),
                relation="viewer",
                object=("file", agent_dir),
                zone_id=zone_id,
            )
            _logger.info("Granted viewer permission to agent %s on %s", agent_id, agent_dir)
        except Exception as e:
            _logger.warning("Failed to grant viewer permission to agent: %s", e)

    def _write_agent_identity_document(
        self,
        agent_id: str,
        agent_did: str,
        agent_dir: str,
        context: dict | None,
        _logger: logging.Logger,
    ) -> None:
        """Write public DID document to the agent's .identity namespace (Issue #1355)."""
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
            ctx = _parse_context(context)
            self._fs.sys_mkdir(identity_dir, parents=True, exist_ok=True, context=ctx)
            self._fs.sys_write(
                f"{identity_dir}/did.json",
                json.dumps(did_doc, indent=2),
                context=ctx,
            )
            _logger.info("[KYA] Wrote DID document to %s/did.json", identity_dir)
        except Exception as did_err:
            _logger.warning("[KYA] Failed to write DID document: %s", did_err)

    def _provision_agent_api_key(
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
        """Generate an API key for the agent and update its config.yaml."""
        try:
            raw_key = self._create_agent_api_key(
                agent_id=agent_id,
                user_id=user_id,
                context=context,
            )
            agent["api_key"] = raw_key
            agent["has_api_key"] = True

            try:
                updated_config_data = self._create_agent_config_data(
                    agent_id=agent_id,
                    name=name,
                    user_id=user_id,
                    description=description,
                    created_at=agent.get("created_at"),
                    metadata=metadata,
                    api_key=raw_key,
                )
                self._write_agent_config(config_path, updated_config_data, context)
            except Exception as e:
                _logger.warning("Failed to update config with API key: %s", e)
        except Exception as e:
            _logger.error("Failed to create API key for agent: %s", e)
            raise

    # ------------------------------------------------------------------
    # Public @rpc_expose methods (8)
    # ------------------------------------------------------------------

    @rpc_expose(description="Register an AI agent")
    def register_agent(
        self,
        agent_id: str,
        name: str,
        description: str | None = None,
        generate_api_key: bool = False,
        metadata: dict | None = None,
        capabilities: list[str] | None = None,
        context: dict | None = None,
    ) -> dict:
        """Register an AI agent (v0.5.0).

        Agents are persistent identities owned by users. They do NOT have session_id
        or expiry - they live forever until explicitly deleted.

        Agents operate with zero permissions by default (principle of least privilege).
        Permissions must be explicitly granted via ReBAC (rebac_create).
        """
        user_id = _extract_user_id(context)
        if not user_id:
            raise ValueError("user_id required in context to register agent")

        zone_id = _extract_zone_id(context) or ROOT_ZONE_ID

        agent_name_part = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        agent_dir = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"

        self._check_agent_not_exists(agent_id, user_id, zone_id)
        self._ensure_agent_registry()
        assert self._agent_registry is not None

        record = self._agent_registry.register(
            agent_id=agent_id,
            owner_id=user_id,
            zone_id=zone_id,
            name=name,
            metadata=metadata,
            capabilities=capabilities,
        )
        agent = record.to_dict()

        agent_did = self._provision_agent_identity(agent_id, agent, logger)
        self._provision_agent_wallet(agent_id, zone_id, logger)

        config_path = f"{agent_dir}/config.yaml"
        config_data = self._create_agent_config_data(
            agent_id=agent_id,
            name=name,
            user_id=user_id,
            description=description,
            created_at=agent.get("created_at"),
            metadata=metadata,
        )
        self._create_agent_directory(
            agent_id=agent_id,
            user_id=user_id,
            agent_dir=agent_dir,
            config_path=config_path,
            config_data=config_data,
            context=context,
        )
        agent["config_path"] = config_path

        self._grant_agent_self_permission(agent_id, agent_dir, zone_id, context, logger)

        if agent_did:
            self._write_agent_identity_document(agent_id, agent_did, agent_dir, context, logger)

        if generate_api_key:
            self._provision_agent_api_key(
                agent_id=agent_id,
                user_id=user_id,
                name=name,
                description=description,
                metadata=metadata,
                agent=agent,
                config_path=config_path,
                context=context,
                _logger=logger,
            )
        else:
            agent["has_api_key"] = False

        if capabilities:
            agent["capabilities"] = list(capabilities)

        return cast(dict[Any, Any], agent)

    @rpc_expose(description="Update agent configuration")
    def update_agent(
        self,
        agent_id: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        """Update an existing agent's configuration (v0.5.1)."""
        import yaml

        user_id = _extract_user_id(context)
        if not user_id:
            raise ValueError("user_id required in context to update agent")

        zone_id = _extract_zone_id(context) or ROOT_ZONE_ID

        agent_name_part = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        agent_dir = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"
        config_path = f"{agent_dir}/config.yaml"

        try:
            existing_meta = self._fs.metadata.get(config_path)
            if not existing_meta:
                raise ValueError(f"Agent not found at {config_path}")
        except FileNotFoundError as e:
            raise ValueError(f"Agent not found: {agent_id}") from e

        ctx = _parse_context(context)
        existing_content = self._fs.sys_read(config_path, context=ctx)
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
        self._fs.sys_write(config_path, updated_yaml.encode("utf-8"), context=ctx)

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
                        logger.info("Updated entity registry metadata for agent %s", agent_id)
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
        """List all registered agents (v0.5.0)."""
        self._ensure_entity_registry()
        assert self._entity_registry is not None

        entities = self._entity_registry.get_entities_by_type("agent")
        result = []

        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        assert self._session_factory is not None
        session = self._session_factory()
        try:
            agent_keys_stmt = select(APIKeyModel).where(
                APIKeyModel.subject_type == "agent",
                APIKeyModel.revoked == 0,
            )
            agent_keys = {key.subject_id: key for key in session.scalars(agent_keys_stmt).all()}
        finally:
            session.close()

        for e in entities:
            entity_metadata: dict = {}
            if e.entity_metadata:
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    entity_metadata = json.loads(e.entity_metadata)

            agent_info: dict[str, Any] = {
                "agent_id": e.entity_id,
                "user_id": e.parent_id,
                "name": entity_metadata.get("name", e.entity_id),
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }

            if "description" in entity_metadata:
                agent_info["description"] = entity_metadata["description"]

            agent_key = agent_keys.get(e.entity_id)
            if agent_key:
                agent_info["has_api_key"] = True
                agent_info["inherit_permissions"] = bool(agent_key.inherit_permissions)
            else:
                agent_info["has_api_key"] = False
                inherit_perms = None
                try:
                    if "," in e.entity_id:
                        user_id, agent_name = e.entity_id.split(",", 1)
                        zone_id = _extract_zone_id(_context) or ROOT_ZONE_ID
                        config_path = (
                            f"/zone/{zone_id}/user/{user_id}/agent/{agent_name}/config.yaml"
                        )
                        try:
                            config_content = self._fs.sys_read(
                                config_path, context=_parse_context(_context)
                            )
                            import yaml

                            if isinstance(config_content, bytes):
                                config_data = yaml.safe_load(config_content.decode("utf-8"))
                                inherit_perms = config_data.get("inherit_permissions")
                        except Exception as exc:
                            logger.debug("Failed to read agent config at %s: %s", config_path, exc)
                except Exception as exc:
                    logger.debug("Failed to parse agent entity_id for config lookup: %s", exc)

                agent_info["inherit_permissions"] = (
                    bool(inherit_perms) if inherit_perms is not None else True
                )

            result.append(agent_info)

        return result

    @rpc_expose(description="Get agent information")
    def get_agent(self, agent_id: str, _context: dict | None = None) -> dict | None:
        """Get information about a registered agent (v0.5.0)."""
        self._ensure_entity_registry()
        assert self._entity_registry is not None

        entity = self._entity_registry.get_entity("agent", agent_id)
        if not entity:
            return None

        entity_metadata: dict = {}
        if entity.entity_metadata:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                entity_metadata = json.loads(entity.entity_metadata)

        agent_info: dict[str, Any] = {
            "agent_id": entity.entity_id,
            "user_id": entity.parent_id,
            "name": entity_metadata.get("name", entity.entity_id),
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
        }

        if "description" in entity_metadata:
            agent_info["description"] = entity_metadata["description"]

        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        assert self._session_factory is not None
        session = self._session_factory()
        try:
            agent_key_stmt = select(APIKeyModel).where(
                APIKeyModel.subject_type == "agent",
                APIKeyModel.subject_id == agent_id,
                APIKeyModel.revoked == 0,
            )
            agent_key = session.scalar(agent_key_stmt)

            if agent_key:
                agent_info["has_api_key"] = True
                agent_info["inherit_permissions"] = bool(agent_key.inherit_permissions)
                self._enrich_agent_from_config(agent_info, entity, _context)
            else:
                agent_info["has_api_key"] = False
                inherit_perms = self._enrich_agent_from_config(agent_info, entity, _context)
                agent_info["inherit_permissions"] = (
                    bool(inherit_perms) if inherit_perms is not None else True
                )
        finally:
            session.close()

        return agent_info

    def _enrich_agent_from_config(
        self,
        agent_info: dict[str, Any],
        entity: Any,
        _context: dict | None,
    ) -> Any:
        """Read config.yaml and enrich agent_info with config fields.

        Returns inherit_permissions value (or None) for callers that need it.
        """
        inherit_perms = None
        try:
            if "," in entity.entity_id:
                user_id, agent_name = entity.entity_id.split(",", 1)
                ctx = _parse_context(_context)
                zone_id = _extract_zone_id(_context) or ROOT_ZONE_ID
                config_path = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name}/config.yaml"
                try:
                    config_content = self._fs.sys_read(config_path, context=ctx)
                    import yaml

                    if isinstance(config_content, bytes):
                        config_data = yaml.safe_load(config_content.decode("utf-8"))

                        if config_data.get("api_key"):
                            agent_info["api_key"] = config_data["api_key"]

                        inherit_perms = config_data.get("inherit_permissions")

                        cfg_metadata = config_data.get("metadata", {})
                        if isinstance(cfg_metadata, dict):
                            if cfg_metadata.get("platform"):
                                agent_info["platform"] = cfg_metadata["platform"]
                            if cfg_metadata.get("endpoint_url"):
                                agent_info["endpoint_url"] = cfg_metadata["endpoint_url"]
                            if cfg_metadata.get("agent_id"):
                                agent_info["config_agent_id"] = cfg_metadata["agent_id"]

                        if not agent_info.get("platform") and config_data.get("platform"):
                            agent_info["platform"] = config_data["platform"]
                        if not agent_info.get("endpoint_url") and config_data.get("endpoint_url"):
                            agent_info["endpoint_url"] = config_data["endpoint_url"]
                        if (
                            not agent_info.get("config_agent_id")
                            and config_data.get("agent_id")
                            and config_data["agent_id"] != entity.entity_id
                        ):
                            agent_info["config_agent_id"] = config_data["agent_id"]

                        if config_data.get("system_prompt"):
                            agent_info["system_prompt"] = config_data["system_prompt"]
                        if config_data.get("tools"):
                            agent_info["tools"] = config_data["tools"]
                except Exception as exc:
                    logger.debug("Failed to read agent config for %s: %s", entity.entity_id, exc)
        except Exception as exc:
            logger.debug("Failed to parse agent_id %s for config lookup: %s", entity.entity_id, exc)
        return inherit_perms

    @rpc_expose(description="Delete an agent")
    def delete_agent(self, agent_id: str, _context: dict | None = None) -> bool:
        """Delete a registered agent (v0.5.0)."""
        # Ownership check: caller must own the agent or be admin
        ctx = _parse_context(_context)
        if "," in agent_id:
            owner_user_id = agent_id.split(",", 1)[0]
            if ctx.user_id and ctx.user_id != owner_user_id and not ctx.is_admin:
                raise NexusPermissionError(
                    f"Permission denied: only the agent owner or an admin can delete agent {agent_id}"
                )

        try:
            if "," in agent_id:
                user_id, agent_name_part = agent_id.split(",", 1)
                zone_id = _extract_zone_id(_context) or ROOT_ZONE_ID
                agent_dir = f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"

                try:
                    ctx = _parse_context(_context)
                    admin_ctx = OperationContext(
                        user_id=ctx.user_id,
                        groups=ctx.groups,
                        zone_id=ctx.zone_id,
                        agent_id=ctx.agent_id,
                        is_admin=True,
                        is_system=ctx.is_system,
                    )
                    if self._fs.sys_access(agent_dir, context=admin_ctx):
                        self._fs.sys_rmdir(agent_dir, recursive=True, context=admin_ctx)
                except Exception as e:
                    logger.warning("Failed to delete agent directory %s: %s", agent_dir, e)

                assert self._session_factory is not None
                session = self._session_factory()
                try:
                    from sqlalchemy import update

                    from nexus.storage.models import APIKeyModel

                    stmt = (
                        update(APIKeyModel)
                        .where(
                            APIKeyModel.subject_type == "agent",
                            APIKeyModel.subject_id == agent_id,
                            APIKeyModel.revoked == 0,
                        )
                        .values(revoked=1)
                    )
                    result = session.execute(stmt)
                    session.commit()

                    rowcount = result.rowcount if hasattr(result, "rowcount") else 0
                    if rowcount > 0:
                        logger.info("Revoked %d API key(s) for agent %s", rowcount, agent_id)
                except Exception as e:
                    logger.warning("Failed to revoke API keys for agent %s: %s", agent_id, e)
                    session.rollback()
                finally:
                    session.close()

                if self._rebac_manager:
                    try:
                        tuples = self._rebac_manager.rebac_list_tuples(
                            subject=("agent", agent_id),
                        )

                        deleted_count = 0
                        for tuple_data in tuples:
                            try:
                                tuple_id = tuple_data.get("tuple_id")
                                if tuple_id:
                                    self._rebac_manager.rebac_delete(tuple_id=tuple_id)
                                    deleted_count += 1
                            except Exception as e:
                                logger.warning("Failed to delete ReBAC tuple: %s", e)

                        if deleted_count > 0:
                            logger.info(
                                "Deleted %d ReBAC tuple(s) for agent %s", deleted_count, agent_id
                            )
                    except Exception as e:
                        logger.warning(
                            "Failed to delete ReBAC tuples for agent %s: %s", agent_id, e
                        )

                    try:
                        user_tuples = self._rebac_manager.rebac_list_tuples(
                            subject=("user", user_id),
                            object=("file", agent_dir),
                        )
                        for tuple_data in user_tuples:
                            tuple_id = tuple_data.get("tuple_id")
                            if tuple_id:
                                try:
                                    self._rebac_manager.rebac_delete(tuple_id=tuple_id)
                                except Exception as e:
                                    logger.warning("Failed to delete user permission tuple: %s", e)
                    except Exception as e:
                        logger.warning(
                            "Failed to revoke user permissions for agent directory: %s", e
                        )
        except Exception as e:
            logger.warning("Failed to cleanup agent resources: %s", e)

        if self._wallet_provisioner is not None:
            zone_id_for_wallet = _extract_zone_id(_context) or ROOT_ZONE_ID
            try:
                cleanup_fn = getattr(self._wallet_provisioner, "cleanup", None)
                if cleanup_fn is not None:
                    cleanup_fn(agent_id, zone_id_for_wallet)
                    logger.info("[WALLET] Cleaned up wallet for agent %s", agent_id)
                else:
                    logger.debug(
                        "[WALLET] No cleanup handler for agent %s wallet "
                        "(TigerBeetle accounts are immutable)",
                        agent_id,
                    )
            except Exception as wallet_err:
                logger.warning(
                    "[WALLET] Failed to cleanup wallet for agent %s: %s", agent_id, wallet_err
                )

        self._ensure_agent_registry()
        assert self._agent_registry is not None
        deleted = self._agent_registry.unregister(agent_id)
        return cast(bool, deleted)

    # ===== Agent Lifecycle API (Issue #1240) =====

    @rpc_expose(description="Transition agent lifecycle state")
    def agent_transition(
        self,
        agent_id: str,
        target_state: str,
        expected_generation: int | None = None,
        _context: dict | None = None,
    ) -> dict:
        """Transition an agent's lifecycle state with optimistic locking."""
        if not self._agent_registry:
            raise ValueError("AgentRegistry not available")

        from nexus.contracts.agent_types import AgentState

        try:
            target = AgentState(target_state)
        except ValueError as err:
            raise ValueError(
                f"Invalid target state '{target_state}'. Valid states: CONNECTED, IDLE, SUSPENDED"
            ) from err

        record = self._agent_registry.transition(
            agent_id=agent_id,
            target_state=target,
            expected_generation=expected_generation,
        )
        return {
            "agent_id": record.agent_id,
            "state": record.state.value,
            "generation": record.generation,
        }

    @rpc_expose(description="Record agent heartbeat")
    def agent_heartbeat(
        self,
        agent_id: str,
        _context: dict | None = None,
    ) -> dict:
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
        _context: dict | None = None,
    ) -> list[dict]:
        """List agents in a zone, optionally filtered by state."""
        if not self._agent_registry:
            raise ValueError("AgentRegistry not available")

        state_enum = None
        if state:
            from nexus.contracts.agent_types import AgentState

            try:
                state_enum = AgentState(state)
            except ValueError as err:
                raise ValueError(f"Invalid state filter '{state}'") from err

        records = self._agent_registry.list_by_zone(zone_id, state=state_enum)
        return [
            {
                "agent_id": r.agent_id,
                "owner_id": r.owner_id,
                "zone_id": r.zone_id,
                "name": r.name,
                "state": r.state.value,
                "generation": r.generation,
                "last_heartbeat": r.last_heartbeat.isoformat() if r.last_heartbeat else None,
                "created_at": r.created_at.isoformat(),
                "updated_at": r.updated_at.isoformat(),
            }
            for r in records
        ]


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def create_agent_service(nx: Any) -> AgentService | None:
    """Create AgentService for server-layer RPC dispatch (Task #634).

    This is a **server-layer** factory function — the kernel (NexusFS) has
    zero knowledge of AgentService. The server calls this once during
    ``create_app()`` and registers the result as an additional RPC source.

    Args:
        nx: A NexusFS instance (used to extract dependencies).

    Returns:
        AgentService instance, or None if critical dependencies are unavailable.
    """
    record_store = getattr(nx, "_record_store", None)
    session_factory = getattr(nx, "SessionLocal", None)

    if session_factory is None and record_store is None:
        logger.debug("AgentService unavailable: no SessionLocal or record_store")
        return None

    return AgentService(
        fs=nx,
        agent_registry=getattr(nx, "_agent_registry", None),
        session_factory=session_factory,
        entity_registry=getattr(nx, "_entity_registry", None),
        rebac_manager=getattr(nx, "_rebac_manager", None),
        key_service=getattr(nx, "_key_service", None),
        wallet_provisioner=getattr(nx, "_wallet_provisioner", None),
        api_key_creator=getattr(nx, "_api_key_creator", None),
        record_store=record_store,
    )
