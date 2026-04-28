from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestFederationClientWhoami:
    def test_whoami_returns_zone_grants(self) -> None:
        from nexus.server.rpc.services.federation_rpc import FederationRPCMixin

        mixin = FederationRPCMixin.__new__(FederationRPCMixin)
        ctx = MagicMock()
        ctx.zone_perms = (("company", "r"), ("shared", "rw"))
        ctx.zone_id = None
        ctx.is_admin = False

        result = mixin.federation_client_whoami(_context=ctx)
        assert "zones" in result
        zones = {z["zone_id"]: z["permission"] for z in result["zones"]}
        assert zones["company"] == "r"
        assert zones["shared"] == "rw"

    def test_whoami_with_single_zone_context(self) -> None:
        from nexus.server.rpc.services.federation_rpc import FederationRPCMixin

        mixin = FederationRPCMixin.__new__(FederationRPCMixin)
        ctx = MagicMock()
        # No zone_perms — forces the single-zone branch (multi-zone branch is skipped
        # when zone_perms is falsy). zone_id carries the single zone grant.
        # Default permission for single-zone tokens is "rw" per OperationContext policy.
        ctx.zone_perms = None
        ctx.zone_id = "eng"
        ctx.is_admin = False

        result = mixin.federation_client_whoami(_context=ctx)
        assert result["zones"] == [{"zone_id": "eng", "permission": "rw"}]

    def test_whoami_with_no_context_raises(self) -> None:
        from nexus.contracts.exceptions import NexusPermissionError
        from nexus.server.rpc.services.federation_rpc import FederationRPCMixin

        mixin = FederationRPCMixin.__new__(FederationRPCMixin)

        with pytest.raises(NexusPermissionError):
            mixin.federation_client_whoami(_context=None)
