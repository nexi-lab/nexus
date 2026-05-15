"""Regression tests for FederationRPCService registration gating (#3784, #3793).

History:

* #3784 — original bug: ``_federation_rpc_active`` gated registration on
  ``kernel.zone_list()`` being non-empty, which made bootstrap impossible
  (a fresh stack has zero zones, so ``federation_list_zones`` returned
  ``"Method not found"`` at the moment it was needed most). Phase H of
  the rust-workspace restructure replaced ``mount_reconciliation_done``
  + ``zone_list`` with ``nexus_runtime.federation_is_initialized``; the
  gate was tightened to fail closed when that helper raised.

* #3793 — federation_export_zone / federation_import_zone /
  federation_list_zones must also reach the kernel in standalone mode
  (no raft init). The gate was loosened again to ``kernel is not None``
  so the data-portability RPCs are always reachable. Federation-only
  RPCs (federation_join, federation_share, etc.) still surface a clear
  error from the underlying kernel call when invoked without raft —
  preferable to ``Method not found`` which obscured the cause.
"""

from __future__ import annotations

from types import SimpleNamespace

from nexus.server.fastapi_server import _federation_rpc_active


class TestFederationRPCGating:
    def test_none_kernel_inactive(self) -> None:
        assert _federation_rpc_active(None) is False

    def test_kernel_present_active(self) -> None:
        """#3793: kernel present ⇒ RPC active regardless of raft init.

        FederationRPCService is mounted in both standalone and federation
        modes so data-portability RPCs reach the kernel.
        """
        assert _federation_rpc_active(SimpleNamespace()) is True
