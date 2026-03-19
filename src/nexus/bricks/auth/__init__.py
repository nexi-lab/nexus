"""Auth brick — self-contained authentication module (Issue #1399).

Exports types and protocol only at package level (lazy loading).
Import providers and service explicitly when needed.
"""

from nexus.bricks.auth.constants import (
    API_KEY_MIN_LENGTH,
    API_KEY_PREFIX,
    HMAC_SALT,
    PERSONAL_EMAIL_DOMAINS,
    RESERVED_ZONE_IDS,
    get_hmac_secret,
)
from nexus.bricks.auth.protocol import AuthBrickProtocol
from nexus.bricks.auth.types import AuthConfig, AuthResult

__all__ = [
    # Types
    "AuthResult",
    "AuthConfig",
    # Protocol
    "AuthBrickProtocol",
    # Constants
    "API_KEY_PREFIX",
    "API_KEY_MIN_LENGTH",
    "HMAC_SALT",
    "get_hmac_secret",
    "PERSONAL_EMAIL_DOMAINS",
    "RESERVED_ZONE_IDS",
]
