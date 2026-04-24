"""Regression tests for FederationRPCService registration gating (#3784).

Pre-#3784 bug: ``_federation_rpc_active`` gated registration on
``kernel.zone_list()`` being non-empty, which made bootstrap impossible —
a fresh stack has zero zones, so ``federation_list_zones`` returned
``"Method not found"`` at the exact moment it was needed most (to confirm
the zone list is in fact empty).

The fix gates on the *presence* of a callable ``zone_list`` attribute,
not its result. Zero-zone stacks still expose ``federation_list_zones``,
which returns ``{"zones": [], "node_id": []}``.
"""

from __future__ import annotations

from types import SimpleNamespace

from nexus.server.fastapi_server import _federation_rpc_active


def _fake_kernel(*, mrd_done: bool = True, has_zone_list: bool = True) -> object:
    attrs: dict[str, object] = {"mount_reconciliation_done": lambda: mrd_done}
    if has_zone_list:
        attrs["zone_list"] = lambda: []  # empty zone list — the bug case
    return SimpleNamespace(**attrs)


class TestFederationRPCGating:
    def test_none_kernel_inactive(self) -> None:
        assert _federation_rpc_active(None) is False

    def test_fresh_stack_with_empty_zones_is_active(self) -> None:
        """Regression: zero-zone kernel MUST still register federation RPC."""
        kernel = _fake_kernel(mrd_done=True, has_zone_list=True)
        assert _federation_rpc_active(kernel) is True

    def test_mount_reconciliation_not_done_inactive(self) -> None:
        kernel = _fake_kernel(mrd_done=False, has_zone_list=True)
        assert _federation_rpc_active(kernel) is False

    def test_slim_profile_no_zone_list_inactive(self) -> None:
        """Slim/embedded profiles lack zone_list — federation RPC skipped."""
        kernel = _fake_kernel(mrd_done=True, has_zone_list=False)
        assert _federation_rpc_active(kernel) is False

    def test_mrd_as_attribute_not_callable(self) -> None:
        """mount_reconciliation_done may be a plain attribute (not a method)."""
        kernel = SimpleNamespace(
            mount_reconciliation_done=True,
            zone_list=lambda: [],
        )
        assert _federation_rpc_active(kernel) is True

    def test_zone_list_raises_still_active(self) -> None:
        """zone_list is callable but raises — still count as 'available';
        the RPC call itself will propagate the error, but registration
        should not depend on runtime health of the accessor.
        """

        def boom() -> list[str]:
            raise RuntimeError("kernel not ready")

        kernel = SimpleNamespace(
            mount_reconciliation_done=lambda: True,
            zone_list=boom,
        )
        assert _federation_rpc_active(kernel) is True

    def test_mrd_raises_inactive(self) -> None:
        """If the bootstrap accessor itself raises, treat as inactive."""

        def boom() -> bool:
            raise RuntimeError("kernel not ready")

        kernel = SimpleNamespace(
            mount_reconciliation_done=boom,
            zone_list=lambda: [],
        )
        assert _federation_rpc_active(kernel) is False
