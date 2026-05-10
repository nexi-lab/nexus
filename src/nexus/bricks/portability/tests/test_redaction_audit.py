"""CI gate: every registered connector must pass the secret-shape audit.

Adding a backend whose CONNECTION_ARGS has a key like 'token_xyz' or
'auth_secret' without marking it secret=True must fail this test.
"""

import pytest

from nexus.backends._manifest import CONNECTOR_MANIFEST
from nexus.bricks.portability.redaction import (
    SECRET_SHAPED,
    _get_connection_args,
    audit_backend,
)


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_connector_registry():
    """Register manifest placeholders + attempt connector imports once per session.

    Without this, ConnectorRegistry is empty and _get_connection_args raises
    KeyError for every entry. Called once at session start; the idempotency
    guard inside _register_optional_backends() makes repeated calls a no-op.
    """
    from nexus.backends import _register_optional_backends

    _register_optional_backends()


@pytest.mark.parametrize("entry", CONNECTOR_MANIFEST, ids=lambda e: e.name)
def test_connector_passes_secret_shape_audit(entry):
    """Every CONNECTION_ARGS key matching SECRET_SHAPED must be marked secret=True."""
    args = _get_connection_args(entry.name)
    if not args:
        pytest.skip(f"{entry.name}: connector class not loaded (optional extra not installed)")
    leaks = audit_backend(entry.name)
    assert not leaks, (
        f"{entry.name}: argument names {leaks} match secret-shape regex "
        f"({SECRET_SHAPED.pattern}) but are not marked secret=True. "
        f"Either rename them or set secret=True in CONNECTION_ARGS."
    )
