"""Shared test context constants for NexusFS tests.

After #1801, NexusFS no longer fabricates identity — callers must provide
an OperationContext.  Tests inject one of these shared constants via
``nx._default_context = TEST_CONTEXT`` after construction.
"""

from nexus.contracts.types import OperationContext

TEST_CONTEXT = OperationContext(
    user_id="test",
    groups=[],
    is_admin=False,
)

TEST_ADMIN_CONTEXT = OperationContext(
    user_id="test-admin",
    groups=[],
    is_admin=True,
)
