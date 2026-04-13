"""User Provisioning Service — replaces NexusFS provision/deprovision facades.

Handles user lifecycle: provision_user creates all resources (user record,
zone, directories, workspace, agents, API key, ReBAC permissions).
deprovision_user tears everything down.

Issue #2033 — Phase 2.2 of LEGO microkernel decomposition.
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.rpc import rpc_expose
from nexus.contracts.types import OperationContext, VFSOperations

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


class UserProvisioningService:
    """RPC surface for user provisioning and deprovisioning.

    Replaces ~950 LOC of facades in NexusFS (provision_user, deprovision_user,
    _delete_directory_recursive, _create_user_directories).
    """

    def __init__(
        self,
        *,
        vfs: VFSOperations,
        session_factory: Any,
        entity_registry: Any | None = None,
        api_key_creator: Any | None = None,
        backend: Any | None = None,
        rebac_manager: Any | None = None,
        # Callables for NexusFS facade methods
        rmdir_fn: Any | None = None,
        rebac_create_fn: Any | None = None,
        rebac_delete_fn: Any | None = None,
        register_workspace_fn: Any | None = None,
        register_agent_fn: Any | None = None,
        # Cache references for invalidation during directory deletion
        list_cache: Any | None = None,
        exists_cache: Any | None = None,
    ) -> None:
        self._vfs = vfs
        self._session_factory = session_factory
        self._entity_registry = entity_registry
        self._api_key_creator = api_key_creator
        self._backend = backend
        self._rebac_manager = rebac_manager
        self._rmdir_fn = rmdir_fn
        self._rebac_create_fn = rebac_create_fn
        self._rebac_delete_fn = rebac_delete_fn
        self._register_workspace_fn = register_workspace_fn
        self._register_agent_fn = register_agent_fn
        self._list_cache = list_cache
        self._exists_cache = exists_cache

    def _ensure_entity_registry(self) -> None:
        if self._entity_registry is None:
            raise RuntimeError("EntityRegistry not available")

    # ------------------------------------------------------------------
    # Public RPC Methods
    # ------------------------------------------------------------------

    @rpc_expose(description="Provision a new user account with all resources", admin_only=True)
    async def provision_user(
        self,
        user_id: str,
        email: str,
        display_name: str | None = None,
        zone_id: str | None = None,
        zone_name: str | None = None,
        create_api_key: bool = True,
        api_key_name: str | None = None,
        api_key_expires_at: "datetime | None" = None,
        create_agents: bool = True,
        import_skills: bool = False,  # noqa: ARG002
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Provision a new user with all default resources (Issue #820)."""
        from datetime import UTC, datetime

        if not user_id:
            raise ValueError("user_id is required")
        if not email or "@" not in email:
            raise ValueError("Valid email required")

        if not zone_id:
            zone_id = email.split("@")[0]
            if not zone_id:
                raise ValueError("Could not extract zone_id from email")

        logger.info("Provisioning user %s (email=%s, zone=%s)", user_id, email, zone_id)

        admin_context = context or OperationContext(
            user_id=user_id,
            groups=[],
            zone_id=zone_id,
            is_admin=True,
        )

        created_resources: dict[str, Any] = {
            "user": False,
            "zone": False,
            "directories": [],
            "workspace": None,
            "agents": [],
        }

        self._ensure_entity_registry()

        session = self._session_factory()
        api_key = None
        key_id = None

        try:
            from sqlalchemy import select as sa_select

            from nexus.storage.models import UserModel, ZoneModel

            # 1. Create/update ZoneModel
            zone = (
                session.execute(sa_select(ZoneModel).filter_by(zone_id=zone_id)).scalars().first()
            )
            if not zone:
                zone = ZoneModel(
                    zone_id=zone_id,
                    name=zone_name or f"{zone_id} Organization",
                    phase="Active",
                    finalizers="[]",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                session.add(zone)
                session.commit()
                logger.info("Created zone: %s", zone_id)
                created_resources["zone"] = True

            # 2. Register zone in entity registry
            assert self._entity_registry is not None
            if not self._entity_registry.get_entity("zone", zone_id):
                self._entity_registry.register_entity("zone", zone_id)

            # 3. Create/update UserModel
            user = (
                session.execute(sa_select(UserModel).filter_by(user_id=user_id)).scalars().first()
            )
            if user:
                if not user.is_active:
                    user.is_active = 1
                    user.deleted_at = None
                    session.commit()
                    logger.info("Reactivated soft-deleted user: %s", user_id)
            else:
                user = UserModel(
                    user_id=user_id,
                    email=email,
                    username=user_id,
                    display_name=display_name or user_id,
                    zone_id=zone_id,
                    primary_auth_method="api_key",
                    is_active=1,
                    is_global_admin=0,
                    email_verified=1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                session.add(user)
                session.commit()
                logger.info("Created user: %s", user_id)
                created_resources["user"] = True

            # 4. Register user in entity registry
            assert self._entity_registry is not None
            if not self._entity_registry.get_entity("user", user_id):
                self._entity_registry.register_entity(
                    "user",
                    user_id,
                    parent_type="zone",
                    parent_id=zone_id,
                )

            admin_context.user_id = user_id

            # 5. Create API key
            if create_api_key:
                from sqlalchemy import select

                from nexus.storage.models import APIKeyModel

                if self._api_key_creator is None:
                    raise RuntimeError(
                        "API key creator not injected. "
                        "Use factory.create_nexus_services() to wire auth services."
                    )
                user_row = session.execute(
                    select(UserModel).where(UserModel.user_id == user_id).with_for_update()
                ).scalar_one_or_none()
                if not user_row:
                    raise ValueError(f"User not found: {user_id}")

                existing_key = session.scalar(
                    select(APIKeyModel)
                    .where(
                        APIKeyModel.user_id == user_id,
                        APIKeyModel.subject_type == "user",
                        APIKeyModel.revoked == 0,
                    )
                    .limit(1)
                )
                if not existing_key:
                    key_name = api_key_name or f"Primary key for {email}"
                    key_id, api_key = self._api_key_creator.create_key(
                        session,
                        user_id=user_id,
                        name=key_name,
                        zone_id=zone_id,
                        is_admin=False,
                        expires_at=api_key_expires_at,
                    )
                    session.commit()
                    logger.info("Created API key for user: %s", user_id)

        except Exception as e:
            logger.error("Database operation failed during provisioning: %s", e)
            session.rollback()
            raise
        finally:
            session.close()

        # 6. Create user directories
        try:
            dir_paths = await self._create_user_directories(user_id, zone_id, admin_context)
            created_resources["directories"] = dir_paths
            logger.info("Created %d directories for user %s", len(dir_paths), user_id)
        except Exception as e:
            logger.error("Failed to create user directories: %s", e)

        # 7. Create default workspace
        workspace_path = None
        try:
            import uuid

            uuid_suffix = str(uuid.uuid4()).replace("-", "")[:12]
            workspace_id = f"ws_personal_{uuid_suffix}"
            workspace_path = f"/zone/{zone_id}/user/{user_id}/workspace/{workspace_id}"

            if not self._vfs.access(workspace_path, context=admin_context):
                self._vfs.mkdir(workspace_path, parents=True, exist_ok=True, context=admin_context)
                if self._register_workspace_fn:
                    self._register_workspace_fn(
                        workspace_path,
                        name="Personal Workspace",
                        description="Default personal workspace",
                        context=admin_context,
                    )
                logger.info("Created workspace: %s", workspace_path)
            created_resources["workspace"] = workspace_path
        except Exception as e:
            logger.error("Failed to create workspace: %s", e)

        # 8. Create agents
        agent_paths: list[str] = []
        if create_agents:
            try:
                from nexus.services.agents.agent_provisioning import create_standard_agents

                agent_results = create_standard_agents(self._vfs, user_id, admin_context)
                for agent_name, agent_result in agent_results.items():
                    if agent_result and "config_path" in agent_result:
                        agent_paths.append(agent_result["config_path"])
                        created_resources["agents"].append(agent_name)
                logger.info("Created %d agents for user %s", len(agent_paths), user_id)
            except Exception as e:
                logger.error("Failed to create agents: %s", e)

        # 9. Grant ReBAC permissions (zone owner)
        if self._rebac_create_fn:
            try:
                self._rebac_create_fn(
                    subject=("user", user_id),
                    relation="member",
                    object=("group", f"zone_owners:{zone_id}"),
                    zone_id=zone_id,
                    context=admin_context,
                )
                logger.info("Granted zone owner permissions to user %s", user_id)
            except Exception as e:
                if "already exists" in str(e).lower():
                    logger.debug("User already has zone owner permissions: %s", user_id)
                else:
                    logger.warning("Failed to grant zone owner permissions: %s", e)

        logger.info("Successfully provisioned user %s", user_id)
        return {
            "user_id": user_id,
            "zone_id": zone_id,
            "api_key": api_key,
            "key_id": key_id,
            "workspace_path": workspace_path,
            "agent_paths": agent_paths,
            "created_resources": created_resources,
        }

    @rpc_expose(description="Deprovision a user and remove all their resources", admin_only=True)
    def deprovision_user(
        self,
        user_id: str,
        zone_id: str | None = None,
        delete_user_record: bool = False,
        force: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Deprovision a user and remove all their resources."""
        from datetime import UTC, datetime

        if not user_id:
            raise ValueError("user_id is required")

        logger.info("Deprovisioning user %s", user_id)

        admin_context = context or OperationContext(
            user_id="system",
            groups=[],
            zone_id=zone_id or "system",
            is_admin=True,
        )

        result: dict[str, Any] = {
            "user_id": user_id,
            "zone_id": None,
            "deleted_directories": [],
            "deleted_api_keys": 0,
            "deleted_oauth_api_keys": 0,
            "deleted_oauth_accounts": 0,
            "deleted_permissions": 0,
            "deleted_entities": 0,
            "user_record_deleted": False,
        }

        session = self._session_factory()
        try:
            from sqlalchemy import select as sa_select

            from nexus.storage.models import UserModel

            user = (
                session.execute(sa_select(UserModel).filter_by(user_id=user_id)).scalars().first()
            )

            if not user:
                logger.warning("User not found in database: %s", user_id)
            else:
                if not zone_id:
                    zone_id = user.zone_id
                result["zone_id"] = zone_id

                if user.is_global_admin and not force:
                    raise ValueError(
                        f"Cannot deprovision global admin user {user_id}. "
                        "Use force=True to override."
                    )

            if zone_id:
                admin_context = OperationContext(
                    user_id="system",
                    groups=[],
                    zone_id=zone_id,
                    is_admin=True,
                )

            # 1. Delete user directories
            if zone_id:
                user_base_path = f"/zone/{zone_id}/user/{user_id}"
                for resource_type in [
                    "workspace",
                    "memory",
                    "agent",
                    "connector",
                    "resource",
                ]:
                    dir_path = f"{user_base_path}/{resource_type}"
                    try:
                        was_deleted = self._delete_directory_recursive(dir_path, admin_context)
                        if was_deleted:
                            result["deleted_directories"].append(dir_path)
                    except Exception as e:
                        logger.warning("Failed to delete directory %s: %s", dir_path, e)

            # 2. Delete API keys
            try:
                from sqlalchemy import delete as sa_delete

                from nexus.storage.models import APIKeyModel

                del_result: Any = session.execute(sa_delete(APIKeyModel).filter_by(user_id=user_id))
                deleted_keys = del_result.rowcount
                session.commit()
                result["deleted_api_keys"] = deleted_keys
                logger.info("Deleted %d API keys for user %s", deleted_keys, user_id)
            except Exception as e:
                logger.warning("Failed to delete API keys: %s", e)
                session.rollback()

            # 3. Delete OAuth records
            try:
                from sqlalchemy import inspect

                from nexus.storage.models import OAuthAPIKeyModel, UserOAuthAccountModel

                has_oauth_tables = False
                if session.bind is not None:
                    inspector = inspect(session.bind)
                    table_names = inspector.get_table_names()
                    has_oauth_tables = (
                        "oauth_api_keys" in table_names and "user_oauth_accounts" in table_names
                    )

                if has_oauth_tables:
                    from sqlalchemy import delete as sa_delete

                    oauth_key_result: Any = session.execute(
                        sa_delete(OAuthAPIKeyModel).filter_by(user_id=user_id)
                    )
                    result["deleted_oauth_api_keys"] = oauth_key_result.rowcount

                    oauth_acct_result: Any = session.execute(
                        sa_delete(UserOAuthAccountModel).filter_by(user_id=user_id)
                    )
                    session.commit()
                    result["deleted_oauth_accounts"] = oauth_acct_result.rowcount
            except Exception as e:
                logger.warning("Failed to delete OAuth records: %s", e)
                session.rollback()

            # 4. Delete ReBAC permissions
            try:
                if self._rebac_manager:
                    tuples = self._rebac_manager.query_tuples_by_subject(("user", user_id))
                    deleted_count = 0
                    for tuple_info in tuples:
                        tuple_id = tuple_info.get("tuple_id")
                        if tuple_id and self._rebac_delete_fn:
                            try:
                                self._rebac_delete_fn(tuple_id)
                                deleted_count += 1
                            except Exception as exc:
                                logger.warning("Failed to delete ReBAC tuple %s: %s", tuple_id, exc)
                    result["deleted_permissions"] = deleted_count
            except Exception as e:
                logger.warning("Failed to delete ReBAC permissions: %s", e)

            # 5. Delete entity registry entries
            try:
                if self._entity_registry:
                    user_entity = self._entity_registry.get_entity("user", user_id)
                    if user_entity:
                        deleted = self._entity_registry.delete_entity(
                            "user",
                            user_id,
                            cascade=True,
                        )
                        if deleted:
                            result["deleted_entities"] = 1
            except Exception as e:
                logger.warning("Failed to delete entity registry entries: %s", e)

            # 6. Soft-delete user record
            if delete_user_record and user:
                try:
                    user.is_active = 0
                    user.deleted_at = datetime.now(UTC)
                    session.commit()
                    result["user_record_deleted"] = True
                except Exception as e:
                    logger.warning("Failed to soft-delete user record: %s", e)
                    session.rollback()

        except Exception as e:
            logger.error("Error during user deprovisioning: %s", e)
            session.rollback()
            raise
        finally:
            session.close()

        logger.info(
            "Successfully deprovisioned user %s: dirs=%d, keys=%d, perms=%d, entities=%d",
            user_id,
            len(result["deleted_directories"]),
            result["deleted_api_keys"],
            result["deleted_permissions"],
            result["deleted_entities"],
        )
        return result

    # ------------------------------------------------------------------
    # Private Helpers
    # ------------------------------------------------------------------

    async def _delete_directory_recursive(
        self,
        dir_path: str,
        context: OperationContext,
    ) -> bool:
        """Recursively delete a directory and all its contents."""
        directory_removed = False
        had_content = False

        # Approach 1: Backend rmdir
        if self._backend:
            try:
                response = self._backend.rmdir(dir_path, recursive=True, context=context)
                if response.success:
                    directory_removed = True
                    had_content = True
                    logger.info("Deleted directory via backend: %s", dir_path)
            except Exception as e:
                logger.debug("Backend rmdir failed for %s: %s", dir_path, e)

        if directory_removed:
            self._cleanup_after_directory_delete(dir_path)
            return True

        # Approach 2: Virtual filesystem deletion (fallback)
        from contextlib import suppress

        directory_exists = False
        with suppress(Exception):
            directory_exists = self._vfs.access(dir_path, context=context)

        if not directory_exists:
            return False

        try:
            result = self._vfs.sys_readdir(dir_path, recursive=False, context=context)
            if isinstance(result, dict) and "files" in result:
                children = result["files"]
            elif isinstance(result, list):
                children = result
            else:
                children = []

            if children:
                had_content = True

            for item in children:
                child_path: str | None = None
                is_dir = False

                if isinstance(item, str):
                    child_path = item
                    try:
                        self._vfs.sys_readdir(child_path, recursive=False, context=context)
                        is_dir = True
                    except Exception:
                        is_dir = False  # readdir failed → treat as file
                elif isinstance(item, dict):
                    child_path = item.get("path")
                    if not child_path:
                        continue
                    is_dir = item.get("type", "") == "directory"

                if not child_path or child_path == dir_path:
                    continue

                try:
                    if is_dir:
                        await self._delete_directory_recursive(child_path, context)
                    else:
                        self._vfs.sys_unlink(child_path, context=context)
                except Exception as e:
                    logger.warning("Failed to delete %s: %s", child_path, e)

            # Remove directory itself
            if self._rmdir_fn:
                try:
                    self._rmdir_fn(dir_path, context=context)
                    directory_removed = True
                except Exception as e:
                    logger.debug("rmdir failed for %s (will try unlink): %s", dir_path, e)
            if not directory_removed:
                try:
                    self._vfs.sys_unlink(dir_path, context=context)
                    directory_removed = True
                except Exception as e:
                    logger.debug("unlink fallback also failed for %s: %s", dir_path, e)

        except Exception as e:
            logger.error("Virtual filesystem deletion failed for %s: %s", dir_path, e)

        if not directory_removed:
            logger.warning("Could not remove directory %s", dir_path)

        return had_content or directory_removed

    def _cleanup_after_directory_delete(self, dir_path: str) -> None:
        """Clean up metadata, ReBAC tuples, and caches after directory deletion."""
        # Clean up file paths
        try:
            session = self._session_factory()
            try:
                from sqlalchemy import delete as sa_delete

                from nexus.storage.models import FilePathModel

                fp_result: Any = session.execute(
                    sa_delete(FilePathModel).where(FilePathModel.virtual_path.like(f"{dir_path}%"))
                )
                session.commit()
                logger.debug("Deleted %d file path entries for %s", fp_result.rowcount, dir_path)
            finally:
                session.close()
        except Exception as e:
            logger.warning("Failed to clean up file paths for %s: %s", dir_path, e)

        # Clean up ReBAC tuples
        if self._rebac_manager:
            try:
                session = self._session_factory()
                try:
                    from sqlalchemy import delete as sa_delete

                    from nexus.storage.models import ReBACTupleModel

                    rebac_result: Any = session.execute(
                        sa_delete(ReBACTupleModel).where(
                            ReBACTupleModel.object_type == "file",
                            ReBACTupleModel.object_id.like(f"{dir_path}%"),
                        )
                    )
                    session.commit()
                    logger.debug("Deleted %d ReBAC tuples for %s", rebac_result.rowcount, dir_path)
                finally:
                    session.close()
            except Exception as e:
                logger.warning("Failed to clean up ReBAC tuples for %s: %s", dir_path, e)

        # Invalidate caches
        try:
            parent_path = "/".join(dir_path.rstrip("/").split("/")[:-1])
            if self._list_cache is not None:
                self._list_cache.pop(parent_path, None)
                self._list_cache.pop(dir_path, None)
            if self._exists_cache is not None:
                self._exists_cache.pop(dir_path, None)
                if parent_path:
                    self._exists_cache.pop(parent_path, None)
        except Exception as exc:
            logger.debug("Failed to invalidate caches for %s: %s", dir_path, exc)

        # Clear tiger cache
        if self._rebac_manager and hasattr(self._rebac_manager, "_tiger_cache"):
            try:
                tiger_cache = self._rebac_manager._tiger_cache
                if hasattr(tiger_cache, "invalidate_all"):
                    tiger_cache.invalidate_all()
            except Exception as e:
                logger.debug("Tiger cache invalidation failed for %s: %s", dir_path, e)

    async def _create_user_directories(
        self,
        user_id: str,
        zone_id: str,
        context: OperationContext,
    ) -> list[str]:
        """Create all user directories with proper permissions."""
        all_types = ["workspace", "memory", "agent", "connector", "resource"]
        created_paths: list[str] = []

        for resource_type in all_types:
            folder_path = f"/zone/{zone_id}/user/{user_id}/{resource_type}"
            try:
                self._vfs.mkdir(folder_path, parents=True, exist_ok=True, context=context)
                if self._rebac_create_fn:
                    try:
                        self._rebac_create_fn(
                            subject=("user", user_id),
                            relation="direct_owner",
                            object=("file", folder_path),
                            zone_id=zone_id,
                            context=context,
                        )
                    except Exception as e:
                        if "already exists" not in str(e).lower():
                            raise
                created_paths.append(folder_path)
            except Exception as e:
                logger.warning("Failed to create directory %s: %s", folder_path, e)

        return created_paths
