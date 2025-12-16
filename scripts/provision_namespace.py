"""
Provision Nexus server with resources following multi-tenant namespace convention.

Usage:
    python scripts/provision_namespace.py --tenant default --user alice
    python scripts/provision_namespace.py --user alice  # Uses 'default' tenant
"""

import argparse
import base64
import os
import re
import uuid
from pathlib import Path
from typing import Any

from _core.agent_manager import create_standard_agents, grant_agent_resource_access

# Import provisioning utilities
from _core.constants import (
    AGENT_RESOURCE_GRANTS,
    ALL_RESOURCE_TYPES,
    RESOURCE_PREFIXES,
)

import nexus
from nexus.core.permissions import OperationContext


def generate_resource_id(resource_type: str, name: str | None = None) -> str:
    """Generate resource ID with prefix and 12-character UUID for uniqueness."""
    prefixes = RESOURCE_PREFIXES

    prefix = prefixes.get(resource_type, "res")

    # Always use consistent 12-character UUID for all cases
    uuid_suffix = str(uuid.uuid4()).replace("-", "")[:12]

    if name:
        # Sanitize name (lowercase, alphanumeric + underscore)
        sanitized = re.sub(r"[^a-z0-9_]", "_", name.lower())

        # Validate sanitized name has meaningful content (at least 2 non-underscore chars)
        meaningful_chars = sanitized.replace("_", "")
        if len(meaningful_chars) < 2:
            print(f"Warning: Name '{name}' sanitizes to mostly underscores, using UUID-only ID")
            return f"{prefix}_{uuid_suffix}"

        # Truncate if too long (max 30 chars for name portion)
        max_name_length = 30
        truncated = sanitized[:max_name_length] if len(sanitized) > max_name_length else sanitized

        return f"{prefix}_{truncated}_{uuid_suffix}"
    else:
        return f"{prefix}_{uuid_suffix}"


def system_path(resource_type: str, resource_id: str) -> str:
    """Generate system-wide path."""
    return f"/{resource_type}/{resource_id}"


def tenant_path(tenant_id: str, resource_type: str, resource_id: str) -> str:
    """Generate tenant-wide path."""
    return f"/tenant:{tenant_id}/{resource_type}/{resource_id}"


def user_path(tenant_id: str, user_id: str, resource_type: str, resource_id: str) -> str:
    """Generate user-owned path."""
    return f"/tenant:{tenant_id}/user:{user_id}/{resource_type}/{resource_id}"


def provision_system_resources(nx: Any) -> None:
    """Create system-wide resources."""
    # Create system context for provisioning
    # Note: Using admin user context as system context may not be fully supported
    context = OperationContext(
        user="system",
        groups=[],
        tenant_id="default",
        is_admin=True,
        is_system=False,  # Set to False as is_system may not be fully supported
    )

    print("Creating system resources...")

    try:
        # System default template
        template_id = generate_resource_id("resource", "default_template")
        template_path = system_path("resource", template_id)
        nx.write(template_path, b'{"type": "template", "version": "1.0"}', context=context)

        # Grant system ownership
        nx.rebac_create(
            subject=("system", "admin"),
            relation="owner-of",
            object=("file", template_path),
            tenant_id="default",
            context=context,
        )
        print(f"  ✓ Created {template_path} with ownership")
    except Exception as e:
        print(f"  ✗ Failed to create system template: {e}")

    try:
        # System skill
        skill_id = generate_resource_id("skill", "summarize")
        skill_path = system_path("skill", skill_id)
        nx.write(f"{skill_path}/skill.py", b"# Summarize skill", context=context)

        # Grant system ownership
        nx.rebac_create(
            subject=("system", "admin"),
            relation="owner-of",
            object=("file", skill_path),
            tenant_id="default",
            context=context,
        )
        print(f"  ✓ Created {skill_path} with ownership")
    except Exception as e:
        print(f"  ✗ Failed to create system skill: {e}")


def provision_tenant_resources(nx: Any, tenant_id: str) -> None:
    """Create tenant-wide resources."""
    # Create admin context for provisioning
    context = OperationContext(
        user="system",
        groups=[],
        tenant_id=tenant_id,
        is_admin=True,
        is_system=False,
    )

    print(f"Creating tenant resources for {tenant_id}...")

    try:
        # Tenant logo
        logo_id = generate_resource_id("resource", "company_logo")
        logo_path = tenant_path(tenant_id, "resource", logo_id)
        nx.write(logo_path, b"# Company logo placeholder", context=context)

        # Grant tenant ownership
        nx.rebac_create(
            subject=("tenant", tenant_id),
            relation="owner-of",
            object=("file", logo_path),
            tenant_id=tenant_id,
            context=context,
        )
        print(f"  ✓ Created {logo_path} with ownership")
    except Exception as e:
        print(f"  ✗ Failed to create tenant logo: {e}")

    try:
        # Tenant connector (e.g., Slack)
        connector_id = generate_resource_id("connector", "slack")
        connector_path = tenant_path(tenant_id, "connector", connector_id)
        # Note: Actual connector setup would use add_mount()
        print(f"  ✓ Would create connector at {connector_path}")
    except Exception as e:
        print(f"  ✗ Failed to create tenant connector: {e}")


def provision_admin_user_folders(nx: Any, tenant_id: str) -> None:
    """Create folder structure for admin user with proper permissions."""
    print(f"Creating admin user folders for {tenant_id}/admin...")

    # Create admin context for provisioning
    context = OperationContext(
        user="system",
        groups=[],
        tenant_id=tenant_id,
        is_admin=True,
        is_system=False,
    )

    admin_user_id = "admin"

    # First, create and grant permissions on the parent user directory
    try:
        user_dir_path = f"/tenant:{tenant_id}/user:{admin_user_id}"
        nx.mkdir(user_dir_path, parents=True, exist_ok=True, context=context)

        # Create placeholder file to make directory discoverable
        placeholder_path = f"{user_dir_path}/.placeholder"
        nx.write(placeholder_path, b"", context=context)

        # Grant admin user ownership on the user directory
        nx.rebac_create(
            subject=("user", admin_user_id),
            relation="owner-of",
            object=("file", user_dir_path),
            tenant_id=tenant_id,
            context=context,
        )
        print(f"  ✓ Created user directory {user_dir_path} with ownership")
    except Exception as e:
        print(f"  ✗ Failed to create admin user directory: {e}")

    # Create all 6 folders: workspace, memory, skill, agent, connector, resource
    for resource_type in ALL_RESOURCE_TYPES:
        try:
            # Create the folder path: /tenant:<tid>/user:admin/<resource_type>
            folder_path = f"/tenant:{tenant_id}/user:{admin_user_id}/{resource_type}"

            # Create the directory
            nx.mkdir(folder_path, parents=True, exist_ok=True, context=context)

            # Create a placeholder file to make the directory visible in listings
            placeholder_path = f"{folder_path}/.placeholder"
            nx.write(placeholder_path, b"", context=context)

            # Grant admin user ownership on the directory (CRITICAL: admin user must own these folders)
            nx.rebac_create(
                subject=("user", admin_user_id),
                relation="owner-of",
                object=("file", folder_path),
                tenant_id=tenant_id,
                context=context,
            )
            print(f"  ✓ Created folder {folder_path} with admin user ownership")
        except Exception as e:
            print(f"  ✗ Failed to create admin folder {resource_type}: {e}")

    # Create context with admin as the user (not system) so agents are owned by admin
    admin_context = OperationContext(
        user=admin_user_id,  # Set user to admin, not system
        groups=[],
        tenant_id=tenant_id,
        is_admin=True,
        is_system=False,
    )

    # Create default workspace "my workspace" for admin user
    try:
        workspace_id = generate_resource_id("workspace", "my_workspace")
        workspace_path = user_path(tenant_id, admin_user_id, "workspace", workspace_id)

        # Create the workspace directory first
        nx.mkdir(workspace_path, parents=True, exist_ok=True, context=admin_context)

        # Register the workspace (this will auto-grant ownership via ReBAC if rebac_manager is available)
        workspace_info = nx.register_workspace(
            workspace_path, name="my workspace", context=admin_context
        )
        print(
            f"  ✓ Registered workspace {workspace_path} with name '{workspace_info.get('name', 'N/A') if isinstance(workspace_info, dict) else getattr(workspace_info, 'name', 'N/A')}'"
        )

        # Explicitly grant admin user ownership via ReBAC (in case auto-grant didn't work)
        try:
            nx.rebac_create(
                subject=("user", admin_user_id),
                relation="owner-of",
                object=("file", workspace_path),
                tenant_id=tenant_id,
                context=admin_context,
            )
            print("  ✓ Granted ownership to admin user")
        except Exception as rebac_e:
            print(f"  ⚠ Failed to grant ownership (may already exist): {rebac_e}")

        # Verify workspace is registered by checking list_workspaces
        try:
            all_workspaces = nx.list_workspaces()
            workspace_found = any(ws.get("path") == workspace_path for ws in all_workspaces)
            if workspace_found:
                print(
                    f"  ✓ Verified workspace appears in list_workspaces (total: {len(all_workspaces)} workspaces)"
                )
            else:
                print("  ⚠ WARNING: Workspace not found in list_workspaces")
                print(f"    Expected path: {workspace_path}")
                print(f"    Found workspaces: {[ws.get('path') for ws in all_workspaces]}")
        except Exception as verify_e:
            print(f"  ⚠ Could not verify workspace registration: {verify_e}")
            import traceback

            traceback.print_exc()

        # Read README.md from nexus/data/ and copy it to the workspace
        nexus_dir = Path(__file__).parent.parent
        readme_source = nexus_dir / "data" / "README.md"

        if readme_source.exists():
            readme_content = readme_source.read_bytes()
            readme_path = f"{workspace_path}/README.md"
            nx.write(readme_path, readme_content, context=admin_context)
            print("  ✓ Copied README.md from data/ to workspace")
        else:
            print(f"  ⚠ README.md not found at {readme_source}, skipping")
    except Exception as e:
        print(f"  ✗ Failed to create admin workspace: {e}")
        import traceback

        traceback.print_exc()

    # Import default skills from pre-zipped .skill files (before creating agents)
    # Returns a dict mapping skill names to their paths
    skill_paths_map = provision_default_skills(nx, tenant_id, admin_user_id, admin_context)
    print(f"\n[DEBUG] Imported skills map: {skill_paths_map}")

    # Create standard agents (ImpersonatedUser and UntrustedAgent)
    agent_metadata = {
        "platform": "langgraph",
        "endpoint_url": "http://localhost:2024",
        "agent_id": "agent",
    }
    agent_results = create_standard_agents(nx, admin_user_id, admin_context, agent_metadata)

    # Define agent_id for UntrustedAgent (used later for granting skill-creator access)
    agent_id = f"{admin_user_id},UntrustedAgent"

    # Grant read-only permissions to UntrustedAgent for resource folders
    # Note: 'skill' is excluded from AGENT_RESOURCE_GRANTS by default
    if agent_results["untrusted"]:
        grant_agent_resource_access(
            nx, admin_user_id, tenant_id, AGENT_RESOURCE_GRANTS, agent_name="UntrustedAgent"
        )

        # Then, grant access to skill-creator skill (using actual path from import)
        try:
            # Get the actual skill path from the imported skills
            print(f"\n[DEBUG] Available skill names in map: {list(skill_paths_map.keys())}")
            skill_creator_path = skill_paths_map.get("skill-creator")
            if not skill_creator_path:
                # Try to find it by checking all keys (maybe the name is slightly different)
                matching_keys = [
                    k for k in skill_paths_map if "skill" in k.lower() and "creator" in k.lower()
                ]
                if matching_keys:
                    skill_creator_path = skill_paths_map[matching_keys[0]]
                    print(
                        f"    [DEBUG] Found skill-creator with key '{matching_keys[0]}': {skill_creator_path}"
                    )
                else:
                    print(
                        f"    ⚠ skill-creator not found in imported skills. Available: {list(skill_paths_map.keys())}"
                    )
            else:
                print(f"    [DEBUG] Using skill-creator path: {skill_creator_path}")
                # Normalize path: remove trailing slash to match parent tuple format
                # Parent tuples use paths without trailing slash (e.g., /skill/skill-creator)
                # but skill import returns paths with trailing slash (e.g., /skill/skill-creator/)
                skill_creator_path_normalized = skill_creator_path.rstrip("/")
                print(
                    f"    [DEBUG] Normalized path (removed trailing slash): {skill_creator_path_normalized}"
                )

                # Grant direct_viewer on the skill-creator directory itself (without trailing slash)
                # Files inside should inherit via parent_viewer from this permission
                # The parent tuples point to paths without trailing slash, so we must match that format
                nx.rebac_create(
                    subject=("agent", agent_id),
                    relation="direct_viewer",  # Read-only access
                    object=("file", skill_creator_path_normalized),
                    tenant_id=tenant_id,
                    context=admin_context,
                )
                print(
                    f"    ✓ Granted read-only access to skill-creator skill directory at {skill_creator_path_normalized}"
                )
                print("    Note: Files inside should inherit parent_viewer from this permission")

                # Verify the permission was created by checking if agent can list the skill directory
                try:
                    # Try to list the skill directory as the agent to verify access
                    skill_list = nx.list(skill_creator_path, context=admin_context)
                    print(
                        f"    [DEBUG] Agent can list skill directory: {len(skill_list) if skill_list else 0} items"
                    )

                    # Also check permission directly
                    check_result = nx.rebac_check(
                        subject=("agent", agent_id),
                        permission="read",
                        object=("file", skill_creator_path),
                        tenant_id=tenant_id,
                        context=admin_context,
                    )
                    print(f"    [DEBUG] Direct permission check (read): {check_result}")
                except Exception as check_e:
                    print(f"    [DEBUG] Could not verify permission: {check_e}")
                    import traceback

                    traceback.print_exc()
        except Exception as e:
            print(f"    ✗ Failed to grant read-only access to skill-creator: {e}")
            import traceback

            traceback.print_exc()


def provision_default_skills(
    nx: Any, tenant_id: str, user_id: str, context: OperationContext
) -> dict[str, str]:
    """Import default skills from pre-zipped .skill files in data/skills/.

    Skills are imported from:
    - nexus/data/skills/skill-creator.skill
    - nexus/data/skills/pdf.skill
    - nexus/data/skills/docx.skill
    - nexus/data/skills/xlsx.skill
    - nexus/data/skills/pptx.skill
    - nexus/data/skills/internal-comms.skill

    Returns:
        Dictionary mapping skill names to their paths (e.g., {"skill-creator": "/tenant:.../skill/skill-creator/"})
    """
    print(f"\nImporting default skills for {tenant_id}/{user_id}...")

    # Get the nexus directory (parent of scripts/)
    nexus_dir = Path(__file__).parent.parent
    skills_dir = nexus_dir / "data" / "skills"

    skill_files = [
        "skill-creator.skill",
        "pdf.skill",
        "docx.skill",
        "xlsx.skill",
        "pptx.skill",
        "internal-comms.skill",
    ]

    skill_paths_map = {}  # Map skill name -> skill path

    for skill_file_name in skill_files:
        skill_file_path = skills_dir / skill_file_name

        if not skill_file_path.exists():
            print(f"  ⚠ Skill file not found: {skill_file_path}, skipping")
            continue

        try:
            print(f"  📥 Importing skill: {skill_file_name}...")
            # Read the .skill file
            with open(skill_file_path, "rb") as f:
                zip_bytes = f.read()

            # Encode to base64 for skills_import API
            zip_base64 = base64.b64encode(zip_bytes).decode("utf-8")

            # Import the skill to admin's personal skill folder
            result = nx.skills_import(
                zip_data=zip_base64,
                tier="personal",  # Import to personal skills
                allow_overwrite=True,  # Allow overwriting if skill already exists
                context=context,
            )

            imported_skills = result.get("imported_skills", [])
            skill_paths = result.get("skill_paths", [])

            # Map skill names to their paths
            for skill_name, skill_path in zip(imported_skills, skill_paths, strict=True):
                skill_paths_map[skill_name] = skill_path
                print(f"  ✓ Successfully imported skill '{skill_name}' to {skill_path}")

            if not imported_skills:
                print(f"  ⚠ No skills were imported from {skill_file_name}")

        except Exception as e:
            print(f"  ✗ Failed to import skill {skill_file_name}: {e}")
            import traceback

            traceback.print_exc()

    return skill_paths_map


def load_env_file(env_file: str = ".env") -> dict:
    """Load environment variables from .env file."""
    env_vars = {}
    env_path = Path(env_file)

    if not env_path.exists():
        # Try in nexus directory
        nexus_env_path = Path(__file__).parent.parent / env_file
        if nexus_env_path.exists():
            env_path = nexus_env_path

    if env_path.exists():
        print(f"Loading environment from {env_path}")
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    # Remove quotes if present
                    value = value.strip("\"'")
                    env_vars[key.strip()] = value
        print(f"  ✓ Loaded {len(env_vars)} environment variables")
    else:
        print(f"  ⚠  {env_file} not found, skipping")

    return env_vars


def ensure_admin_api_key(tenant_id: str = "default", env_file: str = ".env") -> str | None:
    """Ensure admin API key exists, loading from .env.local or creating if needed.

    Returns:
        API key string if found/created, None otherwise
    """
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.core.entity_registry import EntityRegistry
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth

    # Load .env
    env_vars = load_env_file(env_file)
    api_key: str | None = env_vars.get("NEXUS_API_KEY")

    # Get database URL
    database_url = os.getenv("NEXUS_DATABASE_URL")
    if not database_url:
        # Try to construct from common defaults
        if "NEXUS_DATABASE_URL" in env_vars:
            database_url = env_vars["NEXUS_DATABASE_URL"]
        else:
            print("  ⚠  NEXUS_DATABASE_URL not set, cannot verify/create API key")
            if api_key:
                print(f"  ℹ  Using API key from {env_file}: {api_key[:20]}...")
                return api_key
            return None

    # At this point database_url is guaranteed to be a string
    assert database_url is not None

    # Create engine and session
    engine = create_engine(database_url)
    SessionFactory = sessionmaker(bind=engine)

    # Check if API key from .env exists in database
    if api_key:
        print(f"  ℹ  Found API key in {env_file}")
        # Try to verify the key exists (we can't easily check without hashing, so we'll just note it)
        print(f"  ℹ  Using API key: {api_key[:30]}...")
        return api_key

    # No API key in .env, check if admin key exists in database
    print(f"  ℹ  No API key in {env_file}, checking database...")
    with SessionFactory() as session:
        from sqlalchemy import select

        # APIKeyModel lives in storage.models (not database_key)
        from nexus.storage.models import APIKeyModel

        # Check for existing admin keys
        result = session.execute(
            select(APIKeyModel)
            .where(
                APIKeyModel.user_id == "admin", APIKeyModel.is_admin == 1, APIKeyModel.revoked == 0
            )
            .limit(1)
        )
        existing_key = result.scalar_one_or_none()

        if existing_key:
            print("  ✓ Found existing admin API key in database")
            print("  ⚠  Note: Cannot retrieve existing key value (keys are hashed)")
            print(f"  ℹ  If you have the key, add it to {env_file} as NEXUS_API_KEY")
            return None

        # Create new admin API key
        print("  Creating new admin API key...")
        try:
            # Register admin user in entity registry
            entity_registry = EntityRegistry(SessionFactory)
            entity_registry.register_entity(
                entity_type="user",
                entity_id="admin",
                parent_type="tenant",
                parent_id=tenant_id,
            )

            # Create API key
            key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="admin",
                name="Admin key (provisioning)",
                tenant_id=tenant_id,
                is_admin=True,
                expires_at=None,
            )
            session.commit()

            print("  ✓ Created new admin API key")
            print(f"  API Key: {raw_key}")
            print(f"  Add this to {env_file} as: NEXUS_API_KEY={raw_key}")
            return raw_key

        except Exception as e:
            print(f"  ✗ Failed to create API key: {e}")
            return None


def provision_user_resources(nx: Any, tenant_id: str, user_id: str) -> None:
    """Create user-owned resources."""
    print(f"Creating user resources for {tenant_id}/{user_id}...")

    # Create admin context for provisioning
    context = OperationContext(
        user="system",
        groups=[],
        tenant_id=tenant_id,
        is_admin=True,
        is_system=False,
    )

    try:
        # User workspace (personal)
        workspace_id = generate_resource_id("workspace", "personal")
        workspace_path = user_path(tenant_id, user_id, "workspace", workspace_id)
        nx.register_workspace(workspace_path, name="Personal Workspace", context=context)

        # CRITICAL: Grant ownership via ReBAC
        nx.rebac_create(
            subject=("user", user_id),
            relation="owner-of",
            object=("file", workspace_path),
            tenant_id=tenant_id,
            context=context,
        )
        print(f"  ✓ Created {workspace_path} with ownership")
    except Exception as e:
        print(f"  ✗ Failed to create user workspace: {e}")

    try:
        # Default workspace
        default_workspace_id = generate_resource_id("workspace", "default_workspace")
        default_workspace_path = user_path(tenant_id, user_id, "workspace", default_workspace_id)
        nx.register_workspace(default_workspace_path, name="Default Workspace", context=context)

        # CRITICAL: Grant ownership via ReBAC
        nx.rebac_create(
            subject=("user", user_id),
            relation="owner-of",
            object=("file", default_workspace_path),
            tenant_id=tenant_id,
            context=context,
        )
        print(f"  ✓ Created {default_workspace_path} with ownership")
    except Exception as e:
        print(f"  ✗ Failed to create default workspace: {e}")

    # Create context with the actual user (not system) so agents are owned by the user
    user_context = OperationContext(
        user=user_id,  # Set user to the actual user_id, not system
        groups=[],
        tenant_id=tenant_id,
        is_admin=True,
        is_system=False,
    )

    # Create standard agents (ImpersonatedUser and UntrustedAgent)
    agent_metadata = {
        "platform": "langgraph",
        "endpoint_url": "http://localhost:2024",
        "agent_id": "agent",
    }
    agent_results = create_standard_agents(nx, user_id, user_context, agent_metadata)

    # Grant read-only permissions to UntrustedAgent for resource folders
    # Note: 'skill' is excluded from AGENT_RESOURCE_GRANTS by default
    if agent_results["untrusted"]:
        grant_agent_resource_access(
            nx, user_id, tenant_id, AGENT_RESOURCE_GRANTS, agent_name="UntrustedAgent"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision Nexus with namespace convention")
    parser.add_argument("--tenant", default="default", help="Tenant ID (default: default)")
    parser.add_argument("--user", help="User ID (optional, for user resources)")
    parser.add_argument("--config", help="Nexus config file path")
    parser.add_argument(
        "--env-file", default=".env", help="Environment file to load (default: .env)"
    )

    args = parser.parse_args()

    # Ensure admin API key exists (load from .env.local or create)
    print("\n" + "=" * 60)
    print("API Key Setup")
    print("=" * 60)
    api_key = ensure_admin_api_key(args.tenant, args.env_file)
    if api_key:
        # Set it in environment for this process
        os.environ["NEXUS_API_KEY"] = api_key
        print("  ✓ API key available")
    print("=" * 60 + "\n")

    # Connect to Nexus
    nx = nexus.connect(config=args.config) if args.config else nexus.connect()

    try:
        # Provision resources
        provision_system_resources(nx)
        provision_tenant_resources(nx, args.tenant)

        # Always provision admin user folders
        provision_admin_user_folders(nx, args.tenant)

        if args.user:
            provision_user_resources(nx, args.tenant, args.user)

        print("\n✓ Provisioning complete!")
    except Exception as e:
        print(f"\n✗ Provisioning failed: {e}")
        raise
    finally:
        # Close connection if it has a close method
        if hasattr(nx, "close"):
            nx.close()


if __name__ == "__main__":
    main()
