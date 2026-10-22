"""HTTP credentials cannot borrow ambient MCP authority."""

import pytest
from starlette.datastructures import Headers

from nexus.bricks.mcp.credentials import http_api_key


@pytest.mark.parametrize(
    "pairs",
    [
        [],
        [(b"authorization", b"Bearer ")],
        [(b"authorization", b"Basic credentials")],
        [(b"authorization", b"Bearer one two")],
        [(b"x-nexus-api-key", b" ")],
        [(b"authorization", b"Bearer first"), (b"authorization", b"Bearer second")],
        [(b"x-nexus-api-key", b"first"), (b"x-nexus-api-key", b"second")],
        [(b"authorization", b"Basic invalid"), (b"x-nexus-api-key", b"valid-key")],
        [(b"authorization", b"Bearer same"), (b"x-nexus-api-key", b"same")],
    ],
)
def test_absent_invalid_or_ambiguous_credentials_are_explicitly_anonymous(pairs):
    assert http_api_key(Headers(raw=pairs)) == ""


@pytest.mark.parametrize(
    "header,value",
    [
        ("Authorization", "Bearer opaque-native-key"),
        ("Authorization", "bEaReR opaque-native-key"),
        ("X-Nexus-API-Key", "opaque-native-key"),
    ],
)
def test_original_opaque_credential_is_forwarded(header, value):
    assert http_api_key(Headers({header: value})) == "opaque-native-key"
