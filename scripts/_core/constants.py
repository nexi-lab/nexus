"""
Constants for namespace provisioning operations.

This module centralizes all hardcoded values and resource type definitions
used across provisioning scripts.
"""

# Resource type prefixes for ID generation
RESOURCE_PREFIXES = {
    "workspace": "ws",
    "resource": "res",
    "connector": "conn",
    "memory": "mem",
    "skill": "skill",
    "agent": "agent",
}

# All resource types that can be provisioned
ALL_RESOURCE_TYPES = ["workspace", "memory", "skill", "agent", "connector", "resource"]

# Resource types that agents can access (read-only viewer permissions)
# Note: 'skill' is intentionally excluded - agents should have zero skill access by default
# Skills must be explicitly granted if needed for security
AGENT_RESOURCE_GRANTS = ["resource"]

# Default admin user and tenant IDs
DEFAULT_ADMIN_USER = "admin"
DEFAULT_TENANT_ID = "default"

# Default API key (for development only - should be in environment in production)
DEFAULT_ADMIN_API_KEY = "sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
