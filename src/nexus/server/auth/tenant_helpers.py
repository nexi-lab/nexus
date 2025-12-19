"""Tenant management helper functions.

Utilities for creating, validating, and managing tenants.
"""

import re
from typing import Any

from sqlalchemy.orm import Session

from nexus.storage.models import TenantModel


# Personal email providers (free email services)
# Users with these domains get personal workspaces
PERSONAL_EMAIL_DOMAINS = {
    # Google
    "gmail.com",
    "googlemail.com",
    # Microsoft
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    # Yahoo
    "yahoo.com",
    "yahoo.co.uk",
    "yahoo.ca",
    "yahoo.fr",
    "yahoo.de",
    "ymail.com",
    # Apple
    "icloud.com",
    "me.com",
    "mac.com",
    # Other popular providers
    "aol.com",
    "protonmail.com",
    "proton.me",
    "mail.com",
    "zoho.com",
    "fastmail.com",
    "gmx.com",
    "gmx.net",
    "qq.com",
    "163.com",
    "126.com",
}


# Reserved tenant_id values that cannot be used
RESERVED_TENANT_IDS = {
    # System identifiers
    "admin",
    "system",
    "default",
    "tenant",
    "user",
    "agent",
    "group",
    "root",
    "nexus",
    # Common routes/endpoints
    "api",
    "auth",
    "oauth",
    "login",
    "signup",
    "register",
    "logout",
    "callback",
    "health",
    "status",
    "docs",
    "swagger",
    # Reserved for future use
    "settings",
    "billing",
    "support",
    "help",
    "pricing",
    "features",
}


def is_personal_email_domain(domain: str) -> bool:
    """Check if email domain is a personal/free email provider.

    Args:
        domain: Email domain (e.g., "gmail.com", "acme.com")

    Returns:
        True if personal email provider, False if company domain

    Example:
        >>> is_personal_email_domain("gmail.com")
        True
        >>> is_personal_email_domain("acme.com")
        False
    """
    return domain.lower() in PERSONAL_EMAIL_DOMAINS


def get_tenant_strategy_from_email(
    email: str,
) -> tuple[str, str, str | None, bool]:
    """Determine tenant strategy based on email domain.

    Args:
        email: User's email address

    Returns:
        Tuple of (base_slug, tenant_name_base, domain, is_personal)
        - base_slug: Base for tenant_id generation
        - tenant_name_base: Base for tenant display name
        - domain: Domain to store in tenant
        - is_personal: True if personal workspace, False if company tenant

    Example:
        >>> get_tenant_strategy_from_email("alice@gmail.com")
        ("alice", "alice", "gmail.com", True)  # Personal workspace

        >>> get_tenant_strategy_from_email("bob@acme.com")
        ("acme-com", "Acme", "acme.com", False)  # Company tenant
    """
    if "@" not in email:
        # Fallback for invalid email
        return "user", "user", None, True

    username, domain = email.split("@", 1)
    domain = domain.lower()

    if is_personal_email_domain(domain):
        # Personal email: Use username as tenant_id base
        # tenant_name will be "<FirstName>'s Workspace"
        return username, username, domain, True
    else:
        # Company email: Use domain as tenant_id base
        # Convert "acme.com" → "acme-com" slug
        domain_slug = domain.replace(".", "-")

        # Extract company name from domain for display
        # "acme.com" → "Acme", "tech-startup.io" → "Tech Startup"
        company_name = domain.split(".")[0].replace("-", " ").title()

        return domain_slug, company_name, domain, False


def validate_tenant_id(tenant_id: str) -> tuple[bool, str | None]:
    """Validate tenant_id format and check for reserved names.

    Rules:
    - Length: 3-63 characters
    - Format: lowercase alphanumeric + hyphens
    - Cannot start or end with hyphen
    - Cannot be a reserved name

    Args:
        tenant_id: Tenant identifier to validate

    Returns:
        Tuple of (is_valid, error_message)

    Example:
        >>> validate_tenant_id("acme")
        (True, None)
        >>> validate_tenant_id("admin")
        (False, "Tenant ID 'admin' is reserved")
        >>> validate_tenant_id("a")
        (False, "Tenant ID must be 3-63 characters")
    """
    # Check length
    if len(tenant_id) < 3:
        return False, "Tenant ID must be at least 3 characters"
    if len(tenant_id) > 63:
        return False, "Tenant ID must be 63 characters or less"

    # Check format: lowercase alphanumeric + hyphens, cannot start/end with hyphen
    pattern = r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$"
    if not re.match(pattern, tenant_id):
        return (
            False,
            "Tenant ID must be lowercase alphanumeric (a-z, 0-9) with hyphens, "
            "and cannot start or end with a hyphen",
        )

    # Check reserved names
    if tenant_id in RESERVED_TENANT_IDS:
        return False, f"Tenant ID '{tenant_id}' is reserved"

    return True, None


def is_tenant_id_available(session: Session, tenant_id: str) -> bool:
    """Check if tenant_id is available (not already taken).

    Args:
        session: Database session
        tenant_id: Tenant identifier to check

    Returns:
        True if available, False if already taken
    """
    existing = session.get(TenantModel, tenant_id)
    return existing is None


def suggest_tenant_id(base_name: str, session: Session) -> str:
    """Suggest an available tenant_id based on a base name.

    If base_name is taken, tries appending numbers (base-2, base-3, etc.)
    until an available ID is found.

    Args:
        base_name: Desired base name (e.g., "acme" or "Acme Corp")
        session: Database session

    Returns:
        Available tenant_id

    Example:
        >>> suggest_tenant_id("Acme Corp", session)
        "acme-corp"  # if available
        >>> suggest_tenant_id("acme", session)
        "acme-2"  # if "acme" is taken
    """
    # Normalize base name to slug format
    tenant_id = normalize_to_slug(base_name)

    # If normalized slug is available, return it
    if is_tenant_id_available(session, tenant_id):
        return tenant_id

    # Try appending numbers
    counter = 2
    while counter < 1000:  # Reasonable limit
        candidate = f"{tenant_id}-{counter}"
        if is_tenant_id_available(session, candidate):
            return candidate
        counter += 1

    # Fallback: use timestamp
    import time

    return f"{tenant_id}-{int(time.time())}"


def normalize_to_slug(name: str) -> str:
    """Normalize a name to a valid tenant_id slug format.

    Converts to lowercase, replaces spaces and special characters with hyphens,
    removes consecutive hyphens, and trims to valid length.

    Args:
        name: Original name (e.g., "Acme Corporation")

    Returns:
        Normalized slug (e.g., "acme-corporation")

    Example:
        >>> normalize_to_slug("Acme Corporation")
        "acme-corporation"
        >>> normalize_to_slug("Tech@Startup!!!  Inc.")
        "tech-startup-inc"
    """
    # Convert to lowercase
    slug = name.lower()

    # Replace spaces and special characters with hyphens
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)

    # Remove consecutive hyphens
    slug = re.sub(r"-+", "-", slug)

    # Remove leading/trailing hyphens
    slug = slug.strip("-")

    # Ensure minimum length (pad with numbers if needed)
    if len(slug) < 3:
        slug = f"{slug}-org"

    # Trim to maximum length
    if len(slug) > 63:
        slug = slug[:63].rstrip("-")

    return slug


def create_tenant(
    session: Session,
    tenant_id: str,
    name: str,
    domain: str | None = None,
    description: str | None = None,
    settings: str | None = None,
) -> TenantModel:
    """Create a new tenant with validation.

    Args:
        session: Database session
        tenant_id: Desired tenant identifier (validated before creation)
        name: Display name for the tenant
        domain: Optional domain (e.g., "acme.com")
        description: Optional description
        settings: Optional JSON settings

    Returns:
        Created TenantModel

    Raises:
        ValueError: If tenant_id is invalid or already taken
    """
    # Validate tenant_id format
    is_valid, error_msg = validate_tenant_id(tenant_id)
    if not is_valid:
        raise ValueError(f"Invalid tenant_id: {error_msg}")

    # Check availability
    if not is_tenant_id_available(session, tenant_id):
        raise ValueError(f"Tenant ID '{tenant_id}' is already taken")

    # Create tenant
    from datetime import UTC, datetime

    tenant = TenantModel(
        tenant_id=tenant_id,
        name=name,
        domain=domain,
        description=description,
        settings=settings,
        is_active=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    session.add(tenant)
    session.commit()

    return tenant
