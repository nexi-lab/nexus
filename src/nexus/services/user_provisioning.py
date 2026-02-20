"""User Provisioning Service — extracted from NexusFS kernel (Issue #635).

Handles user lifecycle operations: provisioning (creating user records, zones,
directories, workspaces, agents, skills, API keys, permissions) and
deprovisioning (removing all user resources).

These are **service-layer** operations, not kernel operations. Per
KERNEL-ARCHITECTURE.md §3: "NexusFS contains no service business logic."
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from nexus.contracts.types import OperationContext
from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)


class UserProvisioningService:
    """Service for user provisioning and deprovisioning.

    Receives NexusFS kernel and its internal services via constructor DI.
    The kernel has zero knowledge of this service.
    """

    def __init__(self, *, nx: Any) -> None:
        self._nx = nx

    # ------------------------------------------------------------------
    # RPC-exposed methods
    # ------------------------------------------------------------------

    @rpc_expose(description="Provision a new user account with all resources")
    def provision_user(
        self,
        user_id: str,
        email: str,
        display_name: str | None = None,
        zone_id: str | None = None,
        zone_name: str | None = None,
        create_api_key: bool = True,
        api_key_name: str | None = None,
        api_key_expires_at: datetime | None = None,
        create_agents: bool = True,
        import_skills: bool = True,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Provision a new user with all default resources (Issue #820).

        Creates:
        - User record (UserModel) in database
        - Zone record (ZoneModel) if it doesn't exist
        - All user directories under /zone/{zone_id}/user/{user_id}/
        - Default workspace
        - Default agents (ImpersonatedUser, UntrustedAgent)
        - Default skills (all from data/skills/)
        - API key (if create_api_key=True)
        - ReBAC permissions (user as zone owner)
        - Entity registry entries

        Args:
            user_id: Unique user identifier
            email: User email address
            display_name: Optional display name
            zone_id: Zone ID (extracted from email if not provided)
            zone_name: Optional custom zone name (default: "{zone_id} Organization")
            create_api_key: Whether to create API key for user
            api_key_name: Optional custom name for API key
            api_key_expires_at: Optional expiry datetime for API key
            create_agents: Whether to create default agents
            import_skills: Whether to import default skills
            context: Operation context

        Returns:
            Dict with user_id, zone_id, api_key, key_id, workspace_path,
            agent_paths, skill_paths, created_resources.
        """
        from datetime import UTC
        from datetime import datetime as dt_cls

        # Input validation
        if not user_id:
            raise ValueError("user_id is required")
        if not email or "@" not in email:
            raise ValueError("Valid email required")

        # Extract zone_id from email if not provided
        if not zone_id:
            zone_id = email.split("@")[0]
            if not zone_id:
                raise ValueError("Could not extract zone_id from email")

        logger.info("Provisioning user %s (email=%s, zone=%s)", user_id, email, zone_id)

        # Use admin context for provisioning
        admin_context = context or OperationContext(
            user_id=user_id,
            groups=[],
            zone_id=zone_id,
            is_admin=True,
        )

        # Track created resources
        created_resources: dict[str, Any] = {
            "user": False,
            "zone": False,
            "directories": [],
            "workspace": None,
            "agents": [],
            "skills": [],
        }

        nx = self._nx

        # Initialize entity registry
        nx._ensure_entity_registry()

        session = nx.SessionLocal()
        api_key = None
        key_id = None

        try:
            # 1. Create/update ZoneModel (idempotent)
            from sqlalchemy import select as sa_select

            from nexus.storage.models import UserModel, ZoneModel

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
            else:
                logger.debug("Zone already exists: %s", zone_id)

            # 2. Register zone in entity registry (idempotent)
            if not nx._entity_registry.get_entity("zone", zone_id):
                nx._entity_registry.register_entity("zone", zone_id)
                logger.info("Registered zone in entity registry: %s", zone_id)

            # 3. Create/update UserModel (idempotent)
            user = (
                session.execute(sa_select(UserModel).filter_by(user_id=user_id)).scalars().first()
            )
            if user:
                logger.debug("User already exists: %s", user_id)
                # Reactivate if soft-deleted
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
                    created_at=dt_cls.now(UTC),
                    updated_at=dt_cls.now(UTC),
                )
                session.add(user)
                session.commit()
                logger.info("Created user: %s", user_id)
                created_resources["user"] = True

            # 4. Register user in entity registry (idempotent)
            if not nx._entity_registry.get_entity("user", user_id):
                nx._entity_registry.register_entity(
                    "user", user_id, parent_type="zone", parent_id=zone_id
                )
                logger.info("Registered user in entity registry: %s", user_id)

            admin_context.user_id = user_id
            # 5. Create API key (if requested and doesn't exist)
            if create_api_key:
                from sqlalchemy import select

                from nexus.storage.models import APIKeyModel

                api_key_creator = getattr(nx, "_api_key_creator", None)
                if api_key_creator is None:
                    raise RuntimeError(
                        "API key creator not injected. "
                        "Use factory.create_nexus_services() to wire auth services."
                    )

                # Lock the user row to prevent race conditions
                user_row = session.execute(
                    select(UserModel).where(UserModel.user_id == user_id).with_for_update()
                ).scalar_one_or_none()

                if not user_row:
                    raise ValueError(f"User not found: {user_id}")

                # Check if user already has an API key
                existing_key_stmt = (
                    select(APIKeyModel)
                    .where(
                        APIKeyModel.user_id == user_id,
                        APIKeyModel.subject_type == "user",
                        APIKeyModel.revoked == 0,
                    )
                    .limit(1)
                )
                existing_key = session.scalar(existing_key_stmt)

                if not existing_key:
                    key_name = api_key_name or f"Primary key for {email}"

                    # Issue #1519, 3A: uses injected protocol
                    key_id, api_key = api_key_creator.create_key(
                        session,
                        user_id=user_id,
                        name=key_name,
                        zone_id=zone_id,
                        is_admin=False,
                        expires_at=api_key_expires_at,
                    )
                    session.commit()
                    logger.info("Created API key for user: %s", user_id)
                else:
                    logger.debug("User already has an API key: %s", user_id)

        except Exception as e:
            logger.error("Database operation failed during provisioning: %s", e)
            session.rollback()
            raise
        finally:
            session.close()

        # 6. Create user directories
        try:
            dir_paths = self._create_user_directories(user_id, zone_id, admin_context)
            created_resources["directories"] = dir_paths
            logger.info("Created %d directories for user %s", len(dir_paths), user_id)
        except Exception as e:
            logger.error("Failed to create user directories: %s", e)
            # Continue - directories might already exist

        # 7. Create default workspace
        workspace_path = None
        try:
            import uuid

            # Generate workspace ID: ws_personal_{12-char-uuid}
            uuid_suffix = str(uuid.uuid4()).replace("-", "")[:12]
            workspace_id = f"ws_personal_{uuid_suffix}"
            workspace_path = f"/zone/{zone_id}/user/{user_id}/workspace/{workspace_id}"

            if not nx.exists(workspace_path, context=admin_context):
                nx.mkdir(workspace_path, parents=True, exist_ok=True, context=admin_context)
                nx.register_workspace(
                    workspace_path,
                    name="Personal Workspace",
                    description="Default personal workspace",
                    context=admin_context,
                )
                logger.info("Created workspace: %s", workspace_path)
                created_resources["workspace"] = workspace_path
            else:
                logger.debug("Workspace already exists: %s", workspace_path)
                created_resources["workspace"] = workspace_path
        except Exception as e:
            logger.error("Failed to create workspace: %s", e)

        # 8. Create agents (if requested)
        agent_paths: list[str] = []
        if create_agents:
            try:
                from nexus.services.agents.agent_provisioning import create_standard_agents

                agent_results = create_standard_agents(nx, user_id, admin_context)

                for agent_name, agent_result in agent_results.items():
                    if agent_result and "config_path" in agent_result:
                        agent_paths.append(agent_result["config_path"])
                        created_resources["agents"].append(agent_name)

                logger.info("Created %d agents for user %s", len(agent_paths), user_id)
            except Exception as e:
                logger.error("Failed to create agents: %s", e)

        # 9. Import skills (if requested) - ASYNC for fast registration
        skill_paths: list[str] = []
        if import_skills:

            def _import_skills_async() -> None:
                try:
                    logger.info("[ASYNC] Starting background skill import for user %s", user_id)
                    imported_paths = self._import_user_skills(zone_id, user_id, admin_context)
                    logger.info(
                        "[ASYNC] Background skill import completed for %s: %d skills imported",
                        user_id,
                        len(imported_paths),
                    )

                    # Grant SkillBuilder permissions after skills are imported
                    if create_agents:
                        try:
                            from nexus.services.agents.agent_provisioning import (
                                grant_skill_builder_permissions,
                            )

                            granted = grant_skill_builder_permissions(nx, user_id, zone_id)
                            logger.info(
                                "[ASYNC] Granted %d permissions to SkillBuilder agent for user %s",
                                granted,
                                user_id,
                            )
                        except Exception as e:
                            logger.error("[ASYNC] Failed to grant SkillBuilder permissions: %s", e)
                except Exception as e:
                    logger.error("[ASYNC] Failed to import skills in background: %s", e)

            # Start background import thread
            import threading

            skill_import_thread = threading.Thread(
                target=_import_skills_async,
                name=f"skill-import-{user_id[:8]}",
                daemon=True,
            )
            skill_import_thread.start()
            logger.info("Skill import started in background for user %s", user_id)
            created_resources["skills"] = "importing"

        # 10. Grant ReBAC permissions (zone owner)
        try:
            nx.rebac_create(
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
            "skill_paths": skill_paths,
            "created_resources": created_resources,
        }

    @rpc_expose(description="Deprovision a user and remove all their resources")
    def deprovision_user(
        self,
        user_id: str,
        zone_id: str | None = None,
        delete_user_record: bool = False,
        force: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Deprovision a user and remove all their resources.

        Removes:
        - All user directories (workspace, memory, skill, agent, connector, resource)
        - All API keys for the user
        - All OAuth-specific records (OAuth API keys, OAuth account linkages)
        - All ReBAC permissions where user is subject
        - Entity registry entries for user and their agents
        - Optionally: UserModel record (soft delete)

        Args:
            user_id: User ID to deprovision
            zone_id: Zone ID (looked up from user if not provided)
            delete_user_record: If True, soft-deletes UserModel record
            force: Bypass safety checks (e.g., allow deprovisioning admin users)
            context: Operation context

        Returns:
            Dict with user_id, zone_id, and counts of deleted resources.
        """
        from datetime import UTC
        from datetime import datetime as dt_cls

        # Input validation
        if not user_id:
            raise ValueError("user_id is required")

        logger.info("Deprovisioning user %s", user_id)

        nx = self._nx

        # Use admin context for deprovisioning
        admin_context = context or OperationContext(
            user_id="system",
            groups=[],
            zone_id=zone_id or "system",
            is_admin=True,
        )

        # Track deleted resources
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

        # Look up user in database
        session = nx.SessionLocal()
        try:
            from sqlalchemy import select as sa_select

            from nexus.storage.models import UserModel

            user = (
                session.execute(sa_select(UserModel).filter_by(user_id=user_id)).scalars().first()
            )

            if not user:
                logger.warning("User not found in database: %s", user_id)
            else:
                # Get zone_id from user if not provided
                if not zone_id:
                    zone_id = user.zone_id
                result["zone_id"] = zone_id

                # Safety check: prevent deprovisioning global admin
                if user.is_global_admin and not force:
                    raise ValueError(
                        f"Cannot deprovision global admin user {user_id}. "
                        "Use force=True to override."
                    )

                logger.info(
                    "Found user %s (email=%s, zone=%s, is_admin=%s)",
                    user_id,
                    user.email,
                    zone_id,
                    user.is_global_admin,
                )

            # Update context with proper zone_id
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
                logger.info("Deleting user directories under %s", user_base_path)

                ALL_RESOURCE_TYPES = [
                    "workspace",
                    "memory",
                    "skill",
                    "agent",
                    "connector",
                    "resource",
                ]

                for resource_type in ALL_RESOURCE_TYPES:
                    dir_path = f"{user_base_path}/{resource_type}"
                    try:
                        was_deleted = self._delete_directory_recursive(dir_path, admin_context)
                        if was_deleted:
                            result["deleted_directories"].append(dir_path)
                            logger.info("Deleted directory: %s", dir_path)
                    except Exception as e:
                        logger.warning("Failed to delete directory %s: %s", dir_path, e)

            # 2. Delete API keys (both user and agent keys)
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

            # 3. Delete OAuth-specific records (for OAuth authenticated users)
            try:
                from sqlalchemy import inspect

                from nexus.storage.models import OAuthAPIKeyModel, UserOAuthAccountModel

                # Check if OAuth tables exist (they may not in test environments)
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
                    deleted_oauth_keys = oauth_key_result.rowcount
                    result["deleted_oauth_api_keys"] = deleted_oauth_keys
                    logger.info(
                        "Deleted %d OAuth API keys for user %s",
                        deleted_oauth_keys,
                        user_id,
                    )

                    oauth_acct_result: Any = session.execute(
                        sa_delete(UserOAuthAccountModel).filter_by(user_id=user_id)
                    )
                    deleted_oauth_accounts = oauth_acct_result.rowcount
                    session.commit()
                    result["deleted_oauth_accounts"] = deleted_oauth_accounts
                    logger.info(
                        "Deleted %d OAuth accounts for user %s",
                        deleted_oauth_accounts,
                        user_id,
                    )
                else:
                    logger.debug("OAuth tables not present in database, skipping OAuth cleanup")
            except Exception as e:
                logger.warning("Failed to delete OAuth records: %s", e)
                session.rollback()

            # 4. Delete ReBAC permissions
            try:
                rebac_manager = getattr(nx, "rebac_manager", None)
                if rebac_manager:
                    tuples = rebac_manager.query_tuples_by_subject(("user", user_id))
                    deleted_count = 0
                    for tuple_info in tuples:
                        tuple_id = tuple_info.get("tuple_id")
                        if tuple_id:
                            try:
                                nx.rebac_delete(tuple_id)
                                deleted_count += 1
                            except Exception as exc:
                                logger.warning("Failed to delete ReBAC tuple %s: %s", tuple_id, exc)
                    result["deleted_permissions"] = deleted_count
                    logger.info("Deleted %d ReBAC permissions for user %s", deleted_count, user_id)
                else:
                    logger.debug("ReBAC manager not available")
            except Exception as e:
                logger.warning("Failed to delete ReBAC permissions: %s", e)

            # 5. Delete entity registry entries
            try:
                entity_registry = getattr(nx, "_entity_registry", None)
                if entity_registry:
                    user_entity = entity_registry.get_entity("user", user_id)
                    if user_entity:
                        deleted = entity_registry.delete_entity("user", user_id, cascade=True)
                        if deleted:
                            result["deleted_entities"] = 1
                            logger.info(
                                "Deleted user entity and children from registry: %s",
                                user_id,
                            )
                        else:
                            logger.warning("Failed to delete user entity: %s", user_id)
                    else:
                        logger.debug("User not found in entity registry: %s", user_id)
            except Exception as e:
                logger.warning("Failed to delete entity registry entries: %s", e)

            # 6. Soft-delete user record (if requested)
            if delete_user_record and user:
                try:
                    user.is_active = 0
                    user.deleted_at = dt_cls.now(UTC)
                    session.commit()
                    result["user_record_deleted"] = True
                    logger.info("Soft-deleted user record: %s", user_id)
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
    # Internal helpers
    # ------------------------------------------------------------------

    def _delete_directory_recursive(self, dir_path: str, context: OperationContext) -> bool:
        """Recursively delete a directory and all its contents.

        Strategy:
        1. Delegate to backend's rmdir(recursive=True)
        2. Fall back to virtual filesystem deletion if backend deletion fails
        """
        nx = self._nx
        directory_removed = False
        had_content = False

        # Approach 1: Delegate to backend's rmdir
        backend = getattr(nx, "backend", None)
        if backend is not None:
            try:
                response = backend.rmdir(dir_path, recursive=True, context=context)
                if response.success:
                    directory_removed = True
                    had_content = True
                    logger.info("Deleted directory via backend: %s", dir_path)
            except Exception as e:
                logger.debug("Backend rmdir failed for %s: %s", dir_path, e)

        # If physical deletion worked, clean up metadata and permissions
        if directory_removed:
            self._cleanup_directory_metadata(dir_path)
            self._cleanup_directory_rebac(dir_path)
            self._invalidate_directory_caches(dir_path)
            return True

        # Approach 2: Virtual filesystem deletion (fallback)
        from contextlib import suppress

        directory_exists = False
        with suppress(Exception):
            directory_exists = nx.exists(dir_path, context=context)

        if not directory_exists:
            logger.debug("Directory does not exist: %s", dir_path)
            return False

        try:
            # List immediate children
            result = nx.list(dir_path, recursive=False, context=context)

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
                        nx.list(child_path, recursive=False, context=context)
                        is_dir = True
                    except Exception as exc:
                        logger.debug("Path %s is not a listable directory: %s", child_path, exc)
                elif isinstance(item, dict):
                    child_path = item.get("path")
                    if not child_path:
                        continue
                    is_dir = item.get("type", "") == "directory"

                if not child_path or child_path == dir_path:
                    continue

                try:
                    if is_dir:
                        self._delete_directory_recursive(child_path, context)
                    else:
                        nx.delete(child_path, context=context)
                except Exception as e:
                    logger.warning("Failed to delete %s: %s", child_path, e)

            # Try to remove the directory itself
            for method_name, method_func in [
                ("rmdir", lambda: nx.rmdir(dir_path, context=context)),
                ("delete", lambda: nx.delete(dir_path, context=context)),
            ]:
                try:
                    method_func()
                    directory_removed = True
                    logger.info("Deleted directory with %s: %s", method_name, dir_path)
                    break
                except Exception as e:
                    logger.debug("%s failed for %s: %s", method_name, dir_path, e)

        except Exception as e:
            logger.error("Virtual filesystem deletion failed for %s: %s", dir_path, e)

        if not directory_removed:
            logger.warning("Could not remove directory %s", dir_path)

        return had_content or directory_removed

    def _cleanup_directory_metadata(self, dir_path: str) -> None:
        """Clean up file path metadata after directory deletion."""
        nx = self._nx
        session_factory = getattr(nx, "SessionLocal", None)
        if session_factory is None:
            return
        try:
            session = session_factory()
            try:
                from sqlalchemy import delete as sa_delete

                from nexus.storage.models import FilePathModel

                fp_result: Any = session.execute(
                    sa_delete(FilePathModel).where(FilePathModel.virtual_path.like(f"{dir_path}%"))
                )
                deleted_count = fp_result.rowcount
                session.commit()
                logger.debug("Deleted %d file path entries for %s", deleted_count, dir_path)
            finally:
                session.close()
        except Exception as e:
            logger.warning("Failed to clean up file paths for %s: %s", dir_path, e)

    def _cleanup_directory_rebac(self, dir_path: str) -> None:
        """Clean up ReBAC permission tuples for deleted directory."""
        nx = self._nx
        rebac_manager = getattr(nx, "rebac_manager", None)
        if not rebac_manager:
            return
        session_factory = getattr(nx, "SessionLocal", None)
        if session_factory is None:
            return
        try:
            session = session_factory()
            try:
                from sqlalchemy import delete as sa_delete

                from nexus.storage.models import ReBACTupleModel

                rebac_result: Any = session.execute(
                    sa_delete(ReBACTupleModel).where(
                        ReBACTupleModel.object_type == "file",
                        ReBACTupleModel.object_id.like(f"{dir_path}%"),
                    )
                )
                deleted_tuples = rebac_result.rowcount
                session.commit()
                logger.debug("Deleted %d ReBAC tuples for %s", deleted_tuples, dir_path)
            finally:
                session.close()
        except Exception as e:
            logger.warning("Failed to clean up ReBAC tuples for %s: %s", dir_path, e)

    def _invalidate_directory_caches(self, dir_path: str) -> None:
        """Invalidate VFS caches after directory deletion."""
        nx = self._nx
        try:
            parent_path = "/".join(dir_path.rstrip("/").split("/")[:-1])
            list_cache = getattr(nx, "_list_cache", None)
            if parent_path and list_cache is not None:
                list_cache.pop(parent_path, None)
                list_cache.pop(dir_path, None)
            exists_cache = getattr(nx, "_exists_cache", None)
            if exists_cache is not None:
                exists_cache.pop(dir_path, None)
                if parent_path:
                    exists_cache.pop(parent_path, None)
        except Exception as exc:
            logger.debug(
                "Failed to invalidate caches after directory deletion of %s: %s",
                dir_path,
                exc,
            )

        # Clear tiger cache entries
        rebac_manager = getattr(nx, "rebac_manager", None)
        if rebac_manager is not None:
            tiger_cache = getattr(rebac_manager, "_tiger_cache", None)
            if tiger_cache is not None:
                try:
                    if hasattr(tiger_cache, "invalidate_all"):
                        tiger_cache.invalidate_all()
                        logger.debug("Invalidated tiger cache")
                except Exception as e:
                    logger.debug("Failed to invalidate tiger cache: %s", e)

    def _create_user_directories(
        self, user_id: str, zone_id: str, context: OperationContext
    ) -> list[str]:
        """Create all user directories with proper permissions."""
        nx = self._nx
        ALL_RESOURCE_TYPES = [
            "workspace",
            "memory",
            "skill",
            "agent",
            "connector",
            "resource",
        ]
        created_paths: list[str] = []

        for resource_type in ALL_RESOURCE_TYPES:
            folder_path = f"/zone/{zone_id}/user/{user_id}/{resource_type}"

            try:
                nx.mkdir(folder_path, parents=True, exist_ok=True, context=context)

                try:
                    nx.rebac_create(
                        subject=("user", user_id),
                        relation="direct_owner",
                        object=("file", folder_path),
                        zone_id=zone_id,
                        context=context,
                    )
                except Exception as e:
                    if "already exists" in str(e).lower():
                        logger.debug("Permission already exists for %s", folder_path)
                    else:
                        raise

                created_paths.append(folder_path)
            except Exception as e:
                logger.warning("Failed to create directory %s: %s", folder_path, e)

        return created_paths

    def _import_user_skills(
        self, _zone_id: str, _user_id: str, context: OperationContext
    ) -> list[str]:
        """Import all default skills from data/skills/ directory."""
        import base64
        import os
        from pathlib import Path

        nx = self._nx

        # Find skills directory
        possible_dirs = []

        # 1. Try NEXUS_DATA_DIR environment variable (for Docker/production)
        if os.environ.get("NEXUS_DATA_DIR"):
            data_dir = Path(os.environ["NEXUS_DATA_DIR"])
            possible_dirs.append(data_dir / "skills")

        # 2. Try backend data directory if available
        backend = getattr(nx, "backend", None)
        if (
            backend is not None
            and getattr(backend, "has_data_dir", False) is True
            and backend.data_dir
        ):
            backend_data_dir = Path(backend.data_dir)
            possible_dirs.append(backend_data_dir / "skills")

        # 3. Fall back to relative path from module location (for development)
        possible_dirs.append(Path(__file__).parent.parent.parent / "data" / "skills")

        skills_dir = None
        for dir_path in possible_dirs:
            if dir_path.exists() and dir_path.is_dir():
                skills_dir = dir_path
                break

        if not skills_dir:
            logger.warning(
                "Skills directory not found in any of: %s",
                [str(d) for d in possible_dirs],
            )
            return []

        skill_package_service = getattr(nx, "skill_package_service", None)
        if skill_package_service is None:
            logger.warning("skill_package_service not available, skipping skill import")
            return []

        skill_files = list(skills_dir.glob("*.skill"))
        skill_paths: list[str] = []

        for skill_file in skill_files:
            try:
                with open(skill_file, "rb") as f:
                    zip_bytes = f.read()

                zip_base64 = base64.b64encode(zip_bytes).decode("utf-8")

                result = skill_package_service.import_skill(
                    zip_data=zip_base64,
                    tier="personal",
                    allow_overwrite=False,
                    context=context,
                )

                skill_paths.extend(result.get("skill_paths", []))
                logger.debug("Imported skill: %s", skill_file.name)
            except Exception as e:
                logger.warning("Failed to import skill %s: %s", skill_file.name, e)

        return skill_paths
