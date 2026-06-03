"""S3-vs-local ReBAC allow/deny parity (#4267).

Enforcement happens above the backend boundary, so allow/deny must be identical
regardless of backend. Asserted on both a local-backed and an S3-backed file.

Spike findings (2026-06-02):
- Bare NexusFS(enforce=True) does NOT enforce without the factory-wired hook.
- Manually wiring RebacPermissionCheckHook + ReBACManager (in-memory SQLite)
  produces correct deny (PermissionError) and allow after rebac_write().
- Relation for read access: ``"direct_viewer"`` (not plain ``"viewer"`` which
  is a union alias that the graph resolver expands differently).
- Object key for both local and S3 paths: ``("file", <virtual_path>)``
  because dlc=None falls back to the virtual-path default in PermissionEnforcer.
"""

from __future__ import annotations

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext


def _user(uid: str) -> OperationContext:
    return OperationContext(user_id=uid, groups=[], zone_id=ROOT_ZONE_ID, is_admin=False)


@pytest.mark.integration
class TestRebacParity:
    def test_deny_without_grant_parity(self, parity_kernel_enforced):
        """Non-admin user cannot read a file on either backend without a grant."""
        h = parity_kernel_enforced  # h.ctx is admin
        alice = _user("alice_deny")
        for p in h.paths("rebac_deny.txt"):
            h.fs.write(p, b"secret", context=h.ctx)
            with pytest.raises(PermissionError):
                h.fs.sys_read(p, context=alice)

    def test_allow_after_grant_parity(self, parity_kernel_enforced):
        """After a direct_viewer grant, the user can read the file on both backends."""
        h = parity_kernel_enforced
        bob = _user("bob_allow")
        rebac_mgr = h.fs.service("rebac_mgr")
        for p in h.paths("rebac_allow.txt"):
            h.fs.write(p, b"shared", context=h.ctx)
            # Grant bob read access via the ``direct_viewer`` relation.
            # Plain ``viewer`` is a namespace alias (union of owner + direct_viewer);
            # the graph resolver checks the concrete relations, so we use direct_viewer.
            rebac_mgr.rebac_write(
                subject=("user", "bob_allow"),
                relation="direct_viewer",
                object=("file", p),
            )
            assert h.fs.sys_read(p, context=bob) == b"shared"
