"""Regression tests for FederationRPCService registration gating (#3784).

Pre-#3784 bug: ``_federation_rpc_active`` gated registration on
``kernel.zone_list()`` being non-empty, which made bootstrap impossible —
a fresh stack has zero zones, so ``federation_list_zones`` returned
``"Method not found"`` at the exact moment it was needed most (to confirm
the zone list is in fact empty).

Phase H of the rust-workspace restructure replaced the old
``mount_reconciliation_done`` + ``zone_list`` PyKernel methods with a
single ``nexus_runtime.federation_is_initialized(kernel)`` module
helper.  Federation lifecycle is a kernel-internal HAL concern; the
gating function fails closed when the helper raises.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from nexus.server.fastapi_server import _federation_rpc_active


def _install_runtime_stub(monkeypatch, fn):
    """Install a fake ``nexus_runtime.federation_is_initialized``."""
    fake = SimpleNamespace(federation_is_initialized=fn)
    monkeypatch.setitem(sys.modules, "nexus_runtime", fake)


class TestFederationRPCGating:
    def test_none_kernel_inactive(self) -> None:
        assert _federation_rpc_active(None) is False

    def test_initialized_active(self, monkeypatch) -> None:
        """Regression: federation_is_initialized=True → RPC active even
        with zero zones."""
        _install_runtime_stub(monkeypatch, lambda _k: True)
        assert _federation_rpc_active(SimpleNamespace()) is True

    def test_uninitialized_inactive(self, monkeypatch) -> None:
        _install_runtime_stub(monkeypatch, lambda _k: False)
        assert _federation_rpc_active(SimpleNamespace()) is False

    def test_helper_raises_inactive(self, monkeypatch) -> None:
        """If the readiness probe raises, fail closed (treat as inactive)."""

        def boom(_k):
            raise RuntimeError("federation provider not installed")

        _install_runtime_stub(monkeypatch, boom)
        assert _federation_rpc_active(SimpleNamespace()) is False
