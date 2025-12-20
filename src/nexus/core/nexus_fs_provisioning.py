"""Provisioning API for NexusFS - User and Tenant Creation.

This module provides comprehensive provisioning APIs for creating users and tenants
with complete resource setup including directories, workspaces, agents, and permissions.

Key Features:
- provision_user(): Create user with personal or business account
- provision_tenant(): Create new tenant/organization
- Automatic directory structure creation
- Default workspace, agents, and skills provisioning
- ReBAC permission setup using tenant group naming convention
- Database record creation (UserModel, TenantModel)
- API key generation
- Idempotent operations
"""

from __future__ import annotations

import secrets
import string
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.core.permissions import OperationContext
from nexus.core.rpc_decorator import rpc_expose
from nexus.storage.models import TenantModel, UserModel

if TYPE_CHECKING:
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore

# Resource types that get provisioned
ALL_RESOURCE_TYPES = ["workspace", "memory", "skill", "agent", "connector", "resource"]


class NexusFSProvisioningMixin:
    """Mixin providing user and tenant provisioning APIs for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        metadata: SQLAlchemyMetadataStore
        _rebac_manager: Any
        _entity_registry: Any
        _enforce_permissions: bool

        def mkdir(
            self, path: str, parents: bool = False, exist_ok: bool = False, context: Any = None
        ) -> None: ...

        def write(self, path: str, content: bytes, context: Any = None) -> str: ...

        def rebac_create(
            self,
            subject: tuple[str, str],
            relation: str,
            object: tuple[str, str],
            tenant_id: str | None = None,
            context: Any = None,
        ) -> str: ...

        def register_workspace(
            self, path: str, name: str | None = None, context: Any = None
        ) -> dict[str, Any]: ...

        def register_agent(
            self,
            agent_id: str,
            name: str | None = None,
            agent_type: str = "ImpersonatedUser",
            context: Any = None,
        ) -> dict[str, Any]: ...

        def import_skill(
            self, skill_path: str, target_path: str | None = None, context: Any = None
        ) -> dict[str, Any]: ...

    def _generate_api_key(self, prefix: str = "nx") -> str:
        """Generate a secure API key.

        Args:
            prefix: Key prefix (default: "nx")

        Returns:
            Generated API key in format: {prefix}_xxx
        """
        # Generate 32 characters of random alphanumeric string
        alphabet = string.ascii_letters + string.digits
        random_part = "".join(secrets.choice(alphabet) for _ in range(32))
        return f"{prefix}_{random_part}"

    def _create_user_directories(
        self,
        tenant_id: str,
        user_id: str,
        context: OperationContext,
    ) -> None:
        """Create all user resource directories.

        Creates directory structure:
        /tenant:{tenant_id}/user:{user_id}/
          ├── workspace/
          ├── memory/
          ├── skill/
          ├── agent/
          ├── connector/
          └── resource/

        Args:
            tenant_id: Tenant ID
            user_id: User ID
            context: Operation context with admin privileges
        """
        for resource_type in ALL_RESOURCE_TYPES:
            folder_path = f"/tenant:{tenant_id}/user:{user_id}/{resource_type}"
            self.mkdir(folder_path, parents=True, exist_ok=True, context=context)

            # Create placeholder file to make directory visible
            placeholder_path = f"{folder_path}/.placeholder"
            self.write(placeholder_path, b"", context=context)

            # Grant user ownership of their resource folder
            self.rebac_create(
                subject=("user", user_id),
                relation="owner-of",
                object=("file", folder_path),
                tenant_id=tenant_id,
                context=context,
            )

    def _create_tenant_directories(
        self,
        tenant_id: str,
        context: OperationContext,
    ) -> None:
        """Create tenant-level resource directories.

        Creates directory structure:
        /tenant:{tenant_id}/
          ├── workspace/
          ├── memory/
          ├── skill/
          ├── agent/
          ├── connector/
          └── resource/

        Args:
            tenant_id: Tenant ID
            context: Operation context with admin privileges
        """
        # Create tenant root directory
        tenant_root = f"/tenant:{tenant_id}"
        self.mkdir(tenant_root, parents=True, exist_ok=True, context=context)

        # Create all resource type directories
        for resource_type in ALL_RESOURCE_TYPES:
            folder_path = f"{tenant_root}/{resource_type}"
            self.mkdir(folder_path, parents=True, exist_ok=True, context=context)

            # Create placeholder
            placeholder_path = f"{folder_path}/.placeholder"
            self.write(placeholder_path, b"", context=context)

    def _provision_default_workspace(
        self,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        context: OperationContext,
    ) -> dict[str, Any]:
        """Create and register default workspace for user.

        Args:
            tenant_id: Tenant ID
            user_id: User ID
            workspace_id: Workspace ID to create
            context: Operation context

        Returns:
            Workspace info dict
        """
        workspace_path = f"/tenant:{tenant_id}/user:{user_id}/workspace/{workspace_id}"
        self.mkdir(workspace_path, parents=True, exist_ok=True, context=context)

        # Register workspace in entity registry
        workspace_info = self.register_workspace(
            workspace_path, name=f"{user_id}'s workspace", context=context
        )

        # Grant user ownership
        self.rebac_create(
            subject=("user", user_id),
            relation="owner-of",
            object=("file", workspace_path),
            tenant_id=tenant_id,
            context=context,
        )

        return workspace_info

    def _provision_default_agents(
        self,
        tenant_id: str,
        user_id: str,
        context: OperationContext,
    ) -> dict[str, dict[str, Any]]:
        """Create default agents for user.

        Creates two agents:
        - ImpersonatedUser: Full user permissions
        - UntrustedAgent: No permissions by default

        Args:
            tenant_id: Tenant ID
            user_id: User ID
            context: Operation context

        Returns:
            Dict mapping agent IDs to agent info
        """
        agents = {}

        # Create ImpersonatedUser agent
        # Note: agent_type will be added to register_agent() in Issue #823
        impersonated_id = f"{user_id}-impersonated"
        agents["impersonated"] = self.register_agent(
            agent_id=impersonated_id,
            name=f"{user_id}'s Impersonated Agent",
            description="Agent that impersonates user with full permissions",
            metadata={"agent_type": "ImpersonatedUser", "tenant_id": tenant_id},
            context=dict(context.__dict__) if hasattr(context, "__dict__") else context,
        )

        # Create UntrustedAgent
        untrusted_id = f"{user_id}-untrusted"
        agents["untrusted"] = self.register_agent(
            agent_id=untrusted_id,
            name=f"{user_id}'s Untrusted Agent",
            description="Agent with zero permissions by default",
            metadata={"agent_type": "UntrustedAgent", "tenant_id": tenant_id},
            context=dict(context.__dict__) if hasattr(context, "__dict__") else context,
        )

        return agents

    def _import_default_skills(
        self,
        tenant_id: str,
        user_id: str,
        context: OperationContext,
        skip_heavy: bool = False,
    ) -> list[dict[str, Any]]:
        """Import default skills for user.

        Imports skills from data/skills/ directory if available.
        Skills imported: skill-creator, pdf, docx, xlsx, pptx, internal-comms

        Args:
            tenant_id: Tenant ID
            user_id: User ID
            context: Operation context
            skip_heavy: Skip heavy skills for testing/CI

        Returns:
            List of imported skill info dicts
        """
        imported_skills = []

        # Skills to import (from data/skills/)
        skill_names = [
            "skill-creator",
            "pdf",
            "docx",
            "xlsx",
            "pptx",
            "internal-comms",
        ]

        # Skip heavy skills in CI/test environments
        if skip_heavy:
            skill_names = ["skill-creator"]  # Only keep lightweight skills

        # Try to import each skill
        for skill_name in skill_names:
            try:
                # Skills are stored in data/skills/{skill_name}.skill
                skill_file = f"data/skills/{skill_name}.skill"
                target_path = f"/tenant:{tenant_id}/user:{user_id}/skill/{skill_name}.skill"

                # Check if skill file exists (import_skill will handle this)
                skill_info = self.import_skill(
                    skill_path=skill_file, target_path=target_path, context=context
                )
                imported_skills.append(skill_info)
            except Exception:
                # Skip if skill file doesn't exist or import fails
                pass

        return imported_skills

    @rpc_expose(description="Provision a new user with complete resource setup")
    def provision_user(
        self,
        user_id: str,
        email: str | None = None,
        username: str | None = None,
        display_name: str | None = None,
        password_hash: str | None = None,
        account_type: str = "personal",  # "personal" | "business"
        tenant_id: str | None = None,  # Required for business, auto-generated for personal
        role: str = "owner",  # "owner" | "admin" | "member"
        create_api_key: bool = True,
        create_workspace: bool = True,
        create_agents: bool = True,
        import_skills: bool = False,  # Disabled by default (can be slow)
        is_global_admin: bool = False,
        user_metadata: dict[str, Any] | None = None,
        context: OperationContext | dict | None = None,
    ) -> dict[str, Any]:
        """Provision a new user with complete resource setup.

        This API handles two account types:
        1. Personal Account (PLG model): User creates account → auto-creates personal org
           - tenant_id is auto-generated as "personal-{user_id}"
           - User becomes owner of their personal tenant
        2. Business Account: User joins existing organization
           - tenant_id must be provided
           - User gets specified role (owner, admin, or member)

        Creates:
        - Database record (UserModel)
        - User directories: /tenant:{tid}/user:{uid}/{resource_type}
        - Default workspace (optional)
        - Default agents: ImpersonatedUser, UntrustedAgent (optional)
        - Default skills (optional)
        - API key (optional)
        - ReBAC permissions

        Args:
            user_id: Unique user identifier
            email: User email (optional but recommended)
            username: Username (optional)
            display_name: Display name (defaults to user_id)
            password_hash: Hashed password (optional, for password auth)
            account_type: "personal" or "business"
            tenant_id: Tenant ID (required for business, auto for personal)
            role: User role in tenant - "owner", "admin", or "member"
            create_api_key: Generate API key for user
            create_workspace: Create default workspace
            create_agents: Create default agents (ImpersonatedUser, UntrustedAgent)
            import_skills: Import default skills (slow, disabled by default)
            is_global_admin: Mark user as global admin (system-wide privileges)
            user_metadata: Additional metadata as dict (stored as JSON)
            context: Operation context (must have system/admin privileges)

        Returns:
            Dict with provisioning results:
            {
                "user_id": str,
                "tenant_id": str,
                "role": str,
                "account_type": str,
                "api_key": str | None,
                "workspace": dict | None,
                "agents": dict | None,
                "skills": list | None,
                "created_at": datetime,
            }

        Raises:
            ValueError: Invalid parameters
            PermissionError: Insufficient privileges
            RuntimeError: Provisioning failed

        Examples:
            # Personal account (PLG model)
            result = nx.provision_user(
                user_id="alice",
                email="alice@example.com",
                account_type="personal",
                context=system_context,
            )
            # Creates tenant "personal-alice" with alice as owner

            # Business account (join existing org)
            result = nx.provision_user(
                user_id="bob",
                email="bob@acme.com",
                account_type="business",
                tenant_id="acme",
                role="member",
                context=system_context,
            )
        """
        # Convert dict context to OperationContext
        if isinstance(context, dict):
            context = OperationContext(**context)

        # Validate parameters
        if not user_id:
            raise ValueError("user_id is required")

        if account_type not in ["personal", "business"]:
            raise ValueError("account_type must be 'personal' or 'business'")

        if role not in ["owner", "admin", "member"]:
            raise ValueError("role must be 'owner', 'admin', or 'member'")

        # For personal accounts, auto-generate tenant_id
        if account_type == "personal":
            if tenant_id is None:
                tenant_id = f"personal-{user_id}"
            # Personal accounts are always owners of their tenant
            role = "owner"
        else:
            # Business accounts require tenant_id
            if tenant_id is None:
                raise ValueError("tenant_id is required for business accounts")

        # Check if user already exists (idempotency)
        with self.metadata.SessionLocal() as session:
            existing_user = session.query(UserModel).filter_by(user_id=user_id).first()
            if existing_user and existing_user.is_active:
                # User already exists and is active - return existing info
                # Ensure timezone awareness for consistency
                created_at = existing_user.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                return {
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "role": role,
                    "account_type": account_type,
                    "api_key": existing_user.api_key,
                    "workspace": None,
                    "agents": None,
                    "skills": None,
                    "created_at": created_at,
                    "already_exists": True,
                }

        # Create admin context for provisioning operations
        # Note: Use is_admin=True (not is_system) since system bypass is restricted to /system/* paths
        provision_context = OperationContext(
            user="admin_provisioning",
            groups=[],
            is_admin=True,
            is_system=False,
            tenant_id=tenant_id,
        )

        result: dict[str, Any] = {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "role": role,
            "account_type": account_type,
            "api_key": None,
            "workspace": None,
            "agents": None,
            "skills": None,
            "created_at": datetime.now(UTC),
        }

        # For personal accounts, create the tenant first
        if account_type == "personal":
            # Check if personal tenant already exists
            with self.metadata.SessionLocal() as session:
                existing_tenant = session.query(TenantModel).filter_by(tenant_id=tenant_id).first()
                if not existing_tenant or not existing_tenant.is_active:
                    # Create personal tenant
                    self.provision_tenant(
                        tenant_id=tenant_id,
                        name=f"{display_name or user_id}'s Organization",
                        description=f"Personal organization for {user_id}",
                        context=provision_context,
                    )

        # Generate API key if requested
        api_key = None
        if create_api_key:
            api_key = self._generate_api_key()
            result["api_key"] = api_key

        # Create user database record
        import json

        # Use consistent timestamp for both result and database
        now = datetime.now(UTC)
        with self.metadata.SessionLocal() as session:
            user_model = UserModel(
                user_id=user_id,
                username=username,
                email=email,
                display_name=display_name or user_id,
                password_hash=password_hash,
                api_key=api_key,
                tenant_id=tenant_id,
                is_global_admin=1 if is_global_admin else 0,
                is_active=1,
                user_metadata=json.dumps(user_metadata) if user_metadata else None,
                created_at=now,
                updated_at=now,
            )
            session.add(user_model)
            session.commit()
            # Update result with actual created_at from database
            result["created_at"] = user_model.created_at

        # Create user directory structure
        self._create_user_directories(tenant_id, user_id, provision_context)

        # Add user to tenant with specified role
        from nexus.server.auth.user_helpers import add_user_to_tenant

        add_user_to_tenant(
            self._rebac_manager,
            user_id,
            tenant_id,
            role,
            caller_user_id=None,  # Skip permission check (system provisioning)
        )

        # Create default workspace if requested
        if create_workspace:
            workspace_id = "default"
            result["workspace"] = self._provision_default_workspace(
                tenant_id, user_id, workspace_id, provision_context
            )

        # Create default agents if requested
        if create_agents:
            result["agents"] = self._provision_default_agents(tenant_id, user_id, provision_context)

        # Import default skills if requested
        if import_skills:
            result["skills"] = self._import_default_skills(
                tenant_id, user_id, provision_context, skip_heavy=False
            )

        return result

    @rpc_expose(description="Provision a new tenant/organization")
    def provision_tenant(
        self,
        tenant_id: str,
        name: str,
        domain: str | None = None,
        description: str | None = None,
        settings: dict[str, Any] | None = None,
        create_directories: bool = True,
        context: OperationContext | dict | None = None,
    ) -> dict[str, Any]:
        """Provision a new tenant/organization.

        Creates:
        - Database record (TenantModel)
        - Tenant directories: /tenant:{tid}/{resource_type}
        - ReBAC groups: tenant-{tid}, tenant-{tid}-admins, tenant-{tid}-owners
        - Entity registry entries

        Args:
            tenant_id: Unique tenant identifier
            name: Tenant display name
            domain: Tenant domain (optional, must be unique if provided)
            description: Tenant description
            settings: Tenant settings as dict (stored as JSON)
            create_directories: Create tenant directory structure
            context: Operation context (must have system/admin privileges)

        Returns:
            Dict with tenant info:
            {
                "tenant_id": str,
                "name": str,
                "domain": str | None,
                "created_at": datetime,
                "groups": {
                    "members": str,  # "tenant-{tid}"
                    "admins": str,   # "tenant-{tid}-admins"
                    "owners": str,   # "tenant-{tid}-owners"
                }
            }

        Raises:
            ValueError: Invalid parameters or tenant already exists
            PermissionError: Insufficient privileges

        Examples:
            result = nx.provision_tenant(
                tenant_id="acme",
                name="Acme Corp",
                domain="acme.com",
                description="Acme Corporation",
                context=system_context,
            )
        """
        # Convert dict context to OperationContext
        if isinstance(context, dict):
            context = OperationContext(**context)

        # Validate parameters
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not name:
            raise ValueError("name is required")

        # Import helper function locally to avoid circular import
        from nexus.server.auth.user_helpers import tenant_group_id

        # Check if tenant already exists (idempotency)
        with self.metadata.SessionLocal() as session:
            existing_tenant = session.query(TenantModel).filter_by(tenant_id=tenant_id).first()
            if existing_tenant and existing_tenant.is_active:
                # Tenant already exists - return existing info
                # Ensure timezone awareness for consistency
                created_at = existing_tenant.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                return {
                    "tenant_id": tenant_id,
                    "name": existing_tenant.name,
                    "domain": existing_tenant.domain,
                    "created_at": created_at,
                    "groups": {
                        "members": tenant_group_id(tenant_id),
                        "admins": f"{tenant_group_id(tenant_id)}-admins",
                        "owners": f"{tenant_group_id(tenant_id)}-owners",
                    },
                    "already_exists": True,
                }

        # Create database record
        import json

        # Use consistent timestamp for both result and database
        now = datetime.now(UTC)
        with self.metadata.SessionLocal() as session:
            tenant_model = TenantModel(
                tenant_id=tenant_id,
                name=name,
                domain=domain,
                description=description,
                settings=json.dumps(settings) if settings else None,
                is_active=1,
                created_at=now,
                updated_at=now,
            )
            session.add(tenant_model)
            session.commit()
            # Store created_at for return value
            tenant_created_at = tenant_model.created_at

        # Create admin context for provisioning
        # Note: Use is_admin=True (not is_system) since system bypass is restricted to /system/* paths
        provision_context = OperationContext(
            user="admin_provisioning",
            groups=[],
            is_admin=True,
            is_system=False,
            tenant_id=tenant_id,
        )

        # Create tenant directory structure
        if create_directories:
            self._create_tenant_directories(tenant_id, provision_context)

        # Define tenant groups (managed via ReBAC, not entity registry)
        # Groups: tenant-{tid} (members), tenant-{tid}-admins, tenant-{tid}-owners
        base_group = tenant_group_id(tenant_id)
        groups = {
            "members": base_group,
            "admins": f"{base_group}-admins",
            "owners": f"{base_group}-owners",
        }

        return {
            "tenant_id": tenant_id,
            "name": name,
            "domain": domain,
            "created_at": tenant_created_at,
            "groups": groups,
        }
