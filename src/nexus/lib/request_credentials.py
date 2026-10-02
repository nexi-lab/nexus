"""Ephemeral caller credentials shared by request ingress and outbound transports.

Keep credentials out of identity caches, operation contexts and query DTOs.
None denotes no bearer; an explicitly invalid/empty header remains an empty
credential so a transport cannot silently fall back to its node identity.
"""

from contextvars import ContextVar

request_api_key: ContextVar[str | None] = ContextVar("request_api_key", default=None)


def api_key_from_authorization(value: str | None) -> str | None:
    """Read the bearer or raw sk- form accepted by Nexus HTTP authentication."""
    if value is None:
        return None
    if value.startswith("sk-") and not any(c.isspace() for c in value):
        return value
    scheme, separator, token = value.partition(" ")
    if separator and scheme.lower() == "bearer" and token and not any(c.isspace() for c in token):
        return token
    return ""
