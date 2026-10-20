"""Opaque HTTP caller credentials for the MCP transport."""

from starlette.datastructures import Headers

from nexus.lib.request_credentials import api_key_from_authorization


def http_api_key(headers: Headers) -> str:
    """Return one caller credential; missing or ambiguous headers are anonymous.

    An HTTP request must never inherit the MCP host's configured credential or
    certificate. The empty string preserves that distinction in native transports.
    """
    authorization = headers.getlist("Authorization")
    api_keys = headers.getlist("X-Nexus-API-Key")
    if len(authorization) + len(api_keys) != 1:
        return ""
    if authorization:
        return api_key_from_authorization(authorization[0]) or ""
    key = api_keys[0]
    return "" if any(character.isspace() for character in key) else key
