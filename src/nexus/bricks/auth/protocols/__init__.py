"""Auth brick Protocol interfaces.

Defines the contracts that the Auth brick requires from external systems.
Concrete implementations are wired by factory.py at boot time.

Issue #2281: Extract Auth/OAuth brick from server/auth.
"""

from nexus.bricks.auth.protocols.user_lookup import UserLookupProtocol

__all__ = [
    "UserLookupProtocol",
]
