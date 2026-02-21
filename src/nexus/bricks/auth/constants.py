"""Auth brick constants — single source of truth (Decisions #5, #6).

Consolidates constants previously duplicated across:
- server/auth/database_key.py (API_KEY_PREFIX, HMAC_SALT)
- server/auth/static_key.py (API_KEY_PREFIX)
- server/auth/factory.py (API_KEY_PREFIX)
- server/auth/zone_helpers.py (PERSONAL_EMAIL_DOMAINS)
- server/auth/auth_routes.py (partial personal domain list)
"""

from __future__ import annotations

# P0-1: Token type discrimination
API_KEY_PREFIX = "sk-"

# P0-5: API key security
API_KEY_MIN_LENGTH = 32
HMAC_SALT = "nexus-api-key-v1"

# Personal email providers (free email services).
# Users with these domains get personal workspaces.
PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
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
)

# Reserved zone_id values that cannot be used
RESERVED_ZONE_IDS: frozenset[str] = frozenset(
    {
        # System identifiers
        "admin",
        "system",
        "default",
        "zone",
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
)
