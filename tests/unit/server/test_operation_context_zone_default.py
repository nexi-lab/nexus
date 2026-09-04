"""Server-boundary zone defaults (#4740).

``get_operation_context`` used to coerce a missing zone claim to ROOT for
every caller, which is how a zone-less tenant key reached ``sys_readdir``
looking like a root-zone operator.  Now only admins default to ROOT; a
non-admin without a zone stays zone-less (and is refused by list/search).
Open-access mode, which has no tenant model, claims ROOT explicitly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.lib.zone_scoping import scope_params_for_zone
from nexus.server.dependencies import get_operation_context, resolve_auth


def _auth(**overrides):
    base = {
        "authenticated": True,
        "subject_type": "user",
        "subject_id": "alice",
        "is_admin": False,
    }
    base.update(overrides)
    return base


class TestGetOperationContextZone:
    def test_non_admin_without_zone_stays_zone_less(self) -> None:
        ctx = get_operation_context(_auth())
        assert ctx.zone_id is None
        assert ctx.zone_set == ()
        assert ctx.zone_perms == ()

    def test_non_admin_with_empty_zone_string_stays_zone_less(self) -> None:
        assert get_operation_context(_auth(zone_id="")).zone_id is None

    def test_admin_without_zone_defaults_to_root(self) -> None:
        ctx = get_operation_context(_auth(subject_id="admin", is_admin=True))
        assert ctx.zone_id == ROOT_ZONE_ID
        assert ctx.zone_set == (ROOT_ZONE_ID,)

    def test_provider_zone_is_kept(self) -> None:
        ctx = get_operation_context(_auth(zone_id="acme", zone_perms=[["acme", "rw"]]))
        assert ctx.zone_id == "acme"
        assert ctx.zone_perms == (("acme", "rw"),)

    def test_multi_zone_token_keeps_root_routing_placeholder(self) -> None:
        ctx = get_operation_context(
            _auth(zone_id="eng", zone_perms=[["eng", "r"], ["ops", "rw"]]),
        )
        assert ctx.zone_id == ROOT_ZONE_ID
        assert ctx.zone_perms == (("eng", "r"), ("ops", "rw"))


class TestRestZoneOverride:
    """REST ``?zone=`` override must retarget an admin's grants, not just zone_id (#4740)."""

    def test_admin_override_retargets_perms_so_the_zone_view_follows(self) -> None:
        from nexus.lib.zone_visibility import resolve_zone_view
        from nexus.server.api.v2.routers.async_files import _apply_zone_override

        auth = _auth(subject_id="admin", is_admin=True)
        ctx = get_operation_context(auth)
        assert ctx.zone_perms == ((ROOT_ZONE_ID, "rw"),)

        out = _apply_zone_override(ctx, "ta", auth)

        assert out is not ctx
        assert out.zone_id == "ta"
        assert out.zone_perms == (("ta", "rw"),)
        assert resolve_zone_view(out).zones == {"ta"}
        # the caller's own context is untouched
        assert ctx.zone_id == ROOT_ZONE_ID

    def test_non_admin_override_keeps_own_grants(self) -> None:
        from nexus.lib.zone_visibility import resolve_zone_view
        from nexus.server.api.v2.routers.async_files import _apply_zone_override

        auth = _auth(zone_id="eng", zone_perms=[["eng", "r"], ["ops", "r"]])
        ctx = get_operation_context(auth)
        out = _apply_zone_override(ctx, "ops", auth)
        assert out.zone_id == "ops"
        assert resolve_zone_view(out).zones == {"eng", "ops"}

    def test_no_override_returns_same_context(self) -> None:
        from nexus.server.api.v2.routers.async_files import _apply_zone_override

        auth = _auth(zone_id="eng", zone_perms=[["eng", "rw"]])
        ctx = get_operation_context(auth)
        assert _apply_zone_override(ctx, None, auth) is ctx


class TestScopeParamsForZoneLessContext:
    def test_none_zone_leaves_paths_untouched(self) -> None:
        params = SimpleNamespace(path="/a.txt", paths=["/b.txt"])
        scope_params_for_zone(params, None)
        assert params.path == "/a.txt"
        assert params.paths == ["/b.txt"]

    def test_root_zone_leaves_paths_untouched(self) -> None:
        params = SimpleNamespace(path="/a.txt")
        scope_params_for_zone(params, ROOT_ZONE_ID)
        assert params.path == "/a.txt"

    def test_concrete_zone_still_prefixes(self) -> None:
        params = SimpleNamespace(path="/a.txt")
        scope_params_for_zone(params, "acme")
        assert params.path == "/zone/acme/a.txt"


class TestOpenAccessResolvesRootExplicitly:
    @pytest.mark.asyncio
    async def test_no_header_means_root_zone(self) -> None:
        state = SimpleNamespace(api_key=None, auth_provider=None)
        result = await resolve_auth(
            state,
            x_nexus_subject="user:alice",
            client_host="127.0.0.1",
        )
        assert result is not None
        assert result["authenticated"] is True
        assert result["is_admin"] is False
        assert result["zone_id"] == ROOT_ZONE_ID
        assert get_operation_context(result).zone_id == ROOT_ZONE_ID

    @pytest.mark.asyncio
    async def test_zone_header_wins(self) -> None:
        state = SimpleNamespace(api_key=None, auth_provider=None)
        result = await resolve_auth(
            state,
            x_nexus_subject="user:alice",
            x_nexus_zone_id="acme",
            client_host="127.0.0.1",
        )
        assert result is not None
        assert result["zone_id"] == "acme"
